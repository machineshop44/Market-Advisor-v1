# Market Advisor v1

Multi-broker quantitative trading desktop app (Robinhood + Coinbase Advanced) built with PyQt5.

## Setup

1. Install Python 3.12+
2. From this folder:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `Src/settings.example.json` → `Src/settings.json` and fill in your credentials locally (never commit `settings.json`).
4. Run:
   ```bash
   cd Src
   py main.py
   ```

## Web monitor (read-only)

While the desktop app is running, open `http://127.0.0.1:8791/` for live balances, auto-trader status, recent trades, and the activity log. Toggle/port/optional Basic Auth are under Settings. Port **8791** is chosen to avoid Arrs-Hub defaults (Readarr uses 8787). The monitor has no trade controls.

## Safety

- Keep this repo **private**
- `settings.json`, trade journals, and activity logs are gitignored on purpose
