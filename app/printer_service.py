"""Windows printer discovery (win32print), spooler probe, and Virtual/Fault-Injection Simulators."""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from PIL import ImageWin

from app.config import MOCK_PRINTS_DIR, load_config
from app.models import ColorMode, DuplexMode, PrintJob, PrinterInfo
from app.rules_engine import load_rules

try:
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32print  # type: ignore
    import win32ui  # type: ignore
except ImportError:
    win32con = None
    win32gui = None
    win32print = None
    win32ui = None

VIRTUAL_PRINTER_NAME = "Virtual Print Simulator (PDF)"
VIRTUAL_BACKUP_NAME = "Virtual Backup Laser (B&W)"
VIRTUAL_FAULTY_NAME = "Virtual Faulty Printer (Jam Test)"

VIRTUAL_PRINTERS = {VIRTUAL_PRINTER_NAME, VIRTUAL_BACKUP_NAME, VIRTUAL_FAULTY_NAME}


class SpoolerFaultError(RuntimeError):
    """Raised when a printer reports Paper Jam, Offline, or stalls mid-job."""


def list_printers() -> list[PrinterInfo]:
    """Enumerate Windows printers via win32print plus Virtual Simulators for printerless testing."""
    cfg = load_config()
    rules = load_rules()
    quirks = rules.get("driver_quirks", {})
    hidden = set(cfg.get("hidden_printers", []))
    exposed_cfg = cfg.get("exposed_printers", [])

    printers: list[PrinterInfo] = []

    sim_defs = [
        (VIRTUAL_PRINTER_NAME, "Ready (Saves PDFs to data/mock_prints)", True, True),
        (VIRTUAL_BACKUP_NAME, "Ready (Backup B&W Virtual Printer)", False, True),
        (VIRTUAL_FAULTY_NAME, "Ready (Simulates Paper Jam / Reroute Test)", True, True),
    ]
    for sim_name, sim_status, default_color, default_duplex in sim_defs:
        quirk = quirks.get(sim_name, {})
        printers.append(
            PrinterInfo(
                name=sim_name,
                status=sim_status,
                is_online=True,
                is_virtual=True,
                supports_color=bool(quirk.get("supports_color", default_color)),
                supports_duplex=bool(quirk.get("supports_duplex", default_duplex)),
                queued_jobs=0,
                is_exposed=(not exposed_cfg) or (sim_name in exposed_cfg),
            )
        )

    if win32print is not None:
        try:
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            raw_printers = win32print.EnumPrinters(flags, None, 2)
            for info in raw_printers:
                name = info.get("pPrinterName", "")
                if not name or name in hidden:
                    continue
                status_code = int(info.get("Status", 0))
                is_offline = bool(status_code & getattr(win32print, "PRINTER_STATUS_OFFLINE", 0x80))
                is_error = bool(status_code & getattr(win32print, "PRINTER_STATUS_ERROR", 0x02))
                is_jam = bool(status_code & getattr(win32print, "PRINTER_STATUS_PAPER_JAM", 0x08))
                queued = int(info.get("cJobs", 0))

                status_text = "Ready"
                if is_jam:
                    status_text = "Paper Jam"
                elif is_offline:
                    status_text = "Offline"
                elif is_error:
                    status_text = "Error"

                p_quirk = quirks.get(name, {})
                is_exposed = (not exposed_cfg) or (name in exposed_cfg)

                printers.append(
                    PrinterInfo(
                        name=name,
                        status=status_text,
                        is_online=not (is_offline or is_error or is_jam),
                        is_virtual=False,
                        supports_color=bool(p_quirk.get("supports_color", True)),
                        supports_duplex=bool(p_quirk.get("supports_duplex", False)),
                        queued_jobs=queued,
                        is_exposed=is_exposed,
                    )
                )
        except Exception:
            pass

    return printers


def probe_windows_spooler() -> dict[str, Any]:
    """Inspect real Windows Print Spooler (spoolsv) via win32print.OpenPrinter & EnumJobs."""
    if win32print is None:
        return {"available": False, "reason": "pywin32 not installed", "printers": []}

    details: list[dict[str, Any]] = []
    try:
        default_printer = win32print.GetDefaultPrinter()
    except Exception:
        default_printer = None

    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for info in win32print.EnumPrinters(flags, None, 2):
            name = info.get("pPrinterName", "")
            if not name:
                continue
            handle = None
            active_jobs = 0
            port = info.get("pPortName", "")
            driver = info.get("pDriverName", "")
            try:
                handle = win32print.OpenPrinter(name)
                pinfo = win32print.GetPrinter(handle, 2)
                jobs = win32print.EnumJobs(handle, 0, 20, 1)
                active_jobs = len(jobs)
                port = pinfo.get("pPortName", port)
                driver = pinfo.get("pDriverName", driver)
            except Exception as exc:
                details.append({"name": name, "error": str(exc)})
                continue
            finally:
                if handle is not None:
                    try:
                        win32print.ClosePrinter(handle)
                    except Exception:
                        pass

            details.append(
                {
                    "name": name,
                    "driver": driver,
                    "port": port,
                    "status_code": int(info.get("Status", 0)),
                    "active_spooler_jobs": active_jobs,
                    "is_default": name == default_printer,
                }
            )
        return {"available": True, "default_printer": default_printer, "printers": details}
    except Exception as exc:
        return {"available": False, "reason": str(exc), "printers": details}


def _build_devmode(printer_name: str, job: PrintJob) -> Any:
    """Build a Windows DEVMODE with A4, Color/Mono, Duplex, and Copies applied."""
    handle = win32print.OpenPrinter(printer_name)
    try:
        pinfo = win32print.GetPrinter(handle, 2)
        status_code = int(pinfo.get("Status", 0))
        if status_code & getattr(win32print, "PRINTER_STATUS_PAPER_JAM", 0x08):
            raise SpoolerFaultError(f"{printer_name} reports Paper Jam.")
        if status_code & getattr(win32print, "PRINTER_STATUS_OFFLINE", 0x80):
            raise SpoolerFaultError(f"{printer_name} is Offline.")
        if status_code & getattr(win32print, "PRINTER_STATUS_ERROR", 0x02):
            raise SpoolerFaultError(f"{printer_name} is in an Error state.")

        devmode = pinfo.get("pDevMode")
        if devmode is None:
            return None

        devmode.Fields |= win32con.DM_PAPERSIZE | win32con.DM_COLOR | win32con.DM_COPIES
        devmode.PaperSize = win32con.DMPAPER_A4
        devmode.Color = (
            win32con.DMCOLOR_COLOR
            if job.color_mode == ColorMode.COLOR
            else win32con.DMCOLOR_MONOCHROME
        )
        devmode.Copies = max(1, int(job.copies))

        if job.duplex_mode == DuplexMode.DUPLEX:
            devmode.Fields |= win32con.DM_DUPLEX
            devmode.Duplex = win32con.DMDUP_VERTICAL
        else:
            devmode.Fields |= win32con.DM_DUPLEX
            devmode.Duplex = win32con.DMDUP_SIMPLEX

        win32print.DocumentProperties(0, handle, printer_name, devmode, devmode, win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER)
        return devmode
    finally:
        win32print.ClosePrinter(handle)


def _print_pdf_windows_gdi(job: PrintJob, printer_name: str, chunk_path: Path, chunk_index: int) -> None:
    """Render PDF pages via PDFium and spool directly to a Windows printer DC using DEVMODE."""
    if win32print is None or win32ui is None or win32gui is None or win32con is None:
        raise SpoolerFaultError("pywin32 Windows GDI printing is not available on this system.")

    devmode = _build_devmode(printer_name, job)
    hdc = win32gui.CreateDC("WINSPOOL", printer_name, devmode)
    if not hdc:
        raise SpoolerFaultError(f"Could not open Windows GDI device context for {printer_name}.")

    dc = win32ui.CreateDCFromHandle(hdc)
    pdf = pdfium.PdfDocument(str(chunk_path))
    try:
        printable_w = dc.GetDeviceCaps(win32con.HORZRES)
        printable_h = dc.GetDeviceCaps(win32con.VERTRES)
        dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX) or 300
        render_scale = min(max(dpi_x / 72.0, 2.0), 4.2)  # ~150-300 DPI crisp render

        dc.StartDoc(f"PrintShop_Token_{job.token}_Chunk_{chunk_index}")
        try:
            for page_idx in range(len(pdf)):
                page = pdf[page_idx]
                bitmap = page.render(scale=render_scale)
                pil_img = bitmap.to_pil().convert("RGB")
                img_w, img_h = pil_img.size

                scale = min(printable_w / img_w, printable_h / img_h)
                draw_w = int(img_w * scale)
                draw_h = int(img_h * scale)
                x0 = (printable_w - draw_w) // 2
                y0 = (printable_h - draw_h) // 2

                dc.StartPage()
                dib = ImageWin.Dib(pil_img)
                dib.draw(dc.GetHandleOutput(), (x0, y0, x0 + draw_w, y0 + draw_h))
                dc.EndPage()
            dc.EndDoc()
        except Exception as exc:
            try:
                dc.AbortDoc()
            except Exception:
                pass
            raise SpoolerFaultError(f"Windows GDI spooling failed on {printer_name}: {exc}") from exc
    finally:
        pdf.close()
        dc.DeleteDC()

    # Brief spooler health check via win32print.EnumJobs
    time.sleep(0.3)
    handle = win32print.OpenPrinter(printer_name)
    try:
        for sp_job in win32print.EnumJobs(handle, 0, 50, 1):
            st = int(sp_job.get("Status", 0))
            if st & getattr(win32print, "JOB_STATUS_ERROR", 0x02):
                raise SpoolerFaultError(f"Spooler reported job error on {printer_name}.")
            if st & getattr(win32print, "JOB_STATUS_PAPEROUT", 0x40):
                raise SpoolerFaultError(f"{printer_name} is out of paper.")
            if st & getattr(win32print, "JOB_STATUS_OFFLINE", 0x20):
                raise SpoolerFaultError(f"{printer_name} went offline during spooling.")
    finally:
        win32print.ClosePrinter(handle)


def execute_chunk(
    job: PrintJob,
    printer_name: str,
    chunk_path: Path,
    chunk_index: int,
    total_chunks: int,
) -> dict[str, Any]:
    """
    Spool a single chunk to the target printer (or virtual simulator).
    Simulates a mid-job Paper Jam on Chunk 2 (or Offline on 1-chunk jobs) for VIRTUAL_FAULTY_NAME.
    """
    if printer_name == VIRTUAL_FAULTY_NAME:
        if total_chunks > 1 and chunk_index >= 2:
            raise SpoolerFaultError(
                f"Paper Jam detected on {printer_name} at Chunk {chunk_index}/{total_chunks}. "
                "Queue paused — reroute remaining chunks to a backup printer."
            )
        if total_chunks == 1:
            raise SpoolerFaultError(
                f"Printer {printer_name} went Offline before spooling. "
                "Reroute this job to a backup printer."
            )

    MOCK_PRINTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_printer = "".join(c if c.isalnum() or c in "-_" else "_" for c in printer_name)[:28]
    job_out_dir = MOCK_PRINTS_DIR / f"Token_{job.token}_{safe_printer}"
    job_out_dir.mkdir(parents=True, exist_ok=True)

    out_file = job_out_dir / f"chunk_{chunk_index:02d}_of_{total_chunks:02d}_{job.filename}.pdf"
    shutil.copyfile(chunk_path, out_file)

    receipt_path = job_out_dir / "receipt.json"
    receipt = {
        "token": job.token,
        "printer": printer_name,
        "filename": job.filename,
        "copies": job.copies,
        "color_mode": job.color_mode.value,
        "duplex_mode": job.duplex_mode.value,
        "selected_pages": job.selected_pages_spec,
        "auto_fixes": job.auto_fixes,
        "last_chunk_printed": chunk_index,
        "total_chunks": total_chunks,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    # Real Windows printer spooling via bundled PDFium + Windows GDI + DEVMODE (never silently skipped)
    if printer_name not in VIRTUAL_PRINTERS:
        _print_pdf_windows_gdi(job, printer_name, chunk_path, chunk_index)

    return {"file": str(out_file), "output_dir": str(job_out_dir)}
