"""Local LAN IP discovery and QR code PNG generator."""

from __future__ import annotations

import io
import socket

import qrcode


def get_lan_ip() -> str:
    """Discover the primary local LAN IPv4 address without making external requests."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        return ip
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def generate_qr_png_bytes(url: str) -> bytes:
    """Generate a crisp PNG QR code for the given URL."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#111827", back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
