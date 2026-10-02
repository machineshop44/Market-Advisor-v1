import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import auto_cycle
import desk_advisor_ai
from overnight_research import assess_overnight_hold


def _bars(n=80, drift=0.002, gap=0.001, close_loc=0.8, gap_shock=None):
    """Steady uptrend; each day opens `gap` above prior close; last bar closes near its high."""
    opens, highs, lows, closes = [], [], [], []
    px = 10.0
    for i in range(n):
        o = px * (1 + gap)
        if gap_shock and i in gap_shock:
            o = px * (1 + gap_shock[i])
        c = o * (1 + drift)
        lo = min(o, c) * 0.99
        hi = max(o, c) * 1.01
        opens.append(o); highs.append(hi); lows.append(lo); closes.append(c)
        px = c
    rng = highs[-1] - lows[-1]
    closes[-1] = lows[-1] + rng * close_loc
    return opens, highs, lows, closes


def test_clean_uptrend_passes():
    r = assess_overnight_hold(*_bars(), stop_pct=0.04, earnings_next=False)
    assert r["ok"] is True, r
    assert r["summary"].startswith("Overnight OK")


def test_earnings_blocks():
    r = assess_overnight_hold(*_bars(), stop_pct=0.04, earnings_next=True)
    assert r["ok"] is False
    assert any("earnings" in x for x in r["fails"])


def test_unknown_earnings_blocks_new_overnight_entry():
    r = assess_overnight_hold(*_bars(), stop_pct=0.04, earnings_next=None)
    assert r["ok"] is False


def test_fat_gap_down_tail_blocks():
    shocks = {i: -0.06 for i in range(30, 80, 6)}
    r = assess_overnight_hold(*_bars(gap_shock=shocks), stop_pct=0.04, earnings_next=False)
    assert r["ok"] is False
    assert any("gap-downs" in x for x in r["fails"])


def test_fading_into_close_blocks():
    r = assess_overnight_hold(*_bars(close_loc=0.2), stop_pct=0.04, earnings_next=False)
    assert r["ok"] is False
    assert any("fading" in x for x in r["fails"])


def test_downtrend_blocks():
    r = assess_overnight_hold(*_bars(drift=-0.004, gap=-0.001), stop_pct=0.04, earnings_next=False)
    assert r["ok"] is False


def test_short_history_blocks():
    r = assess_overnight_hold(*_bars(n=20), stop_pct=0.04, earnings_next=False)
    assert r["ok"] is False


def test_intent_holds_small_loser():
    action, why = auto_cycle.et_eod_overnight_decision(
        "X", price=2.765, avg_cost=2.775, has_broker_stop=True,
        earnings_next=False, settings={}, overnight_intent=True,
    )
    assert action == "hold"
    assert "as planned" in why


def test_intent_flattens_breakdown():
    action, _ = auto_cycle.et_eod_overnight_decision(
        "X", price=2.70, avg_cost=2.775, has_broker_stop=True,
        earnings_next=False, settings={}, overnight_intent=True,
    )
    assert action == "flatten"


def test_intent_still_needs_stop_and_no_earnings():
    assert auto_cycle.et_eod_overnight_decision(
        "X", price=2.80, avg_cost=2.775, has_broker_stop=False,
        earnings_next=False, settings={}, overnight_intent=True,
    )[0] == "flatten"
    assert auto_cycle.et_eod_overnight_decision(
        "X", price=2.80, avg_cost=2.775, has_broker_stop=True,
        earnings_next=True, settings={}, overnight_intent=True,
    )[0] == "flatten"


def _prop(overnight):
    return {
        "ticker": "AMC", "broker": "E*TRADE", "dollars": 19.0, "price": 2.77,
        "score": 121, "engine": "BREAKOUT", "asset_type": "stock", "overnight": overnight,
    }


def test_local_advisor_skips_failed_overnight():
    out = desk_advisor_ai.local_analyze_proposal(
        _prop({"ok": False, "summary": "Not an overnight hold: fading into the close"}),
        {"buying_power": 80, "equity": 100},
    )
    assert out["verdict"] == desk_advisor_ai.VERDICT_SKIP
    assert "overnight" in out["brief"].lower()


def test_local_advisor_cites_passing_overnight():
    out = desk_advisor_ai.local_analyze_proposal(
        _prop({"ok": True, "summary": "Overnight OK: gap-down tail -1.2% inside stop"}),
        {"buying_power": 80, "equity": 100},
    )
    assert out["verdict"] == desk_advisor_ai.VERDICT_APPROVE
    assert "overnight" in out["brief"].lower()


def test_prompt_flags_overnight_hold():
    p = desk_advisor_ai._proposal_prompt(_prop({"ok": True, "summary": "x"}), {})
    assert "OVERNIGHT HOLD" in p
    p2 = desk_advisor_ai._proposal_prompt(_prop(None), {})
    assert "OVERNIGHT HOLD" not in p2
