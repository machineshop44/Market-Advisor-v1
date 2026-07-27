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

## Safety

- Keep this repo **private**
- `settings.json`, trade journals, and activity logs are gitignored on purpose
