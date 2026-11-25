"""Deterministic rule playbook loader, printer scorer, and price calculator."""

from __future__ import annotations

import json
from typing import Any

from app.config import DEFAULT_RULES, RULES_PATH, ensure_directories, load_config
from app.models import ColorMode, DuplexMode, PrinterInfo


_RULES_CACHE: dict[str, Any] | None = None
_RULES_MTIME: float = 0.0


def load_rules() -> dict[str, Any]:
    """Hot-reload rules.json automatically whenever its file mtime changes (zero restart)."""
    global _RULES_CACHE, _RULES_MTIME
    ensure_directories()
    try:
        stat = RULES_PATH.stat()
        if _RULES_CACHE is not None and stat.st_mtime == _RULES_MTIME:
            return _RULES_CACHE
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        _RULES_CACHE = {**DEFAULT_RULES, **data}
        _RULES_MTIME = stat.st_mtime
        return _RULES_CACHE
    except Exception:
        return dict(DEFAULT_RULES)


def score_printer(
    printer: PrinterInfo,
    color_mode: ColorMode,
    duplex_mode: DuplexMode,
    rules: dict[str, Any] | None = None,
) -> int:
    """Score a printer deterministically based on rules.json weights."""
    rules = rules or load_rules()
    weights = rules.get("printer_scoring", {})
    score = 0

    if printer.is_online:
        score += weights.get("online_bonus", 30)
    else:
        score -= 100

    if color_mode == ColorMode.COLOR and printer.supports_color:
        score += weights.get("color_match_bonus", 50)
    elif color_mode == ColorMode.COLOR and not printer.supports_color:
        score -= 80

    if duplex_mode == DuplexMode.DUPLEX and printer.supports_duplex:
        score += weights.get("duplex_match_bonus", 25)

    if printer.is_virtual:
        score += weights.get("virtual_fallback_penalty", -10)

    score -= printer.queued_jobs * weights.get("queue_depth_penalty", 5)
    return score


def pick_best_printer(
    printers: list[PrinterInfo],
    color_mode: ColorMode,
    duplex_mode: DuplexMode,
) -> str | None:
    """Return the highest-scoring exposed printer name."""
    exposed = [p for p in printers if p.is_exposed]
    if not exposed:
        exposed = printers
    if not exposed:
        return None

    rules = load_rules()
    best = max(exposed, key=lambda p: score_printer(p, color_mode, duplex_mode, rules))
    return best.name


def calculate_price(
    selected_pages: int,
    copies: int,
    color_mode: ColorMode,
    duplex_mode: DuplexMode,
) -> tuple[float, str]:
    """Calculate estimated print price from shop config."""
    cfg = load_config()
    pricing = cfg.get("pricing", {})
    currency = str(pricing.get("currency", "₹"))

    if color_mode == ColorMode.COLOR:
        rate = float(
            pricing.get("color_duplex" if duplex_mode == DuplexMode.DUPLEX else "color_single", 10.0)
        )
    else:
        rate = float(
            pricing.get("bw_duplex" if duplex_mode == DuplexMode.DUPLEX else "bw_single", 2.0)
        )

    total = round(selected_pages * max(copies, 1) * rate, 2)
    return total, currency
