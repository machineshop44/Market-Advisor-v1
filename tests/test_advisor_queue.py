"""Desk Advisor proposal queue + 1.37.1 honesty helpers."""
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


def test_claim_then_complete(tmp_path, monkeypatch):
    qfile = tmp_path / "advisor_queue.json"
    monkeypatch.setattr(aq, "QUEUE_FILE", str(qfile))

    prop = aq.propose(
        broker="Robinhood",
        ticker="MSFT",
        asset_type="stock",
        price=400.0,
        dollars=40.0,
        score=80.0,
        engine="CORE",
    )
    pid = prop["id"]
    claimed = aq.claim(pid)
    assert claimed and claimed.get("status") == "executing"
    assert aq.list_pending() == []
    assert aq.claim(pid) is None  # second approve cannot double-fire

    missed = aq.complete(pid, ok=False)
    assert missed and missed.get("status") == "pending"
    assert len(aq.list_pending()) == 1

    aq.claim(pid)
    done = aq.complete(pid, ok=True)
    assert done and done.get("status") == "approved"
    assert aq.list_pending() == []


def test_reject_while_executing(tmp_path, monkeypatch):
    qfile = tmp_path / "advisor_queue.json"
    monkeypatch.setattr(aq, "QUEUE_FILE", str(qfile))
    prop = aq.propose(broker="Robinhood", ticker="NVDA", price=100.0, dollars=25.0, score=70.0)
    aq.claim(prop["id"])
    rejected = aq.reject(prop["id"])
    assert rejected and rejected.get("status") == "rejected"
    assert aq.complete(prop["id"], ok=True) is None


def test_posture_for_broker_override():
    from scoring import posture_for_broker

    settings = {
        "risk_posture": "balanced",
        "auto_scale_growth": False,
        "risk_posture_by_broker": {"Robinhood": "aggressive", "E*TRADE": "safer"},
    }
    assert posture_for_broker("Robinhood", settings) == "aggressive"
    assert posture_for_broker("E*TRADE", settings) == "safer"
    assert posture_for_broker("Coinbase", settings) == "balanced"


def test_posture_knobs_override_ignores_global_advanced():
    from scoring import posture_knobs_for_broker

    settings = {
        "risk_posture": "balanced",
        "auto_scale_growth": False,
        "risk_posture_by_broker": {"Robinhood": "aggressive"},
        "max_open_positions": 99,
        "day_dd_pause_pct": 0.03,
        "target_bp_utilization_pct": 70.0,
    }
    rh = posture_knobs_for_broker("Robinhood", settings)
    cb = posture_knobs_for_broker("Coinbase", settings)
    assert rh["max_open_positions"] == 5  # aggressive profile, no overlay
    assert rh["day_dd_pause_pct"] == 0.08
    assert rh["target_bp_utilization_pct"] == 95.0
    assert cb["max_open_positions"] == 99  # global Mode + Advanced overlay
    assert cb["day_dd_pause_pct"] == 0.03
    assert cb["target_bp_utilization_pct"] == 70.0


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


def test_overnight_scorecard_reauth_risks():
    from auto_cycle import overnight_scorecard

    oc = overnight_scorecard(
        protective_health={"missing_count": 0, "expected": 1, "ok": True},
        reauth_needed={"E*TRADE": True, "Robinhood": True, "Coinbase": True},
        et_equity_count=0,
        et_flatten_enabled=False,
        auto_armed=True,
        session_label="REGULAR",
    )
    joined = " ".join(oc["risks"])
    assert "E*TRADE reauth" in joined
    assert "Robinhood reauth" in joined
    assert "Coinbase reauth" in joined
    assert oc["score"] <= 55


def test_drawdown_uses_broker_override_not_global_overlay():
    import scoring

    prev = scoring._equity_dd.get("ROBINHOOD")
    scoring._equity_dd["ROBINHOOD"] = {
        "day": "", "day_open": 0.0, "peak": 0.0, "pause_until": 0.0, "pause_reason": ""
    }
    settings = {
        "risk_posture": "balanced",
        "risk_posture_by_broker": {"Robinhood": "aggressive"},
        "day_dd_pause_pct": 0.03,
    }
    try:
        scoring.update_equity_drawdown(
            "ROBINHOOD", 10000.0, posture="aggressive", settings=settings
        )
        paused, _ = scoring.update_equity_drawdown(
            "ROBINHOOD", 9600.0, posture="aggressive", settings=settings  # -4% < aggressive 8%
        )
        assert not paused
        paused2, msg = scoring.update_equity_drawdown(
            "ROBINHOOD", 9100.0, posture="aggressive", settings=settings  # -9% > 8%
        )
        assert paused2
        assert "Day drawdown" in msg
    finally:
        if prev is None:
            scoring._equity_dd.pop("ROBINHOOD", None)
        else:
            scoring._equity_dd["ROBINHOOD"] = prev
