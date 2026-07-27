"""Persistent trade journal — append-only JSONL + recent-trade helpers."""
import os
import json
from datetime import datetime

JOURNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_journal.jsonl")


def log_trade(entry):
    """
    Append one trade event.
    Expected keys: timestamp, broker, side, ticker, price, qty/dollars, status,
    reason, fee_profile, paper (bool), order_id (optional), confirmed (optional)
    """
    row = dict(entry)
    row.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        print(f"Journal write error: {e}")
    return row


def read_recent(limit=20):
    """Return the most recent trade events (newest last)."""
    if not os.path.exists(JOURNAL_FILE):
        return []
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        rows = []
        for ln in lines[-max(limit, 1):]:
            try:
                rows.append(json.loads(ln))
            except Exception:
                continue
        return rows
    except Exception:
        return []


def summarize_day(date_str=None):
    """Quick counts for today (or given YYYY-MM-DD)."""
    if date_str is None:
        date_str = datetime.now().date().isoformat()
    buys = sells = fails = 0
    for row in read_recent(500):
        ts = str(row.get("timestamp", ""))
        if not ts.startswith(date_str):
            continue
        side = str(row.get("side", "")).upper()
        status = str(row.get("status", ""))
        if "Fail" in status or "Skipped" in status:
            fails += 1
        elif side == "BUY":
            buys += 1
        elif side == "SELL":
            sells += 1
    return {"buys": buys, "sells": sells, "fails": fails}
