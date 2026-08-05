"""Coinbase adapter: nested payload hardening, limits, live-order gate."""
import os
import sys
import unittest
from unittest.mock import MagicMock

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestCbParseHelpers(unittest.TestCase):
    def test_money_value_nested_and_scalar(self):
        from broker import _money_value, _as_dict, _as_list, _cb_response_dict

        self.assertAlmostEqual(_money_value({"value": "12.5", "currency": "USD"}), 12.5)
        self.assertAlmostEqual(_money_value("3.25"), 3.25)
        self.assertAlmostEqual(_money_value(None), 0.0)
        self.assertAlmostEqual(_money_value("oops"), 0.0)
        # string leaf must not explode via .get chain
        self.assertEqual(_as_dict("oops"), {})
        self.assertEqual(_as_list(None), [])
        self.assertEqual(_as_list({"a": 1}), [{"a": 1}])
        self.assertEqual(_cb_response_dict([{"currency": "BTC"}]), {"accounts": [{"currency": "BTC"}]})
        self.assertEqual(_cb_response_dict("bad"), {})


class TestCbHoldingsShape(unittest.TestCase):
    def test_malformed_balance_leaves_do_not_explode(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.client = MagicMock()
        # available_balance as string leaf (classic crash on .get chain)
        cb.client.get_accounts.return_value = {
            "accounts": [
                {"currency": "USD", "available_balance": "41.25", "hold": None},
                {"currency": "BTC", "available_balance": {"value": "0.01"}, "hold": "bad"},
                "not-an-account",
            ],
            "has_next": False,
        }
        cb.get_live_price = MagicMock(return_value=50000.0)
        eq, bp = cb.get_account_balances()
        self.assertGreaterEqual(eq, 0.0)
        self.assertAlmostEqual(bp, 41.25)
        holdings = cb.get_current_holdings()
        self.assertIsInstance(holdings, list)
        tickers = {h["ticker"] for h in holdings}
        self.assertIn("BTC", tickers)

    def test_non_dict_accounts_payload(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.client = MagicMock()
        cb.client.get_accounts.return_value = "oops"
        holdings = cb.get_current_holdings()
        self.assertEqual(holdings, [])

    def test_product_limits_from_mock(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.client = MagicMock()
        cb.client.get_product.return_value = {
            "base_increment": "0.0001",
            "base_min_size": "0.001",
            "quote_min_size": "5",
        }
        limits = cb._get_product_limits("ETH")
        self.assertAlmostEqual(limits["base_increment"], 0.0001)
        self.assertAlmostEqual(limits["base_min_size"], 0.001)
        self.assertAlmostEqual(limits["quote_min_size"], 5.0)
        dust, reason = cb.position_is_dust("ETH", 0.00001, 3000.0)
        self.assertTrue(dust)
        self.assertTrue(reason)


class TestCbLiveTradingGate(unittest.TestCase):
    def test_orders_blocked_when_disabled(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.live_trading_enabled = False
        cb.client = MagicMock()
        status, spent, oid = cb.place_buy_order("BTC", "crypto", 100.0, 10.0, 0.0, False)
        self.assertIn("live trading is disabled", status.lower())
        self.assertEqual(spent, 0.0)
        self.assertIsNone(oid)
        sell_status, sell_oid = cb.place_sell_order("BTC", "crypto", 100.0, 0.01, 0.0, False)
        self.assertIn("live trading is disabled", sell_status.lower())
        self.assertIsNone(sell_oid)
        cb.client.market_order_buy.assert_not_called()
        cb.client.market_order_sell.assert_not_called()


class TestCbRetryHelper(unittest.TestCase):
    def test_retryable_detection(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        exc = Exception("HTTP 429 rate limit")
        self.assertTrue(cb._cb_retryable(exc))
        self.assertFalse(cb._cb_retryable(Exception("invalid api key")))


if __name__ == "__main__":
    unittest.main()
