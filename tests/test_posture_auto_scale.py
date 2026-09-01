"""Auto-scale Growth posture by broker equity."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scoring import (
    AUTO_SCALE_BALANCED_CEILING,
    SMALL_BOOK_EQUITY,
    describe_posture_for_broker,
    equity_auto_posture,
    manual_posture_for_broker,
    posture_for_broker,
    posture_knobs_for_broker,
    set_broker_equity_snapshot,
)


def test_equity_auto_posture_micro_uses_growth():
    assert equity_auto_posture(75.0, settings={"auto_scale_growth": True}, manual_posture="balanced") == "growth"
    assert equity_auto_posture(499.0, settings={"auto_scale_growth": True}, manual_posture="aggressive") == "growth"


def test_equity_auto_posture_growing_uses_balanced():
    eq = SMALL_BOOK_EQUITY + 100.0
    assert equity_auto_posture(eq, settings={"auto_scale_growth": True}, manual_posture="balanced") == "balanced"
    assert equity_auto_posture(eq, settings={"auto_scale_growth": True}, manual_posture="aggressive") == "balanced"


def test_equity_auto_posture_growing_respects_safer():
    eq = SMALL_BOOK_EQUITY + 50.0
    assert equity_auto_posture(eq, settings={"auto_scale_growth": True}, manual_posture="safer") == "safer"


def test_equity_auto_posture_established_honors_manual():
    eq = AUTO_SCALE_BALANCED_CEILING + 500.0
    assert equity_auto_posture(eq, settings={"auto_scale_growth": True}, manual_posture="aggressive") == "aggressive"


def test_equity_auto_posture_disabled():
    assert equity_auto_posture(
        50.0, settings={"auto_scale_growth": False}, manual_posture="balanced",
    ) == "balanced"


def test_posture_for_broker_uses_equity_snapshot():
    set_broker_equity_snapshot({"Robinhood": 48.0})
    settings = {"risk_posture": "balanced", "auto_scale_growth": True}
    assert posture_for_broker("Robinhood", settings) == "growth"
    set_broker_equity_snapshot({"Robinhood": 800.0})
    assert posture_for_broker("Robinhood", settings) == "balanced"
    set_broker_equity_snapshot({})


def test_describe_posture_auto_label():
    desc = describe_posture_for_broker(
        "Robinhood",
        {"risk_posture": "balanced", "auto_scale_growth": True},
        equity=90.0,
    )
    assert desc["effective"] == "growth"
    assert desc["auto_scaled"] is True
    assert desc["label"] == "growth (auto)"
    assert desc["equity_tier"] == "micro"


def test_auto_scaled_knobs_use_profile_not_stale_settings():
    settings = {
        "risk_posture": "balanced",
        "auto_scale_growth": True,
        "max_buys_per_cycle": 1,
        "peak_dd_pause_pct": 0.12,
    }
    knobs = posture_knobs_for_broker("Robinhood", settings, equity=60.0)
    # Micro full-deploy (<$200) concentrates to 1 buy/slot; peak DD from Growth profile
    assert knobs["max_buys_per_cycle"] == 1
    assert knobs["sizing_focus_slots"] == 1
    assert knobs["peak_dd_pause_pct"] == 0.14
    assert knobs.get("micro_full_deploy") is True


def test_small_book_200_to_500_uses_two_slots():
    knobs = posture_knobs_for_broker(
        "Robinhood",
        {"risk_posture": "balanced", "auto_scale_growth": True},
        equity=350.0,
    )
    assert knobs["sizing_focus_slots"] == 2
    assert knobs["max_buys_per_cycle"] == 1  # Growth profile: 1 buy/cycle (profit discipline)
    assert knobs["target_bp_utilization_pct"] == 95.0


def test_manual_posture_override_still_works():
    settings = {
        "risk_posture": "balanced",
        "risk_posture_by_broker": {"Robinhood": "aggressive"},
    }
    assert manual_posture_for_broker("Robinhood", settings) == "aggressive"
    assert posture_for_broker("E*TRADE", settings) == "balanced"
