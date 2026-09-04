"""Journal read_since_days tail + cache behavior."""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
sys.path.insert(0, str(SRC))

import journal as journal_mod


def test_read_since_days_tail_and_cache(tmp_path, monkeypatch):
    path = tmp_path / "trade_journal.jsonl"
    monkeypatch.setattr(journal_mod, "JOURNAL_FILE", str(path))
    journal_mod._since_cache.clear()

    now = datetime.now()
    rows_in = []
    for i in range(30):
        ts = (now - timedelta(days=i % 5)).isoformat(timespec="seconds")
        rows_in.append({"timestamp": ts, "side": "BUY", "ticker": f"T{i}", "broker": "Robinhood"})
    with open(path, "w", encoding="utf-8") as f:
        for r in rows_in:
            f.write(json.dumps(r) + "\n")

    a = journal_mod.read_since_days(days=7, limit=50)
    assert len(a) > 0
    b = journal_mod.read_since_days(days=7, limit=50)
    assert a == b  # cache hit
    assert len(journal_mod._since_cache) >= 1

    # Append invalidates cache via log_trade
    journal_mod.log_trade({"side": "SELL", "ticker": "ZZ", "broker": "Robinhood"})
    assert len(journal_mod._since_cache) == 0
    c = journal_mod.read_since_days(days=7, limit=50)
    assert any(str(r.get("ticker")) == "ZZ" for r in c)


def test_read_decisions_since_days_exists(tmp_path, monkeypatch):
    path = tmp_path / "decision_journal.jsonl"
    monkeypatch.setattr(journal_mod, "DECISION_FILE", str(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": "SKIP",
            "ticker": "AAA",
        }) + "\n")
    rows = journal_mod.read_decisions_since_days(days=7, limit=100)
    assert len(rows) == 1
