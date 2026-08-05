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

   Optional full freeze (standalone `dist\MarketAdvisor\MarketAdvisor.exe`, slower rebuild):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Build-MarketAdvisor-PyInstaller.ps1
   ```

## Web monitor (read-only)

While the desktop app is running, open `http://127.0.0.1:8791/` for live balances, auto-trader status, recent trades, and the activity log. Toggle/port/optional Basic Auth are under Settings. Port **8791** is chosen to avoid Arrs-Hub defaults (Readarr uses 8787). The monitor has no trade controls.

## Safety

- Keep this repo **private**
- `settings.json`, trade journals, and activity logs are gitignored on purpose
