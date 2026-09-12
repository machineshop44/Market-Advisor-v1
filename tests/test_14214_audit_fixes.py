"""
1.42.14 audit fixes — fill honesty helpers, CB stops flag, crypto multi-venue, monitor probe.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import auto_cycle as ac  # noqa: E402
import broker as br  # noqa: E402
import monitor  # noqa: E402


class TestEtRegularSessionGates(unittest.TestCase):
    def test_order_session_flags_et_regular_only(self):
        flags = ac.order_session_flags(
            "E*TRADE",
            {"label": "EXTENDED", "use_ext": True, "market_hours": "extended_hours"},
        )
        self.assertFalse(flags["use_ext"])
        self.assertEqual(flags["market_hours"], "regular_hours")
        self.assertFalse(flags["et_equity_session_ok"])

    def test_etrade_equity_session_ok_blocks_extended(self):
        ok, why = ac.etrade_equity_session_ok({"label": "EXTENDED"})
        self.assertFalse(ok)
        self.assertIn("REGULAR", why)
        ok2, _ = ac.etrade_equity_session_ok({"label": "REGULAR"})
        self.assertTrue(ok2)

    def test_place_buy_skips_outside_regular_label(self):
        from etrade_broker import ETradeAdapter

        et = ETradeAdapter()
        et.is_connected = True
        et.environment = "sandbox"
        et.live_trading_enabled = True
        et.account_id_key = "acct"
        et.client = MagicMock()
        status, spent, oid = et.place_buy_order(
            "SMCI", "stock", 40.0, 40.0, 0.005, False,
            market_hours="regular_hours",
            session_label="EXTENDED",
        )
        self.assertIn("idle", status.lower())
        self.assertEqual(spent, 0.0)
        self.assertIsNone(oid)
        et.client.preview_equity_order.assert_not_called()


class TestFillHonestyHelpers(unittest.TestCase):
    def test_working_unfilled_pending_fill(self):
        self.assertTrue(
            ac.buy_order_is_working_unfilled(
                "E*TRADE Buy submitted pending fill (MARKET 1 A; OPEN)"
            )
        )
        self.assertFalse(
            ac.buy_order_is_working_unfilled("Skipped: Limit unfilled (x) — cancelled")
        )
        self.assertFalse(ac.buy_order_is_working_unfilled("E*TRADE Buy Filled (MARKET 1 A)"))

    def test_rh_cancel_unfilled_success(self):
        rh = br.RobinhoodAdapter.__new__(br.RobinhoodAdapter)
        rh.cancel_order = MagicMock(return_value=(True, "cancelled"))
        status, spent, oid = rh._rh_cancel_unfilled("oid1", "confirmed", is_crypto=False)
        self.assertIn("cancelled", status.lower())
        self.assertEqual(spent, 0.0)
        self.assertIsNone(oid)

    def test_rh_cancel_unfilled_fail(self):
        rh = br.RobinhoodAdapter.__new__(br.RobinhoodAdapter)
        rh.cancel_order = MagicMock(return_value=(False, "network"))
        status, spent, oid = rh._rh_cancel_unfilled("oid1", "confirmed", is_crypto=True)
        self.assertIn("cancel failed", status.lower())
        self.assertEqual(spent, 0.0)
        self.assertEqual(oid, "oid1")

    def test_coinbase_protective_stops_enabled(self):
        cb = br.CoinbaseAdapter()
        self.assertTrue(cb.supports_protective_stops)


class TestMonitorProbe(unittest.TestCase):
    def test_probe_false_when_not_running(self):
        with patch.object(monitor, "is_running", return_value=False):
            self.assertFalse(monitor.probe_localhost())


if __name__ == "__main__":
    unittest.main()
