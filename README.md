# Market Advisor v1

Multi-broker quantitative trading desktop app (Robinhood + Coinbase Advanced) built with PyQt5.

## Setup

1. Install Python 3.12+
2. From this folder:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `Src/settings.example.json` → `Src/settings.json` and fill in your credentials locally (never commit `settings.json`).
4. Run (preferred on Windows — branded icon + Task Manager name):
   ```powershell
   # Once after install / Python upgrade — copies pythonw → MarketAdvisor.exe
   powershell -ExecutionPolicy Bypass -File .\Build-MarketAdvisor-Launcher.ps1
   # Then use Desktop "Market Advisor" shortcut, or:
   .\Start Market Advisor.lnk
   ```
   Debug / console mode: `cd Src` then `py main.py` (shows as `python.exe` in Task Manager).

   **Task Manager:** Details lists `MarketAdvisor.exe` when launched via the launcher shortcuts.
   Processes (Apps) groups under **Market Advisor** (window title + AppUserModelID).

   Optional full freeze + Drive publish (portable zip + Start Menu installer):
   ```powershell
   # Plex-ready (includes settings / Restore-Sessions) + Inno Start Menu installer
   powershell -ExecutionPolicy Bypass -File .\publish-exe-to-drive.ps1 -IncludeSettings
   # Or freeze only:
   powershell -ExecutionPolicy Bypass -File .\Build-MarketAdvisor-PyInstaller.ps1
   ```
   Artifacts land in `G:\My Drive\exe\` as `MarketAdvisor-<ver>-portable.zip`,
   `MarketAdvisor-<ver>-x64.exe` (Inno), and SHA256 checksums. See `packaging\INSTALL-PLEX.txt`.
   Lightweight install from zip (no Inno): `MarketAdvisor-Setup.ps1`.

## Web monitor & Android companion

While the desktop app is running, open the monitor URL for live balances, auto-trader status, recent trades, and the activity log. Settings cover bind mode, port, User/Pass, HTTPS, and companion arm/disarm. Port **8791** avoids Arrs-Hub defaults (Readarr uses 8787).

**This PC only:** `http://127.0.0.1:8791/` (default).

**Home Wi‑Fi + away (no Tailscale):**
1. Settings → Bind: **Home Wi‑Fi + away (all interfaces)**
2. Set a strong **User** and **Pass** (required)
3. Leave **HTTPS** on (forced for remote bind) — this **TLS-encrypts** the login so passwords are not sent in cleartext
4. Allow the port in Windows Firewall
5. Same Wi‑Fi: `https://<pc-lan-ip>:8791/`
6. Away: port-forward that port on your router to the PC, then `https://<public-ip-or-ddns>:8791/`
7. Settings → **Companion…** → **Apply & restart monitor**, then **Show setup QR**, and scan it in the Android companion (fills URL, user, pass, TLS fingerprint). Or paste those fields manually.
8. Optional: enable **Companion Controls** in that same dialog (per-broker arm/disarm only — never places trades)

Failed logins lock out after **5** attempts (~15 minutes). Use **Clear lockouts** in the Companion dialog if needed. There are **no** buy/sell endpoints.

## Risk posture & opportunity-swap

**Safer / Balanced / Aggressive** is the primary Settings control (fine-tunes under **Show Advanced…**). Discord (webhook + alerts) stays on the main Settings page. Top bar **HALT** disarms all brokers; Home shows E\*TRADE reauth banner, portfolio heat, **cluster heat**, protective-stop chip, and DD-pause vs $-loss chips. Skips/buys append to `decision_journal.jsonl`. **Journal → Reports** shows fill economics plus decision skip/buy rates and a lite posture replay (max_open clearance — not a full bar backtest). Companion shows reauth/DD and can **HALT ALL** when controls are on.

## Safety

- Keep this repo **private**
- `settings.json`, trade journals, and activity logs are gitignored on purpose
