"""Micro full-deploy sizing + fundable rank preference."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scoring import (
    MICRO_FULL_DEPLOY_EQUITY,
    micro_full_deploy_overrides,
    posture_knobs_for_broker,
    risk_sizing_breakdown,
)
from auto_cycle import prefer_fundable_buy_candidates
from desk_advisor_ai import local_analyze_proposal, VERDICT_APPROVE, VERDICT_SKIP
import desk_watchdog as dw


def test_micro_overrides_under_200():
    o = micro_full_deploy_overrides(25.0)
    assert o["sizing_focus_slots"] == 1
    assert o["target_bp_utilization_pct"] == 98.0
    assert o["max_single_name_equity_pct"] == 90.0


def test_posture_knobs_apply_micro_full_deploy():
    knobs = posture_knobs_for_broker(
        "Coinbase",
        {"risk_posture": "balanced", "auto_scale_growth": True},
        equity=24.66,
    )
    assert knobs["sizing_focus_slots"] == 1
    assert knobs["target_bp_utilization_pct"] == 98.0
    assert knobs.get("micro_full_deploy") is True


def test_risk_sizing_micro_cb_uses_deployable():
    """$25 CB must not strand cash under min ticket via risk-$ alone."""
    knobs = posture_knobs_for_broker(
        "Coinbase",
        {"auto_scale_growth": True, "risk_posture": "growth"},
        equity=24.66,
    )
    util = float(knobs["target_bp_utilization_pct"]) / 100.0
    detail = risk_sizing_breakdown(
        24.66,
        24.66,
        0.04,  # crypto-ish stop
        0.08,
        min_dollars=6.0,
        conviction_score=100.0,
        target_bp_utilization=util,
        sizing_focus_slots=int(knobs["sizing_focus_slots"]),
        soft_name_equity_frac=float(knobs["max_single_name_equity_pct"]) / 100.0,
        risk_pct_per_trade=float(knobs.get("risk_pct_per_trade", 0.9)),
        max_open_risk_pct=float(knobs.get("max_open_risk_pct", 8.0)),
    )
    assert not detail.get("skip_reason"), detail
    trade = float(detail["trade"])
    assert trade >= 6.0
    assert trade <= float(detail["deployable"]) + 1e-6
    assert trade >= 15.0  # near-full deploy on micro


def test_prefer_fundable_keeps_order_when_all_ok():
    rows = [
        {"ticker": "BONK", "score": 108, "price": 0.00002, "asset_type": "cryptocurrency"},
        {"ticker": "ETH", "score": 97, "price": 3500, "asset_type": "cryptocurrency"},
    ]
    out, demotions = prefer_fundable_buy_candidates(
        rows,
        buying_power=24.66,
        equity=24.66,
        broker_id="COINBASE",
        broker_name="Coinbase",
        settings={"auto_scale_growth": True, "min_trade_dollars": 5.0},
    )
    assert len(out) == 2
    assert out[0]["ticker"] in ("BONK", "ETH")


def test_local_ai_approve_small_ticket_not_punished():
    prop = {
        "ticker": "BONK",
        "broker": "Coinbase",
        "dollars": 22.0,
        "price": 0.00002,
        "score": 90,
        "engine": "CRYPTO",
        "asset_type": "cryptocurrency",
    }
    ctx = {
        "buying_power": 24.66,
        "deployable_bp": 24.0,
        "equity": 24.66,
        "posture": "growth",
        "dd_paused": False,
        "blockers": [],
        "max_affordable_share_price": 24.0,
        "small_book": True,
    }
    r = local_analyze_proposal(prop, ctx)
    assert r["verdict"] == VERDICT_APPROVE


def test_local_ai_skip_on_dd():
    prop = {
        "ticker": "ETH",
        "broker": "Robinhood",
        "dollars": 20.0,
        "price": 3500,
        "score": 90,
        "asset_type": "cryptocurrency",
    }
    ctx = {
        "buying_power": 80.0,
        "deployable_bp": 70.0,
        "equity": 80.0,
        "dd_paused": True,
        "dd_reason": "peak -23%",
        "blockers": [{"code": "dd_pause", "message": "Peak drawdown"}],
        "max_affordable_share_price": 70.0,
    }
    r = local_analyze_proposal(prop, ctx)
    assert r["verdict"] == VERDICT_SKIP


def test_ranked_then_stop_cleared_after_batch_done():
    lines = [
        "[2026-08-31 15:12:06] [Coinbase] Ranked 3 buys — top: BONK(108), ETH(97), BTC(95)",
        "[2026-08-31 15:12:06] [Coinbase] Buy batch start — 3 candidate(s) · advisor gate",
        "[2026-08-31 15:12:23] [Coinbase] Buy batch done — buys=0 proposals=0",
    ]
    snags = dw.scan_log_snags(lines)
    assert not any(s["code"] == "ranked_then_stop" for s in snags)


def test_ranked_then_stop_warns_when_no_batch():
    lines = [
        "[2026-08-31 15:12:06] [Coinbase] Ranked 3 buys — top: BONK(108)",
        "[2026-08-31 15:12:10] [AUTO] Cycle finished for Coinbase",
    ]
    snags = dw.scan_log_snags(lines)
    assert any(s["code"] == "ranked_then_stop" for s in snags)


def test_micro_equity_constant():
    assert MICRO_FULL_DEPLOY_EQUITY == 200.0
