"""Desk Advisor proposal queue."""
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import advisor_queue as aq


def test_propose_and_approve(tmp_path, monkeypatch):
    qfile = tmp_path / "advisor_queue.json"
    monkeypatch.setattr(aq, "QUEUE_FILE", str(qfile))

    prop = aq.propose(
        broker="Robinhood",
        ticker="AAPL",
        asset_type="stock",
        price=100.0,
        dollars=25.0,
        score=72.0,
        engine="CORE",
    )
    assert prop and prop.get("id")
    pending = aq.list_pending()
    assert len(pending) == 1
    assert pending[0]["ticker"] == "AAPL"

    approved = aq.approve(prop["id"])
    assert approved and approved.get("status") == "approved"
    assert aq.list_pending() == []


def test_posture_for_broker_override():
    from scoring import posture_for_broker

    settings = {
        "risk_posture": "balanced",
        "risk_posture_by_broker": {"Robinhood": "aggressive", "E*TRADE": "safer"},
    }
    assert posture_for_broker("Robinhood", settings) == "aggressive"
    assert posture_for_broker("E*TRADE", settings) == "safer"
    assert posture_for_broker("Coinbase", settings) == "balanced"


def test_overnight_scorecard():
    from auto_cycle import overnight_scorecard

    oc = overnight_scorecard(
        protective_health={"missing_count": 1, "expected": 2, "ok": False},
        et_equity_count=2,
        et_flatten_enabled=False,
        auto_armed=True,
        session_label="REGULAR",
    )
    assert oc["grade"] in ("A", "B", "C", "D")
    assert oc["et_naked"] == 2
    assert any("ET naked" in r for r in oc["risks"])
