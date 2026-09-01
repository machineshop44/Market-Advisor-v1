"""Unified trader context — same facts a human checks before buying."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import trader_context as tc


def test_small_book_breakouts_recommended():
    ctx = tc.build_trader_context(
        "Robinhood",
        equity=75.0,
        buying_power=50.0,
        settings={"risk_posture": "growth"},
        session_label="REGULAR",
        armed=True,
        connected=True,
        supports_equities=True,
        supports_crypto=False,
        regime_equity_ok=True,
        regime_equity_reason="",
    )
    assert ctx["small_book"] is True
    assert ctx["engines"]["breakouts"]["recommended"] is True
    assert ctx["engines"]["core"]["parked"] is True
    assert ctx["max_affordable_share_price"] > 0
    assert "Robinhood" in ctx["summary"]


def test_low_bp_blocker():
    ctx = tc.build_trader_context(
        "Robinhood",
        equity=40.0,
        buying_power=2.0,
        settings={"min_trade_dollars": 5.0},
        armed=True,
        connected=True,
        supports_equities=True,
        supports_crypto=False,
        regime_equity_ok=True,
    )
    codes = [b["code"] for b in ctx["blockers"]]
    assert "low_bp" in codes
    assert ctx["can_place_new_buy"] is False


def test_disarmed_not_auto_ready():
    ctx = tc.build_trader_context(
        "Coinbase",
        equity=100.0,
        buying_power=80.0,
        armed=False,
        connected=True,
        supports_crypto=True,
        supports_equities=False,
        regime_crypto_ok=True,
    )
    assert ctx["can_place_new_buy"] is True
    assert ctx["auto_ready"] is False
    assert any(b["code"] == "disarmed" for b in ctx["blockers"])


def test_format_trader_digest():
    ctx = tc.build_trader_context(
        "Robinhood",
        equity=50.0,
        buying_power=45.0,
        armed=True,
        connected=True,
        supports_equities=True,
        supports_crypto=False,
        regime_equity_ok=True,
    )
    text = tc.format_trader_digest({"Robinhood": ctx})
    assert "Trader desk context" in text
    assert "Robinhood" in text


def test_build_from_monitor_status():
    status = {
        "market": "REGULAR",
        "halted": False,
        "balances": {
            "Robinhood": {"equity": 55.0, "cash": 48.0},
        },
        "brokers": {
            "Robinhood": {"connected": True, "armed": True, "dd_pause": False},
        },
        "holdings_count": {"Robinhood": 1},
        "portfolio_heat": {"combined": {}},
    }
    ctx = tc.build_from_monitor_status(status, "Robinhood")
    assert ctx["buying_power"] == 48.0
    assert ctx["equity"] == 55.0
