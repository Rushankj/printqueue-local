"""Application configuration and workspace-local storage paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Keep all dev/runtime data strictly inside the workspace folder (D:\zindi - work\data)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PRINTSHOP_DATA_DIR", str(BASE_DIR / "data")))
UPLOADS_DIR = DATA_DIR / "uploads"
NORMALIZED_DIR = DATA_DIR / "normalized"
MOCK_PRINTS_DIR = DATA_DIR / "mock_prints"
CONFIG_PATH = DATA_DIR / "config.json"
RULES_PATH = DATA_DIR / "rules.json"

DEFAULT_PORT = int(os.environ.get("PRINTSHOP_PORT", "5050"))

DEFAULT_CONFIG: dict[str, Any] = {
    "shop_name": "Campus Print Hub",
    "onboarded": False,
    "port": DEFAULT_PORT,
    "exposed_printers": [],  # Empty means expose all non-hidden printers
    "hidden_printers": ["Fax", "OneNote (Desktop)", "Microsoft XPS Document Writer"],
    "pricing": {
        "bw_single": 2.0,
        "bw_duplex": 1.5,
        "color_single": 10.0,
        "color_duplex": 9.0,
        "currency": "₹",
    },
    "pending_ttl_minutes": 15,
    "completed_grace_minutes": 3,
}

DEFAULT_RULES: dict[str, Any] = {
    "version": "1.0.0",
    "chunk_threshold_pages": 50,
    "a4_width_pt": 595.28,
    "a4_height_pt": 841.89,
    "stall_timeout_seconds": 40,
    "printer_scoring": {
        "color_match_bonus": 50,
        "duplex_match_bonus": 25,
        "online_bonus": 30,
        "virtual_fallback_penalty": -10,
        "queue_depth_penalty": 5,
    },
    "driver_quirks": {
        "Virtual Print Simulator (PDF)": {
            "supports_color": True,
            "supports_duplex": True,
            "type": "virtual",
        },
        "Microsoft Print to PDF": {
            "supports_color": True,
            "supports_duplex": False,
            "type": "gdi_pdf",
        },
    },
}


def ensure_directories() -> None:
    """Create all required workspace-local directories and default config/rules files."""
    for directory in (DATA_DIR, UPLOADS_DIR, NORMALIZED_DIR, MOCK_PRINTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")

    if not RULES_PATH.exists():
        RULES_PATH.write_text(json.dumps(DEFAULT_RULES, indent=2), encoding="utf-8")


def load_config() -> dict[str, Any]:
    ensure_directories()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = {**DEFAULT_CONFIG, **data}
        merged["pricing"] = {**DEFAULT_CONFIG["pricing"], **data.get("pricing", {})}
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(new_config: dict[str, Any]) -> dict[str, Any]:
    ensure_directories()
    current = load_config()
    current.update(new_config)
    CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current
