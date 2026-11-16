"""1.43.1 — dividend and tech-ETF names are holds, not flips."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import scoring


def test_psx_is_an_income_hold_by_default():
    assert scoring.is_income_hold("PSX", {})
    assert scoring.is_income_hold("schd", {})
    assert "QQQ" in scoring.income_hold_symbols({})


def test_income_list_can_be_turned_off():
    assert not scoring.is_income_hold("PSX", {"income_holds_enabled": False})
    assert scoring.is_income_hold("XOM", {"income_hold_symbols": "XOM, CVX"})
    assert not scoring.is_income_hold("PSX", {"income_hold_symbols": "XOM"})


def test_income_hold_is_not_sold_on_a_hard_stop():
    reason = scoring.evaluate_holding(
        "PSX", 100.0, live_price=50.0, settings={},
    )
    assert reason.startswith("HOLD (Income hold")


def test_income_hold_is_not_a_rotation_funder():
    scoring._portfolio_memory.setdefault("ROBINHOOD", {})
    scoring._portfolio_memory["ROBINHOOD"]["PSX"] = {
        "highest": 140.0,
        "buy_time": time.time() - 400 * 60,
        "last_eval": time.time(),
    }
    fund = scoring.pick_rotation_funding(
        "AVEX",
        120,
        False,
        [{
            "ticker": "PSX",
            "price": 130.0,
            "avg_cost": 140.0,
            "value": 130,
            "is_crypto": False,
            "shares": 1,
        }],
        posture="growth",
        broker_id="ROBINHOOD",
        block_reason="max_open",
        score_fn=lambda t, is_crypto=False: 70.0,
        skip_regime_check=True,
        settings={},
    )
    assert fund is None
