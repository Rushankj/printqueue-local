"""Single-command launcher for PrintShop Local Queue MVP."""

from __future__ import annotations

import threading
import time
import webbrowser

import uvicorn

from app.config import ensure_directories, load_config
from app.qr_utils import get_lan_ip


def _open_dashboard_soon(port: int) -> None:
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:{port}/admin")


def main() -> None:
    ensure_directories()
    cfg = load_config()
    port = int(cfg.get("port", 5050))
    lan_ip = get_lan_ip()

    print("=" * 60)
    print(f"  {cfg['shop_name']} - PrintShop Local Queue MVP")
    print("=" * 60)
    print(f"  Shopkeeper Dashboard : http://127.0.0.1:{port}/admin")
    print(f"  Customer Mobile URL  : http://{lan_ip}:{port}/")
    print(f"  Virtual Print Output : D:\\zindi - work\\data\\mock_prints")
    print("=" * 60)

    threading.Thread(target=_open_dashboard_soon, args=(port,), daemon=True).start()
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
