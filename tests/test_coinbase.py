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
        cb.client.close_position.assert_not_called()


class TestCbSellAll(unittest.TestCase):
    def test_sell_all_prefers_close_position(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.live_trading_enabled = True
        cb.client = MagicMock()
        cb._fetch_all_accounts = MagicMock(return_value=[
            {"currency": "BTC", "available_balance": {"value": "0.01234567"}, "hold": {"value": "0"}},
        ])
        cb._get_product_limits = MagicMock(return_value={
            "base_increment": 0.00000001,
            "base_min_size": 0.00000001,
            "quote_min_size": 1.0,
        })
        cb.confirm_order = MagicMock(return_value=(True, "filled"))
        cb.client.close_position.return_value = {
            "success": True,
            "success_response": {"order_id": "close-1"},
        }
        status, oid = cb.place_sell_order(
            "BTC", "crypto", 50000.0, 0.01, 0.0, False, sell_all=True,
        )
        self.assertIn("Sell-All", status)
        self.assertEqual(oid, "close-1")
        cb.client.close_position.assert_called_once()
        kwargs = cb.client.close_position.call_args.kwargs
        self.assertEqual(kwargs.get("product_id"), "BTC-USD")
        self.assertTrue(float(kwargs.get("size") or 0) > 0)
        cb.client.market_order_sell.assert_not_called()

    def test_sell_all_falls_back_to_market_sell(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.live_trading_enabled = True
        cb.client = MagicMock()
        cb._fetch_all_accounts = MagicMock(return_value=[
            {"currency": "ETH", "available_balance": {"value": "0.5"}, "hold": {"value": "0"}},
        ])
        cb._get_product_limits = MagicMock(return_value={
            "base_increment": 0.0001,
            "base_min_size": 0.0001,
            "quote_min_size": 1.0,
        })
        cb.confirm_order = MagicMock(return_value=(True, "filled"))
        cb.client.close_position.return_value = {"success": False, "error_response": "unsupported"}
        cb.client.market_order_sell.return_value = {
            "success": True,
            "success_response": {"order_id": "mkt-9"},
        }
        status, oid = cb.place_sell_order(
            "ETH", "crypto", 3000.0, 0.4, 0.0, False, sell_all=True,
        )
        self.assertIn("Sell-All", status)
        self.assertEqual(oid, "mkt-9")
        cb.client.market_order_sell.assert_called_once()
        base = cb.client.market_order_sell.call_args.kwargs.get("base_size")
        self.assertAlmostEqual(float(base), 0.5, places=6)

    def test_partial_sell_skips_close_position(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.live_trading_enabled = True
        cb.client = MagicMock()
        cb._get_product_limits = MagicMock(return_value={
            "base_increment": 0.0001,
            "base_min_size": 0.0001,
            "quote_min_size": 1.0,
        })
        cb.confirm_order = MagicMock(return_value=(True, "filled"))
        cb.client.market_order_sell.return_value = {
            "success": True,
            "success_response": {"order_id": "partial-1"},
        }
        status, oid = cb.place_sell_order(
            "BTC", "crypto", 50000.0, 0.01, 0.0, False, sell_all=False,
        )
        self.assertIn("Coinbase Sell", status)
        self.assertNotIn("Sell-All", status)
        cb.client.close_position.assert_not_called()
        cb.client.market_order_sell.assert_called_once()


class TestCbRetryHelper(unittest.TestCase):
    def test_retryable_detection(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        exc = Exception("HTTP 429 rate limit")
        self.assertTrue(cb._cb_retryable(exc))
        self.assertFalse(cb._cb_retryable(Exception("invalid api key")))


if __name__ == "__main__":
    unittest.main()
