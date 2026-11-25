# PrintShop Production Readiness Checklist

This checklist captures every operational, networking, Windows, and mobile requirement discovered during MVP design and live testing so nothing is missed before production release.

---

## 1. Windows Installer & Permissions (One-Time UAC)
- [ ] Package the Python backend + UI into a single executable (`PyInstaller` / `Nuitka`) wrapped in an **Inno Setup** (or NSIS/MSI) installer.
- [ ] Code-sign the installer and `.exe` (EV/OV cert or Azure Trusted Signing) to prevent Windows SmartScreen warnings.
- [ ] Add Windows Firewall rule during installation (one-time UAC):
  ```powershell
  netsh advfirewall firewall add rule name="PrintShop Local 5050" dir=in action=allow protocol=TCP localport=5050 profile=any remoteip=localsubnet
  ```
  *Why `profile=any`?* Windows marks most shop Wi-Fi networks as **Public** by default; restricting to `Private` will silently block customer phones.
- [ ] Register standard-user auto-start (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`) so daily runtime never asks for Administrator UAC.
- [ ] *(Phase 2 / Optional)* Add a `PrintHelperService` (Named Pipe) only if field testing shows `spoolsv.exe` freezes frequently.

---

## 2. Always-On Desktop Shell (Server Never Dies)
- [ ] Wrap the FastAPI server with **`pywebview`** (Edge WebView2) + **`pystray`** (system tray icon).
- [ ] Closing the dashboard window must **minimize to the system tray**—never stop the local web/Bluetooth server.
- [ ] Store runtime shop config, `rules.json`, temp uploads, and logs in `%LOCALAPPDATA%\PrintShop\` (never inside `Program Files` where standard users lack write access).

---

## 3. Networking, QR Resilience & Zero-Cloud Transfers
- [ ] **Router IP Change Protection:** Check the PC's LAN IP on every startup; if it changed since the counter QR was printed, show a prominent warning banner + live backup QR.
- [ ] **mDNS Support:** Broadcast `http://printshop.local:5050` (via `zeroconf`) as a router-IP-independent local address where supported.
- [ ] **One-Click Windows Mobile Hotspot:** Allow shops without a Wi-Fi router (or with router AP Isolation enabled) to turn their PC into a local hotspot directly from the dashboard.
- [ ] **Play Store Android App (Zero-Cloud):**
  - Auto-connect to shop Wi-Fi / PC Hotspot via `WifiNetworkSpecifier`.
  - Automatic **Bluetooth RFCOMM** fallback when Wi-Fi is unavailable (paired with a Windows `AF_BLUETOOTH` / WinRT RFCOMM listener in the PC app).
  - Keep the **browser web UI** as a zero-install fallback for iPhones and walk-in customers.
  - Integrate **Google AdMob** strictly on the live token waiting screen (never blocking file selection).

---

## 4. Document Normalization & Real Printer Verification
- [ ] **`.docx` Fidelity:** Auto-detect Microsoft Word (`win32com` with timeout) or LibreOffice (`soffice --headless --convert-to pdf`); offer a 1-click LibreOffice Portable download during first-run onboarding if neither is installed (keeping the base installer small).
- [ ] **Real Printer Hardware Test:** Before public launch, test `app/printer_service.py` (`pypdfium2` + Windows GDI + `DEVMODE`) against physical USB/LAN printers (HP, Canon, Epson) for:
  - B&W vs. Color `DEVMODE` enforcement
  - 1-Sided vs. 2-Sided (`DMDUP_VERTICAL`) + odd-page trailing blank page
  - 50-page chunking & mid-job `Paper Jam` / `Offline` detection via `win32print.EnumJobs` + stall timeout
  - 1-click **Reroute Remaining** to a backup printer

---

## 5. Two-Layer OTA Updates (Zero-Cost Static Hosting)
- [ ] Host signed `rules.json` (< 50 KB) and core app installer updates on **GitHub Releases** (free static hosting).
- [ ] Verify `rules.json` with an **Ed25519** signature before hot-reloading into `%LOCALAPPDATA%`.
- [ ] Download core binary updates silently in the background and apply **only on next app launch** (never mid-rush).

---

## 6. Privacy & Queue Hygiene
- [ ] Bind customer WebSocket/status views to per-upload `customer_secret` tokens.
- [ ] Auto-expire unapproved pending jobs and wipe temp files after **15 minutes**.
- [ ] Keep completed job files for a short grace period (**3 minutes** for immediate jam reprints) before auto-wiping, plus a manual **Wipe Now** button.
