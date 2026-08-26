"""Unit tests for 1.34.0 grade-lift: movers, holidays, fills CSV, ET/CB limit offset."""
import csv
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestCryptoMoversMerge(unittest.TestCase):
    def test_dedupe_and_tag(self):
        from auto_cycle import merge_crypto_scan_universe

        out = merge_crypto_scan_universe(
            curated=["BTC", "ETH", "SOL"],
            movers=["PEPE", "btc", "WIF", "USDT", "??"],
            max_movers=8,
        )
        syms = [r["symbol"] for r in out]
        self.assertEqual(syms.count("BTC"), 1)
        self.assertEqual(out[0]["type"], "Crypto")
        mover_syms = {r["symbol"] for r in out if r["type"] == "Crypto Mover"}
        self.assertIn("PEPE", mover_syms)
        self.assertIn("WIF", mover_syms)
        self.assertNotIn("USDT", mover_syms)
        self.assertNotIn("BTC", mover_syms)

    def test_max_movers_cap(self):
        from auto_cycle import merge_crypto_scan_universe

        movers = ["WIF", "FLOKI", "ORDI", "SUI", "APT", "NEAR", "INJ", "TIA"]
        out = merge_crypto_scan_universe(curated=["BTC"], movers=movers, max_movers=3)
        self.assertEqual(sum(1 for r in out if r["type"] == "Crypto Mover"), 3)

    def test_extract_coinbase_usd_movers(self):
        from auto_cycle import extract_coinbase_usd_movers

        payload = {
            "products": [
                {"product_id": "WIF-USD", "status": "online", "price_percentage_change_24h": "12.5"},
                {"product_id": "BTC-USD", "status": "online", "price_percentage_change_24h": "2.0"},
                {"product_id": "USDT-USD", "status": "online", "price_percentage_change_24h": "0.1"},
                {"product_id": "ETH-EUR", "status": "online", "price_percentage_change_24h": "9.0"},
                {"product_id": "FLOKI-USD", "status": "online", "price_percentage_change_24h": "8.0"},
                {"product_id": "FLOP-USD", "status": "delisted", "price_percentage_change_24h": "50.0"},
            ]
        }
        got = extract_coinbase_usd_movers(payload, limit=5)
        self.assertEqual(got[0], "WIF")
        self.assertIn("FLOKI", got)
        self.assertNotIn("BTC", got)
        self.assertNotIn("USDT", got)


class TestMarketCalendar(unittest.TestCase):
    def test_known_2026_holidays(self):
        from market_calendar import is_nyse_holiday, is_equity_session_day, nyse_holidays

        # New Year's Day 2026 is Thursday
        self.assertTrue(is_nyse_holiday(date(2026, 1, 1)))
        self.assertFalse(is_equity_session_day(date(2026, 1, 1)))
        # Independence Day 2026 is Saturday → observed Friday Jul 3
        self.assertIn(date(2026, 7, 3), nyse_holidays(2026))
        self.assertTrue(is_nyse_holiday(date(2026, 7, 3)))
        # Regular Tuesday
        self.assertTrue(is_equity_session_day(date(2026, 8, 25)))
        # Weekend
        self.assertFalse(is_equity_session_day(date(2026, 8, 22)))

    def test_thanksgiving_2026(self):
        from market_calendar import is_nyse_holiday

        # 4th Thursday of Nov 2026 = Nov 26
        self.assertTrue(is_nyse_holiday(date(2026, 11, 26)))


class TestExportFillsCsv(unittest.TestCase):
    def test_columns_and_filter(self):
        import journal as journal_mod

        rows = [
            {
                "timestamp": "2026-08-25T10:00:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "SPY",
                "asset_type": "stock",
                "price": 500,
                "qty": 1,
                "dollars": 500,
                "status": "Filled",
                "confirmed": True,
                "fee_est": 0.01,
            },
            {
                "timestamp": "2026-08-25T11:00:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "BAD",
                "status": "Fail: rejected",
            },
            {
                "timestamp": "2026-08-25T12:00:00",
                "broker": "Robinhood",
                "side": "NOTE",
                "ticker": "X",
                "status": "info",
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "fills.csv")
            with patch.object(journal_mod, "read_since_days", return_value=rows):
                n = journal_mod.export_fills_csv(path, days=7)
            self.assertEqual(n, 1)
            with open(path, encoding="utf-8", newline="") as f:
                r = csv.DictReader(f)
                fields = r.fieldnames
                self.assertIn("fee_est", fields)
                self.assertIn("fee_paid", fields)
                self.assertIn("slippage_bps", fields)
                data = list(r)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["ticker"], "SPY")


class TestEtLimitOffsetFraction(unittest.TestCase):
    def test_limit_price_uses_fraction_not_percent(self):
        from etrade_broker import ETradeAdapter

        et = ETradeAdapter()
        et.is_connected = True
        et.environment = "sandbox"
        et.live_trading_enabled = True
        et.supports_fractional = True
        et.supports_extended_hours = True
        et.account_id_key = "acct"
        et.client = MagicMock()
        et.client.preview_equity_order.return_value = {"PreviewOrderResponse": {"PreviewIds": {"previewId": 99}}}
        et.client.place_equity_order.return_value = {"PlaceOrderResponse": {"OrderIds": {"orderId": "OID1"}}}

        # 0.5% buffer as fraction 0.005 → $100.50 (NOT $100 * 1.005/100)
        with patch("etrade_broker._extract_preview_id", return_value=99), patch(
            "etrade_broker._extract_order_id", return_value="OID1"
        ):
            status, spent, oid = et.place_buy_order("SPY", "stock", 100.0, 200.0, 0.005, False)

        self.assertIn("LIMIT", status)
        place_xml = et.client.place_equity_order.call_args[0][1]
        self.assertIn("<limitPrice>100.50</limitPrice>", place_xml)
        self.assertNotIn("<limitPrice>1.01</limitPrice>", place_xml)


class TestCoinbaseLimitBuy(unittest.TestCase):
    def test_limit_buy_when_offset_positive(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.live_trading_enabled = True
        cb.client = MagicMock()
        cb._get_product_limits = MagicMock(
            return_value={
                "base_increment": 0.0001,
                "base_min_size": 0.0001,
                "quote_min_size": 1.0,
                "quote_increment": 0.01,
            }
        )
        cb._cb_call = MagicMock(
            return_value={"success": True, "success_response": {"order_id": "cb1"}}
        )
        cb._cb_payload = lambda x: x
        cb.confirm_order = MagicMock(return_value=(True, "FILLED"))
        cb._orders_allowed = MagicMock(return_value=(True, ""))

        status, spent, oid = cb.place_buy_order("BTC", "crypto", 50000.0, 100.0, 0.01, False)
        self.assertIn("limit", status.lower())
        self.assertEqual(oid, "cb1")
        kwargs = cb._cb_call.call_args.kwargs
        self.assertEqual(kwargs.get("product_id"), "BTC-USD")
        # 50000 * 1.01 = 50500
        self.assertTrue(float(kwargs.get("limit_price")) >= 50500.0)

    def test_market_when_offset_zero(self):
        from broker import CoinbaseAdapter

        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb.live_trading_enabled = True
        cb.client = MagicMock()
        cb._cb_call = MagicMock(
            return_value={"success": True, "success_response": {"order_id": "cb2"}}
        )
        cb._cb_payload = lambda x: x
        cb.confirm_order = MagicMock(return_value=(True, "FILLED"))
        cb._orders_allowed = MagicMock(return_value=(True, ""))

        status, spent, oid = cb.place_buy_order("ETH", "crypto", 3000.0, 50.0, 0.0, False)
        self.assertIn("market", status.lower())
        self.assertEqual(cb._cb_call.call_args.args[0], cb.client.market_order_buy)


class TestCryptoMoverScoreGate(unittest.TestCase):
    def test_mover_min_score_constant(self):
        from scoring import CRYPTO_MOVER_MIN_SCORE_FOR_ENTRY

        self.assertGreaterEqual(CRYPTO_MOVER_MIN_SCORE_FOR_ENTRY, 60)


if __name__ == "__main__":
    unittest.main()
