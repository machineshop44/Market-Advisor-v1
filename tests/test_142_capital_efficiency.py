"""1.42 — capital efficiency, buy-lag day-loss, holdings mismatch."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_sep1_buy_lag_never_accepts_day_loss():
    """Reproduce RH $100.49 → $81.85 after ~$18.64 BTC buy — must keep forever."""
    from balance_guard import decide_suspicious_equity, equity_drop_matches_recent_buy

    baseline = 100.49
    ref = 100.49
    glitch = 81.85
    buy = 18.64
    assert equity_drop_matches_recent_buy(glitch, ref, buy)

    streak = 0
    for _ in range(5):
        d = decide_suspicious_equity(
            glitch, ref, baseline,
            last_trusted=ref,
            bad_streak=streak,
            loss_limit=5.0,
            recent_buy_notional=buy,
        )
        assert d["action"] == "keep"
        assert d["trusted"] is False
        assert "buy" in d["reason"].lower()
        streak = d["streak"]


def test_holdings_equity_gap():
    from balance_guard import holdings_equity_gap

    bad, gap = holdings_equity_gap(100.49, 63.39, 0.0)
    assert bad is True
    assert gap > 30.0
    ok, _ = holdings_equity_gap(100.49, 63.39, 37.0)
    assert ok is False


def test_micro_cb_park():
    import desk_orchestration as do

    parked, why = do.micro_broker_buy_parked(
        "Coinbase", 22.0, {"capital_park_micro_crypto": True, "capital_min_deployable_buy": 40},
    )
    assert parked is True
    assert "Micro crypto" in why
    ok, _ = do.micro_broker_buy_parked(
        "E*TRADE", 22.0, {"capital_park_micro_crypto": True, "capital_min_deployable_buy": 40},
    )
    assert ok is False
    # Legacy alias still honored
    legacy, _ = do.micro_broker_buy_parked(
        "Coinbase", 22.0, {"capital_park_micro_crypto": True, "capital_park_micro_floor": 40},
    )
    assert legacy is True
    # Default park OFF — leftover crypto BP still scans / autosizes
    off, _ = do.micro_broker_buy_parked("Coinbase", 3.0, {})
    assert off is False
    # When park enabled, floor tracks min_trade_dollars (not coin price)
    under_min, _ = do.micro_broker_buy_parked(
        "Coinbase", 3.0, {"capital_park_micro_crypto": True, "min_trade_dollars": 5.0},
    )
    assert under_min is True
    at_min, _ = do.micro_broker_buy_parked(
        "Coinbase", 5.0, {"capital_park_micro_crypto": True, "min_trade_dollars": 5.0},
    )
    assert at_min is False


def test_capital_efficiency_grade():
    import desk_orchestration as do

    ctx = {
        "E*TRADE": {"can_place_new_buy": True, "deployable_bp": 92, "buying_power": 92},
        "Robinhood": {"can_place_new_buy": True, "deployable_bp": 58, "buying_power": 63},
        "Coinbase": {"can_place_new_buy": True, "deployable_bp": 22, "buying_power": 24},
    }
    settings = {
        "desk_focus_mode": "auto",
        "desk_preferred_primary": "E*TRADE",
        "capital_park_micro_crypto": True,
        "capital_min_deployable_buy": 40,
    }
    focus = do.resolve_focus_broker(ctx, settings)
    assert focus == "E*TRADE"
    grade = do.capital_efficiency_grade(ctx, focus_broker=focus, settings=settings)
    assert grade["grade"] in ("fragmented", "consolidating")
    line = do.format_capital_efficiency_line(grade, money_fmt=lambda x: f"${x:.0f}")
    assert "Capital:" in line


def test_preferred_primary_wins_tie():
    import desk_orchestration as do

    ctx = {
        "Robinhood": {"can_place_new_buy": True, "deployable_bp": 90, "buying_power": 90},
        "E*TRADE": {"can_place_new_buy": True, "deployable_bp": 90, "buying_power": 90},
    }
    focus = do.resolve_focus_broker(
        ctx, {"desk_focus_mode": "auto", "desk_preferred_primary": "E*TRADE"},
    )
    assert focus == "E*TRADE"


def test_morning_scan_mult():
    import desk_orchestration as do

    m = do.focus_scan_multiplier(
        "E*TRADE", "E*TRADE",
        {"desk_focus_mode": "auto", "focus_broker_scan_mult": 2.0, "focus_morning_scan_mult": 3.0},
        morning_boost=True,
    )
    assert m == 3.0


def test_consolidation_playbook_ach_hint():
    import desk_orchestration as do

    ctx = {
        "E*TRADE": {"can_place_new_buy": True, "deployable_bp": 92, "buying_power": 100},
        "Robinhood": {"can_place_new_buy": True, "deployable_bp": 58, "buying_power": 63},
        "Coinbase": {"can_place_new_buy": True, "deployable_bp": 22, "buying_power": 24},
    }
    text = do.format_consolidation_playbook(
        ctx, settings={"desk_preferred_primary": "E*TRADE"},
    )
    assert "ACH idle cash" in text
    assert "E*TRADE" in text
    assert "mark-to-market" in text.lower() or "Day P&L" in text
