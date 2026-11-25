"""FastAPI + WebSocket backend for the local PrintShop queue and automation engine."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import (
    MOCK_PRINTS_DIR,
    NORMALIZED_DIR,
    RULES_PATH,
    UPLOADS_DIR,
    ensure_directories,
    load_config,
    save_config,
)
from app.models import (
    ApproveJobRequest,
    ColorMode,
    DuplexMode,
    JobStatus,
    OnboardingRequest,
    PrintJob,
    RerouteJobRequest,
)
from app.normalizer import count_pages_quick, normalize_and_chunk_job
from app.printer_service import execute_chunk, list_printers, probe_windows_spooler
from app.qr_utils import generate_qr_png_bytes, get_lan_ip
from app.rules_engine import calculate_price, load_rules, pick_best_printer

STATIC_DIR = Path(__file__).resolve().parent / "static"
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".docx"}

# In-memory job store for MVP speed (persisted files live in workspace data/)
JOBS: dict[str, PrintJob] = {}
TOKEN_COUNTER = 0
DASHBOARD_CLIENTS: set[WebSocket] = set()
CUSTOMER_CLIENTS: dict[str, set[WebSocket]] = {}


def _next_token() -> str:
    global TOKEN_COUNTER
    TOKEN_COUNTER = (TOKEN_COUNTER % 999) + 1
    return f"{TOKEN_COUNTER:03d}"


def _delete_job_files(job: PrintJob) -> None:
    """Securely remove uploaded source and normalized/chunk temp files."""
    paths = [job.source_path]
    if job.normalized_path:
        paths.append(job.normalized_path)
    paths.extend(job.chunk_paths)
    for raw in paths:
        try:
            p = Path(raw)
            if p.exists():
                p.unlink(missing_ok=True)
        except Exception:
            pass


def _job_position_info(job: PrintJob) -> dict[str, Any]:
    pending_jobs = [j for j in JOBS.values() if j.status == JobStatus.PENDING]
    pending_jobs.sort(key=lambda j: j.created_at)
    pos = next((idx + 1 for idx, j in enumerate(pending_jobs) if j.id == job.id), 0)
    est_minutes = max(1, pos * 1) if pos > 0 else 0
    return {
        "id": job.id,
        "token": job.token,
        "status": job.status.value,
        "filename": job.filename,
        "selected_page_count": job.selected_page_count,
        "copies": job.copies,
        "color_mode": job.color_mode.value,
        "duplex_mode": job.duplex_mode.value,
        "estimated_price": job.estimated_price,
        "currency": job.currency,
        "queue_position": pos,
        "estimated_wait_minutes": est_minutes,
        "assigned_printer": job.assigned_printer,
        "auto_fixes": job.auto_fixes,
        "error_message": job.error_message,
    }


def _dashboard_snapshot() -> dict[str, Any]:
    cfg = load_config()
    printers = [p.model_dump() for p in list_printers()]
    jobs_sorted = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
    return {
        "type": "queue_update",
        "shop_name": cfg["shop_name"],
        "onboarded": cfg.get("onboarded", False),
        "printers": printers,
        "jobs": [j.model_dump(mode="json") for j in jobs_sorted],
        "counts": {
            "pending": sum(1 for j in JOBS.values() if j.status == JobStatus.PENDING),
            "printing": sum(1 for j in JOBS.values() if j.status == JobStatus.PRINTING),
            "completed": sum(1 for j in JOBS.values() if j.status == JobStatus.COMPLETED),
        },
    }


async def broadcast_updates() -> None:
    snapshot = _dashboard_snapshot()
    dead_dash: list[WebSocket] = []
    for ws in list(DASHBOARD_CLIENTS):
        try:
            await ws.send_json(snapshot)
        except Exception:
            dead_dash.append(ws)
    for ws in dead_dash:
        DASHBOARD_CLIENTS.discard(ws)

    for job_id, sockets in list(CUSTOMER_CLIENTS.items()):
        job = JOBS.get(job_id)
        if not job:
            continue
        payload = {"type": "job_update", **_job_position_info(job)}
        dead_cust: list[WebSocket] = []
        for ws in list(sockets):
            try:
                await ws.send_json(payload)
            except Exception:
                dead_cust.append(ws)
        for ws in dead_cust:
            sockets.discard(ws)


async def _cleanup_loop() -> None:
    """Auto-expire unapproved pending jobs (15 min) and wipe completed temp files after grace period."""
    while True:
        await asyncio.sleep(30)
        cfg = load_config()
        pending_ttl = timedelta(minutes=int(cfg.get("pending_ttl_minutes", 15)))
        completed_grace = timedelta(minutes=int(cfg.get("completed_grace_minutes", 3)))
        now = datetime.now(timezone.utc)
        changed = False

        for job_id, job in list(JOBS.items()):
            if job.status == JobStatus.PENDING and (now - job.created_at) > pending_ttl:
                _delete_job_files(job)
                job.status = JobStatus.CANCELLED
                job.error_message = "Expired after 15 minutes"
                job.updated_at = now
                changed = True
            elif (
                job.status == JobStatus.COMPLETED
                and job.completed_at is not None
                and (now - job.completed_at) > completed_grace
            ):
                _delete_job_files(job)
                # Keep minimal record in Completed tab for the session, files wiped
                if job.normalized_path or job.chunk_paths:
                    job.normalized_path = None
                    job.chunk_paths = []
                    changed = True

        if changed:
            await broadcast_updates()


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_directories()
    task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="PrintShop Local Queue MVP", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def mobile_home() -> HTMLResponse:
    html = (STATIC_DIR / "mobile" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/admin", response_class=HTMLResponse)
async def dashboard_home() -> HTMLResponse:
    html = (STATIC_DIR / "dashboard" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/info")
async def get_shop_info() -> dict[str, Any]:
    cfg = load_config()
    rules = load_rules()
    lan_ip = get_lan_ip()
    port = int(cfg.get("port", 5050))
    mobile_url = f"http://{lan_ip}:{port}/"
    return {
        "shop_name": cfg["shop_name"],
        "onboarded": cfg.get("onboarded", False),
        "lan_ip": lan_ip,
        "port": port,
        "mobile_url": mobile_url,
        "pricing": cfg["pricing"],
        "rules_version": rules.get("version", "1.0.0"),
        "printers": [p.model_dump() for p in list_printers()],
        "mock_prints_dir": str(MOCK_PRINTS_DIR),
    }


@app.get("/api/qr")
async def get_qr_png() -> Response:
    cfg = load_config()
    lan_ip = get_lan_ip()
    port = int(cfg.get("port", 5050))
    url = f"http://{lan_ip}:{port}/"
    png_bytes = generate_qr_png_bytes(url)
    return Response(content=png_bytes, media_type="image/png")


@app.post("/api/onboarding")
async def save_onboarding(req: OnboardingRequest) -> dict[str, Any]:
    updated = save_config(
        {
            "shop_name": req.shop_name.strip() or "My Print Shop",
            "onboarded": True,
            "exposed_printers": req.exposed_printers,
            "pricing": {
                "bw_single": req.bw_single,
                "bw_duplex": req.bw_duplex,
                "color_single": req.color_single,
                "color_duplex": req.color_duplex,
                "currency": "₹",
            },
        }
    )
    await broadcast_updates()
    return {"ok": True, "config": updated}


@app.post("/api/probe-pages")
async def probe_pages(file: UploadFile = File(...)) -> dict[str, Any]:
    """Quick page count probe (especially useful for .docx where browser can't count pages)."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    ensure_directories()
    tmp_path = UPLOADS_DIR / f"_probe_{uuid.uuid4().hex}{ext}"
    try:
        with tmp_path.open("wb") as buf:
            shutil.copyfileobj(file.file, buf)
        pages = count_pages_quick(tmp_path)
        return {"filename": file.filename, "pages": pages}
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/jobs")
async def submit_job(
    file: UploadFile = File(...),
    color_mode: ColorMode = Form(ColorMode.BW),
    duplex_mode: DuplexMode = Form(DuplexMode.SINGLE),
    copies: int = Form(1),
    pages_spec: str = Form("all"),
) -> dict[str, Any]:
    """Customer uploads a file + print preferences; normalizes immediately and queues as Pending."""
    original_name = Path(file.filename or "document.pdf").name
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    ensure_directories()
    job_id = uuid.uuid4().hex[:12]
    token = _next_token()
    customer_secret = secrets.token_urlsafe(16)
    source_path = UPLOADS_DIR / f"{job_id}_{original_name}"

    with source_path.open("wb") as buf:
        shutil.copyfileobj(file.file, buf)

    try:
        norm_pdf, chunks, total_pages, selected_count, auto_fixes = normalize_and_chunk_job(
            job_id=job_id,
            source_path=source_path,
            selected_pages_spec=pages_spec,
            duplex_mode=duplex_mode,
        )
    except Exception as exc:
        source_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Could not process file: {exc}") from exc

    printers = list_printers()
    best_printer = pick_best_printer(printers, color_mode, duplex_mode)
    price, currency = calculate_price(selected_count, copies, color_mode, duplex_mode)

    job = PrintJob(
        id=job_id,
        token=token,
        customer_secret=customer_secret,
        filename=original_name,
        file_ext=ext,
        source_path=str(source_path),
        normalized_path=str(norm_pdf),
        chunk_paths=[str(p) for p in chunks],
        total_pages=total_pages,
        selected_pages_spec=pages_spec,
        selected_page_count=selected_count,
        color_mode=color_mode,
        duplex_mode=duplex_mode,
        copies=max(1, min(copies, 100)),
        estimated_price=price,
        currency=currency,
        status=JobStatus.PENDING,
        recommended_printer=best_printer,
        assigned_printer=best_printer,
        auto_fixes=auto_fixes,
    )
    JOBS[job_id] = job
    await broadcast_updates()

    return {
        "job_id": job_id,
        "token": token,
        "customer_secret": customer_secret,
        **_job_position_info(job),
    }


@app.get("/api/jobs/{job_id}")
async def get_customer_job(job_id: str, secret: str = Query(...)) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job or job.customer_secret != secret:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_position_info(job)


@app.get("/api/queue")
async def get_queue() -> dict[str, Any]:
    return _dashboard_snapshot()


@app.get("/api/spooler-health")
async def get_spooler_health() -> dict[str, Any]:
    """Live probe of the Windows Print Spooler (win32print)."""
    return await asyncio.to_thread(probe_windows_spooler)


@app.get("/api/rules")
async def get_rules() -> dict[str, Any]:
    return load_rules()


@app.post("/api/rules")
async def update_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """Hot-swap rules.json on disk and reload immediately without restarting."""
    ensure_directories()
    current = load_rules()
    current.update(payload)
    RULES_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    reloaded = load_rules()
    await broadcast_updates()
    return {"ok": True, "rules": reloaded}


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: str, req: ApproveJobRequest) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Job is already {job.status.value}")

    target_printer = req.printer_name or job.assigned_printer or job.recommended_printer
    if not target_printer:
        raise HTTPException(status_code=400, detail="No printer selected")

    job.assigned_printer = target_printer
    job.status = JobStatus.PRINTING
    job.updated_at = datetime.now(timezone.utc)
    await broadcast_updates()

    try:
        chunk_paths = [Path(p) for p in job.chunk_paths if Path(p).exists()]
        if not chunk_paths and job.normalized_path and Path(job.normalized_path).exists():
            chunk_paths = [Path(job.normalized_path)]
        if not chunk_paths:
            raise FileNotFoundError("Normalized print files are missing.")

        total_chunks = len(chunk_paths)
        saved_files: list[str] = []
        last_dir = ""
        for idx in range(job.completed_chunks, total_chunks):
            chunk_res = await asyncio.to_thread(
                execute_chunk,
                job,
                target_printer,
                chunk_paths[idx],
                idx + 1,
                total_chunks,
            )
            job.completed_chunks = idx + 1
            job.updated_at = datetime.now(timezone.utc)
            saved_files.append(chunk_res["file"])
            last_dir = chunk_res["output_dir"]
            await broadcast_updates()

        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = job.completed_at
        await broadcast_updates()
        return {
            "ok": True,
            "job": job.model_dump(mode="json"),
            "result": {"printer": target_printer, "output_dir": last_dir, "files": saved_files},
        }
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error_message = str(exc)
        job.updated_at = datetime.now(timezone.utc)
        await broadcast_updates()
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/reroute")
async def reroute_job(job_id: str, req: RerouteJobRequest) -> dict[str, Any]:
    """One-click reroute of a failed/stalled job to a backup printer."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.error_message = None
    job.status = JobStatus.PENDING
    job.assigned_printer = req.backup_printer
    return await approve_job(job_id, ApproveJobRequest(printer_name=req.backup_printer))


@app.post("/api/jobs/{job_id}/reject")
async def reject_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _delete_job_files(job)
    job.status = JobStatus.CANCELLED
    job.updated_at = datetime.now(timezone.utc)
    await broadcast_updates()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/wipe")
async def wipe_job_now(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _delete_job_files(job)
    job.normalized_path = None
    job.chunk_paths = []
    await broadcast_updates()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/preview")
async def preview_normalized_pdf(job_id: str) -> FileResponse:
    """Allow shopkeeper to preview the normalized PDF on their laptop."""
    job = JOBS.get(job_id)
    if not job or not job.normalized_path or not Path(job.normalized_path).exists():
        raise HTTPException(status_code=404, detail="Normalized file no longer available")
    return FileResponse(job.normalized_path, media_type="application/pdf", filename=f"Token_{job.token}.pdf")


@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket) -> None:
    await websocket.accept()
    DASHBOARD_CLIENTS.add(websocket)
    try:
        await websocket.send_json(_dashboard_snapshot())
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        DASHBOARD_CLIENTS.discard(websocket)


@app.websocket("/ws/customer/{job_id}")
async def ws_customer(websocket: WebSocket, job_id: str, secret: str = Query(...)) -> None:
    job = JOBS.get(job_id)
    if not job or job.customer_secret != secret:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    CUSTOMER_CLIENTS.setdefault(job_id, set()).add(websocket)
    try:
        await websocket.send_json({"type": "job_update", **_job_position_info(job)})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        CUSTOMER_CLIENTS.get(job_id, set()).discard(websocket)
