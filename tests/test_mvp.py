"""End-to-end tests for PrintShop Local Queue MVP (runs without a physical printer)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _make_pdf_bytes(num_pages: int, width: float = 595.28, height: float = 841.89) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_landscape_png_bytes() -> bytes:
    img = Image.new("RGB", (800, 400), color=(30, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_docx_bytes() -> bytes:
    doc = DocxDocument()
    doc.add_heading("PrintShop Test Document", level=1)
    doc.add_paragraph("Hello from PrintShop MVP automated test.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_info_and_qr(client: TestClient):
    res = client.get("/api/info")
    assert res.status_code == 200
    data = res.json()
    assert "shop_name" in data
    assert any(p["is_virtual"] for p in data["printers"])

    qr_res = client.get("/api/qr")
    assert qr_res.status_code == 200
    assert qr_res.headers["content-type"] == "image/png"
    assert len(qr_res.content) > 100


def test_pdf_odd_duplex_padding_and_chunking_and_virtual_print(client: TestClient):
    # 53-page PDF in duplex mode -> padded to 54 pages -> split into 2 chunks (50 + 4)
    pdf_bytes = _make_pdf_bytes(53)
    res = client.post(
        "/api/jobs",
        files={"file": ("thesis.pdf", pdf_bytes, "application/pdf")},
        data={"color_mode": "bw", "duplex_mode": "duplex", "copies": "1", "pages_spec": "all"},
    )
    assert res.status_code == 200
    job = res.json()
    assert job["selected_page_count"] == 53
    assert any("blank page" in fix.lower() for fix in job["auto_fixes"])
    assert any("split into" in fix.lower() for fix in job["auto_fixes"])

    # Approve using Virtual Print Simulator
    approve_res = client.post(
        f"/api/jobs/{job['job_id']}/approve",
        json={"printer_name": "Virtual Print Simulator (PDF)"},
    )
    assert approve_res.status_code == 200
    out_files = approve_res.json()["result"]["files"]
    assert len(out_files) == 2
    assert len(PdfReader(out_files[0]).pages) == 50
    assert len(PdfReader(out_files[1]).pages) == 4


def test_image_and_docx_normalization_and_reject_cleanup(client: TestClient):
    # Landscape PNG
    png_bytes = _make_landscape_png_bytes()
    res_img = client.post(
        "/api/jobs",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"color_mode": "color", "duplex_mode": "single", "copies": "2", "pages_spec": "all"},
    )
    assert res_img.status_code == 200
    img_job = res_img.json()
    assert any("auto-rotated" in fix.lower() for fix in img_job["auto_fixes"])

    # DOCX probe + upload
    docx_bytes = _make_docx_bytes()
    probe = client.post(
        "/api/probe-pages",
        files={"file": ("notes.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert probe.status_code == 200
    assert probe.json()["pages"] >= 1

    res_docx = client.post(
        "/api/jobs",
        files={"file": ("notes.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"color_mode": "bw", "duplex_mode": "single", "copies": "1", "pages_spec": "all"},
    )
    assert res_docx.status_code == 200
    docx_job_id = res_docx.json()["job_id"]

    # Reject and ensure preview file is deleted
    rej = client.post(f"/api/jobs/{docx_job_id}/reject")
    assert rej.status_code == 200
    prev = client.get(f"/api/jobs/{docx_job_id}/preview")
    assert prev.status_code == 404


def test_non_a4_pdf_scaling_to_a4(client: TestClient):
    # Simulate US Letter / slide deck (792 x 612 pt) which triggers A4 scaling in pypdf 7.x
    slide_pdf = _make_pdf_bytes(4, width=792.0, height=612.0)
    res = client.post(
        "/api/jobs",
        files={"file": ("slides.pdf", slide_pdf, "application/pdf")},
        data={"color_mode": "bw", "duplex_mode": "single", "copies": "1", "pages_spec": "all"},
    )
    assert res.status_code == 200
    job = res.json()
    assert any("scaled pages to a4" in fix.lower() for fix in job["auto_fixes"])


def test_mid_job_paper_jam_and_reroute_remaining_chunks(client: TestClient):
    # 63-page PDF -> 2 chunks (50 + 13). Faulty printer prints Chunk 1, jams on Chunk 2.
    pdf_bytes = _make_pdf_bytes(63)
    res = client.post(
        "/api/jobs",
        files={"file": ("unit5.pdf", pdf_bytes, "application/pdf")},
        data={"color_mode": "bw", "duplex_mode": "single", "copies": "1", "pages_spec": "all"},
    )
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    fail_res = client.post(
        f"/api/jobs/{job_id}/approve",
        json={"printer_name": "Virtual Faulty Printer (Jam Test)"},
    )
    assert fail_res.status_code == 500
    assert "Paper Jam" in fail_res.json()["detail"]

    # Reroute remaining chunks to Virtual Backup Laser (B&W) -> should ONLY print Chunk 2!
    reroute_res = client.post(
        f"/api/jobs/{job_id}/reroute",
        json={"backup_printer": "Virtual Backup Laser (B&W)"},
    )
    assert reroute_res.status_code == 200
    result_files = reroute_res.json()["result"]["files"]
    assert len(result_files) == 1
    assert "chunk_02_of_02" in result_files[0]
    assert reroute_res.json()["job"]["completed_chunks"] == 2


def test_hot_reload_rules_and_windows_spooler_probe(client: TestClient):
    spool = client.get("/api/spooler-health")
    assert spool.status_code == 200
    assert spool.json()["available"] is True

    # Hot-swap chunk threshold to 20 without restarting
    upd = client.post("/api/rules", json={"chunk_threshold_pages": 20, "version": "1.0.1"})
    assert upd.status_code == 200
    assert upd.json()["rules"]["chunk_threshold_pages"] == 20

    try:
        pdf_25 = _make_pdf_bytes(25)
        res = client.post(
            "/api/jobs",
            files={"file": ("small_batch.pdf", pdf_25, "application/pdf")},
            data={"color_mode": "bw", "duplex_mode": "single", "copies": "1", "pages_spec": "all"},
        )
        assert res.status_code == 200
        assert any("2×20p" in fix for fix in res.json()["auto_fixes"])
    finally:
        # Restore default 50-page threshold
        client.post("/api/rules", json={"chunk_threshold_pages": 50, "version": "1.0.0"})


def test_real_printer_fails_loudly_when_missing(client: TestClient):
    pdf_bytes = _make_pdf_bytes(2)
    res = client.post(
        "/api/jobs",
        files={"file": ("check.pdf", pdf_bytes, "application/pdf")},
        data={"color_mode": "bw", "duplex_mode": "single", "copies": "1", "pages_spec": "all"},
    )
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    # Non-virtual printer that does not exist must fail loudly (never silently skip)
    fail_res = client.post(
        f"/api/jobs/{job_id}/approve",
        json={"printer_name": "NonExistent_USB_Printer"},
    )
    assert fail_res.status_code == 500



