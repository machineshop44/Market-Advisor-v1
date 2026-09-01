"""Advisor decision journal history."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import advisor_queue as aq


def test_record_and_list_decisions(tmp_path, monkeypatch):
    path = tmp_path / "advisor_decisions.jsonl"
    monkeypatch.setattr(aq, "DECISIONS_FILE", str(path))
    aq.record_decision(
        proposal_id="abc",
        broker="Coinbase",
        ticker="BONK",
        verdict="approve",
        action="hold_rails",
        source="local",
        brief="rails blocked",
        detail="disconnected typo",
        dollars=22.2,
        score=107,
        engine="CRYPTO",
        status="pending",
    )
    aq.record_decision(
        proposal_id="abc",
        broker="Coinbase",
        ticker="BONK",
        verdict="approve",
        action="auto_apply",
        source="local",
        brief="go",
        dollars=22.2,
        score=107,
        engine="CRYPTO",
        status="executing",
    )
    rows = aq.list_decisions(limit=20)
    actions = {r["action"] for r in rows}
    assert "auto_apply" in actions
    assert "hold_rails" in actions
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["ticker"] == "BONK"
