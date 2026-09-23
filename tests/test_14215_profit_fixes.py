"""1.42.15 — advisor fallback, regime local rules, ET cancel idempotency."""
import os
import sys
import unittest
from unittest.mock import MagicMock

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import desk_advisor_ai as dai  # noqa: E402
from etrade_broker import ETradeAdapter, _order_status_not_cancellable  # noqa: E402


class TestAdvisorLocalFallback(unittest.TestCase):
    def test_regime_block_skips_without_settings_override(self):
        prop = {
            "ticker": "IONQ",
            "broker": "E*TRADE",
            "dollars": 40.0,
            "price": 40.0,
            "score": 88.0,
            "regime_caution": True,
        }
        ctx = {
            "buying_power": 100.0,
            "blockers": [{"code": "regime_equity", "message": "SPY 1H downtrend"}],
            "allow_buys_when_regime_blocked": False,
        }
        out = dai.local_analyze_proposal(prop, ctx)
        self.assertEqual(out["verdict"], dai.VERDICT_SKIP)

    def test_budget_fallback_waits_on_regime_caution(self):
        prop = {
            "ticker": "RKLB",
            "broker": "E*TRADE",
            "dollars": 35.0,
            "price": 35.0,
            "score": 90.0,
            "regime_caution": True,
        }
        ctx = {
            "buying_power": 100.0,
            "blockers": [],
            "allow_buys_when_regime_blocked": True,
        }
        out = {
            "verdict": dai.VERDICT_APPROVE,
            "brief": "ok",
            "source": "local_fallback",
        }
        guarded = dai._local_fallback_guard(out, prop)
        self.assertEqual(guarded["verdict"], dai.VERDICT_WAIT)
        self.assertIn("regime", guarded["brief"].lower())


class TestEtCancelIdempotent(unittest.TestCase):
    def test_terminal_status_not_cancellable(self):
        self.assertTrue(_order_status_not_cancellable("CANCELLED"))
        self.assertTrue(_order_status_not_cancellable("EXECUTED"))
        self.assertFalse(_order_status_not_cancellable("OPEN"))

    def test_cancel_treats_already_cancelled_as_ok(self):
        et = ETradeAdapter()
        et.is_connected = True
        et.account_id_key = "acct"
        et.client = MagicMock()
        et.client.cancel_order.side_effect = Exception("HTTP 400")
        et._order_status_from_list = MagicMock(return_value="CANCELLED")
        ok, msg = et.cancel_order("12345")
        self.assertTrue(ok)
        self.assertIn("cancel", msg.lower())


if __name__ == "__main__":
    unittest.main()
