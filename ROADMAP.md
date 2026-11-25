# PrintShop Local Queue — Roadmap

This document tracks completed MVP milestones and upcoming engineering work toward production deployment.

---

## Phase 1: Core Queue, Normalizer & Windows GDI Engine (Completed)
- [x] FastAPI + WebSocket backend (`app/main.py`) with 3-tab queue (`Pending`, `Printing`, `Completed`)
- [x] Zero-install Customer Mobile Web UI with live 3-digit token and queue position
- [x] Deterministic normalizer (`app/normalizer.py`) for `.pdf`, `.jpg`, `.png`, `.docx` → A4 PDF
- [x] Odd-page duplex blank padding and 50-page safe chunk splitting
- [x] `pypdfium2` + Windows GDI (`DEVMODE`) real spooler integration (`app/printer_service.py`)
- [x] Chunk-level mid-job fault recovery (`Virtual Faulty Printer` + 1-click **Reroute Remaining**)
- [x] Hot-reloadable `rules.json` and 15-minute pending / 3-minute completed file cleanup
- [x] Automated `pytest` end-to-end suite (`tests/test_mvp.py`)

---

## Phase 2: Windows Desktop Shell, Installer & Hardening (Next)
- [ ] Wrap server with `pywebview` (Edge WebView2) + `pystray` system tray icon (minimize to tray without stopping server)
- [ ] Package standalone `.exe` + Inno Setup installer with one-time UAC for `LocalSubnet` (`profile=any`) Windows Firewall rule and `HKCU` auto-start
- [ ] Store runtime config/data in `%LOCALAPPDATA%\PrintShop\` for packaged builds
- [ ] Add mDNS (`printshop.local` via `zeroconf`) and startup LAN IP-change detection alerts
- [ ] Add Ed25519 signature verification for `rules.json` OTA updates + GitHub Releases silent binary update check

---

## Phase 3: Zero-Cloud Mobile App & Offline Transports
- [ ] Windows Bluetooth RFCOMM (`AF_BLUETOOTH` / WinRT) background receiver for offline transfers
- [ ] One-click Windows Mobile Hotspot toggle for shops without a Wi-Fi router or with AP Isolation
- [ ] Play Store Android companion app (Flutter/Kotlin) with QR scanner, auto-Wi-Fi/Hotspot join, Bluetooth RFCOMM fallback, and non-blocking AdMob slot on the live token screen

---

## Phase 4: Frontend Refactor & Multi-Vendor Printer Profiles
- [ ] Modularize Shopkeeper Dashboard and Mobile UI into clean, testable frontend components
- [ ] Expand `rules.json` `driver_quirks` profiles across common HP, Canon, Epson, and Brother USB/LAN printers
- [ ] Add GitHub Actions CI (`windows-latest`) running `pytest` and `ruff`
