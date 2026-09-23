"""
1.42.34 — soak fixes from activity_log (8) / Discord overnight→RTH:
  - RH fractional buy defer only when current session cannot exit (not always overnight)
  - Overnight grade D Discord only when not armed
  - CB protective stop prices snap to quote_increment
"""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal, ROUND_DOWN
from unittest.mock import MagicMock, patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import auto_cycle as ac  # noqa: E402
import desk_watchdog as dw  # noqa: E402


class TestRthFracBuyDefer(unittest.TestCase):
    def test_rth_allows_fractional_buy(self):
        rth = {"label": "REGULAR", "fractional_ok": True, "equity_tradeable": True}
        self.assertIsNone(
            ac.equity_buy_defer_reason(
                "ACHR", 0.9, 5.63, "stock", rth, broker_name="Robinhood",
            )
        )

    def test_overnight_blocks_fractional_buy(self):
        overnight = {
            "label": "OVERNIGHT",
            "fractional_ok": False,
            "equity_tradeable": True,
        }
        why = ac.equity_buy_defer_reason(
            "ACHR", 0.9, 5.63, "stock", overnight, broker_name="Robinhood",
        )
        self.assertIsNotNone(why)
        self.assertIn("overnight", why.lower())


class TestOvernightGradeDiscord(unittest.TestCase):
    def test_grade_d_suppressed_when_armed(self):
        status = {
            "brokers": {
                "Robinhood": {"armed": True, "connected": True},
                "Coinbase": {"armed": True, "connected": True},
            },
            "overnight_scorecard": {"grade": "D", "tip": "review"},
            "protective_health": {"missing_count": 0},
        }
        snags = dw.scan_status_snags(status)
        codes = {s.get("code") for s in snags}
        self.assertNotIn("overnight_grade_low", codes)

    def test_grade_d_warns_when_disarmed(self):
        status = {
            "brokers": {
                "Robinhood": {"armed": False, "connected": True},
            },
            "overnight_scorecard": {"grade": "D", "tip": "review"},
            "protective_health": {"missing_count": 0},
        }
        snags = dw.scan_status_snags(status)
        codes = {s.get("code") for s in snags}
        self.assertIn("overnight_grade_low", codes)


class TestCbStopQuoteIncrement(unittest.TestCase):
    def test_stop_prices_snap_to_quote_increment(self):
        """Sub-$1 alts must not use fixed 6dp — that caused PREVIEW_INVALID_STOP_PRICE_PRECISION."""
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.client = MagicMock()
        cb.live_trading_enabled = True
        cb._product_limits_cache = {
            "FET": {
                "base_increment": 0.1,
                "base_min_size": 1.0,
                "quote_min_size": 1.0,
                "quote_increment": 0.0001,
            }
        }

        captured = {}

        def fake_call(fn, **kwargs):
            captured.update(kwargs)
            return {"success": True, "success_response": {"order_id": "oid-1"}}

        cb._cb_call = fake_call
        cb._cb_payload = lambda x: x
        cb._orders_allowed = lambda: (True, "")

        ok, oid, msg = cb.place_protective_stop("FET", "cryptocurrency", 45.7, 0.15966416, 0.08)
        self.assertTrue(ok, msg)
        self.assertEqual(oid, "oid-1")
        stop_s = captured.get("stop_price")
        limit_s = captured.get("limit_price")
        self.assertIsNotNone(stop_s)
        # Must be on quote_increment grid (0.0001)
        d_q = Decimal("0.0001")
        stop_dec = Decimal(str(stop_s))
        self.assertEqual(stop_dec % d_q, Decimal("0"))
        limit_dec = Decimal(str(limit_s))
        self.assertEqual(limit_dec % d_q, Decimal("0"))
        self.assertLess(limit_dec, stop_dec)


if __name__ == "__main__":
    unittest.main()
