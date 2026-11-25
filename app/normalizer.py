"""Deterministic document normalization: .jpg/.png/.docx/.pdf -> A4 PDF, page ranges, chunking, duplex padding."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Iterable

import img2pdf
from docx import Document as DocxDocument
from PIL import Image, ImageOps
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.config import NORMALIZED_DIR
from app.models import DuplexMode
from app.rules_engine import load_rules

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89


def parse_page_range(spec: str, total_pages: int) -> list[int]:
    """Parse 'all' or '1-3,5,8-10' into 0-based page indices."""
    cleaned = (spec or "all").strip().lower()
    if not cleaned or cleaned == "all":
        return list(range(total_pages))

    indices: list[int] = []
    seen: set[int] = set()
    for part in cleaned.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = max(1, int(bounds[0].strip()))
                end = min(total_pages, int(bounds[1].strip()))
            except ValueError:
                continue
            for p in range(start, end + 1):
                idx = p - 1
                if idx not in seen:
                    seen.add(idx)
                    indices.append(idx)
        else:
            try:
                p = int(part)
            except ValueError:
                continue
            if 1 <= p <= total_pages:
                idx = p - 1
                if idx not in seen:
                    seen.add(idx)
                    indices.append(idx)

    return indices if indices else list(range(total_pages))


def _convert_image_to_a4_pdf(src: Path, dst: Path, auto_fixes: list[str]) -> None:
    """Convert .jpg/.png to an A4-fitted PDF, auto-orienting landscape images."""
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P", "LA"):
            rgb = Image.new("RGB", img.size, (255, 255, 255))
            rgb.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
            img = rgb
        elif img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        if width > height:
            img = img.rotate(90, expand=True)
            auto_fixes.append("Auto-rotated landscape image to fit A4")

        temp_jpg = dst.with_suffix(".tmp.jpg")
        img.save(temp_jpg, format="JPEG", quality=92)

    try:
        a4_layout = img2pdf.get_layout_fun((img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297)))
        dst.write_bytes(img2pdf.convert(str(temp_jpg), layout_fun=a4_layout))
        auto_fixes.append("Normalized image to A4 PDF")
    finally:
        if temp_jpg.exists():
            temp_jpg.unlink(missing_ok=True)


def _convert_docx_to_pdf(src: Path, dst: Path, auto_fixes: list[str]) -> None:
    """Convert .docx to A4 PDF using LibreOffice if present, otherwise built-in python-docx + ReportLab."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(dst.parent), str(src)],
                check=True,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            generated = dst.parent / f"{src.stem}.pdf"
            if generated.exists():
                if generated != dst:
                    generated.replace(dst)
                auto_fixes.append("Converted DOCX via LibreOffice to A4 PDF")
                return
        except Exception:
            pass

    # Pure-Python fallback so DOCX always works out of the box even without Word/LibreOffice
    doc = DocxDocument(str(src))
    styles = getSampleStyleSheet()
    story = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            story.append(Spacer(1, 8))
        else:
            safe_text = (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            story.append(Paragraph(safe_text, styles["Normal"]))
            story.append(Spacer(1, 4))

    if not story:
        story.append(Paragraph("(Empty Document)", styles["Normal"]))

    pdf_doc = SimpleDocTemplate(str(dst), pagesize=A4)
    pdf_doc.build(story)
    auto_fixes.append("Converted DOCX to A4 PDF (built-in engine)")


def _append_a4_scaled_page(
    writer: PdfWriter,
    page: PageObject,
    target_w: float,
    target_h: float,
) -> bool:
    """Append a page to writer, scaling non-A4/oversized pages cleanly without mutating detached pages."""
    w = float(page.mediabox.width)
    h = float(page.mediabox.height)

    # Already within ~3pt of A4 portrait
    if abs(w - target_w) <= 3.0 and abs(h - target_h) <= 3.0:
        writer.add_page(page)
        return False

    scale = min(target_w / w, target_h / h)
    tx = (target_w - w * scale) / 2.0 - float(page.mediabox.left) * scale
    ty = (target_h - h * scale) / 2.0 - float(page.mediabox.bottom) * scale

    dest_page = writer.add_blank_page(width=target_w, height=target_h)
    dest_page.merge_transformed_page(
        page,
        Transformation().scale(sx=scale, sy=scale).translate(tx=tx, ty=ty),
    )
    return True


def count_pages_quick(file_path: Path) -> int:
    """Fast page count for uploaded file."""
    ext = file_path.suffix.lower()
    if ext in (".jpg", ".jpeg", ".png"):
        return 1
    if ext == ".pdf":
        reader = PdfReader(str(file_path))
        return len(reader.pages)
    if ext == ".docx":
        tmp_pdf = NORMALIZED_DIR / f"_probe_{file_path.stem}.pdf"
        try:
            _convert_docx_to_pdf(file_path, tmp_pdf, [])
            reader = PdfReader(str(tmp_pdf))
            return len(reader.pages)
        finally:
            tmp_pdf.unlink(missing_ok=True)
    return 1


def normalize_and_chunk_job(
    job_id: str,
    source_path: Path,
    selected_pages_spec: str,
    duplex_mode: DuplexMode,
) -> tuple[Path, list[Path], int, int, list[str]]:
    """
    Normalize input (.jpg/.png/.docx/.pdf) to A4 PDF, apply page selection,
    pad odd-page duplex jobs with a trailing blank page, and split >50p jobs into chunks.
    Returns: (normalized_pdf, chunk_paths, total_pages, selected_page_count, auto_fixes)
    """
    rules = load_rules()
    target_w = float(rules.get("a4_width_pt", A4_WIDTH_PT))
    target_h = float(rules.get("a4_height_pt", A4_HEIGHT_PT))
    chunk_size = int(rules.get("chunk_threshold_pages", 50))
    if chunk_size % 2 != 0:
        chunk_size += 1  # Keep chunks even-sized for duplex safety

    auto_fixes: list[str] = []
    ext = source_path.suffix.lower()
    raw_pdf = NORMALIZED_DIR / f"{job_id}_raw.pdf"

    if ext in (".jpg", ".jpeg", ".png"):
        _convert_image_to_a4_pdf(source_path, raw_pdf, auto_fixes)
    elif ext == ".docx":
        _convert_docx_to_pdf(source_path, raw_pdf, auto_fixes)
    elif ext == ".pdf":
        shutil.copyfile(source_path, raw_pdf)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    reader = PdfReader(str(raw_pdf))
    total_pages = len(reader.pages)
    indices: Iterable[int] = parse_page_range(selected_pages_spec, total_pages)
    selected_indices = list(indices)
    selected_count = len(selected_indices)

    writer = PdfWriter()
    scaled_any = False
    for idx in selected_indices:
        if _append_a4_scaled_page(writer, reader.pages[idx], target_w, target_h):
            scaled_any = True

    if scaled_any and ext == ".pdf":
        auto_fixes.append("Scaled pages to A4")

    if duplex_mode == DuplexMode.DUPLEX and len(writer.pages) % 2 == 1:
        writer.add_blank_page(width=target_w, height=target_h)
        auto_fixes.append("Added trailing blank page for odd-page duplex")

    normalized_pdf = NORMALIZED_DIR / f"{job_id}_normalized.pdf"
    with normalized_pdf.open("wb") as f:
        writer.write(f)

    # Remove intermediate raw copy
    raw_pdf.unlink(missing_ok=True)

    # Split into chunks if larger than chunk_threshold_pages
    total_norm_pages = len(writer.pages)
    chunk_paths: list[Path] = []
    if total_norm_pages > chunk_size:
        for chunk_idx, start in enumerate(range(0, total_norm_pages, chunk_size), start=1):
            end = min(start + chunk_size, total_norm_pages)
            chunk_writer = PdfWriter()
            for p_idx in range(start, end):
                chunk_writer.add_page(writer.pages[p_idx])
            chunk_path = NORMALIZED_DIR / f"{job_id}_chunk_{chunk_idx:02d}.pdf"
            with chunk_path.open("wb") as cf:
                chunk_writer.write(cf)
            chunk_paths.append(chunk_path)
        auto_fixes.append(f"Split into {len(chunk_paths)}×{chunk_size}p safe chunks")
    else:
        chunk_paths.append(normalized_pdf)

    return normalized_pdf, chunk_paths, total_pages, selected_count, auto_fixes
