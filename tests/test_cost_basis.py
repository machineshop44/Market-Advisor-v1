"""Cost-basis seeding: journal VWAP, resolve priority, dust honesty."""
import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cost_basis as cb


class TestInventoryVwap(unittest.TestCase):
    def test_buy_only_vwap(self):
        rows = [
            {
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "DOGE",
                "price": 0.10,
                "qty": 100,
                "status": "Filled",
                "confirmed": True,
            },
            {
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "DOGE",
                "price": 0.20,
                "qty": 100,
                "status": "Filled",
                "confirmed": True,
            },
        ]
        self.assertAlmostEqual(cb.inventory_vwap_from_journal(rows, "Robinhood", "DOGE"), 0.15)

    def test_partial_sell_keeps_avg(self):
        rows = [
            {
                "broker": "Coinbase",
                "side": "BUY",
                "ticker": "ETH",
                "price": 2000.0,
                "qty": 1.0,
                "status": "Filled",
                "confirmed": True,
            },
            {
                "broker": "Coinbase",
                "side": "SELL",
                "ticker": "ETH",
                "price": 2100.0,
                "qty": 0.4,
                "status": "Filled",
                "confirmed": True,
            },
        ]
        self.assertAlmostEqual(
            cb.inventory_vwap_from_journal(rows, "Coinbase", "ETH"), 2000.0
        )

    def test_full_exit_returns_zero(self):
        rows = [
            {
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "SHIB",
                "price": 0.00001,
                "qty": 1_000_000,
                "status": "Filled",
                "confirmed": True,
            },
            {
                "broker": "Robinhood",
                "side": "SELL",
                "ticker": "SHIB",
                "price": 0.000012,
                "qty": 1_000_000,
                "status": "Filled",
                "confirmed": True,
            },
        ]
        self.assertEqual(cb.inventory_vwap_from_journal(rows, "Robinhood", "SHIB"), 0.0)

    def test_ignores_other_broker(self):
        rows = [
            {
                "broker": "Coinbase",
                "side": "BUY",
                "ticker": "LINK",
                "price": 12.0,
                "qty": 2,
                "status": "Filled",
                "confirmed": True,
            }
        ]
        self.assertEqual(cb.inventory_vwap_from_journal(rows, "Robinhood", "LINK"), 0.0)


class TestResolveHoldingCost(unittest.TestCase):
    def test_broker_wins_when_sane(self):
        cost, src = cb.resolve_holding_cost(
            broker_cost=95.0, tracked_cache=90.0, journal_vwap=88.0, mark=100.0
        )
        self.assertAlmostEqual(cost, 95.0)
        self.assertEqual(src, "broker")

    def test_journal_when_broker_dust(self):
        cost, src = cb.resolve_holding_cost(
            broker_cost=0.0007,
            tracked_cache=0.0,
            journal_vwap=0.12,
            last_known=0.11,
            mark=0.13,
        )
        self.assertAlmostEqual(cost, 0.12)
        self.assertEqual(src, "journal_vwap")

    def test_tracked_before_last_known(self):
        cost, src = cb.resolve_holding_cost(
            broker_cost=0.0,
            tracked_cache=1.5,
            journal_vwap=0.0,
            last_known=1.4,
            mark=1.6,
        )
        self.assertAlmostEqual(cost, 1.5)
        self.assertEqual(src, "tracked")

    def test_last_known_fallback(self):
        cost, src = cb.resolve_holding_cost(
            broker_cost=0.0, tracked_cache=0.0, journal_vwap=0.0, last_known=42.0, mark=40.0
        )
        self.assertAlmostEqual(cost, 42.0)
        self.assertEqual(src, "last_known")

    def test_unknown(self):
        cost, src = cb.resolve_holding_cost(broker_cost=0.0, mark=50.0)
        self.assertEqual(cost, 0.0)
        self.assertEqual(src, "unknown")

    def test_normalize_ticker(self):
        self.assertEqual(cb.normalize_ticker("eth-usd"), "ETH")
        self.assertEqual(cb.normalize_ticker("ETH-USD"), "ETH")
        self.assertEqual(cb.normalize_ticker("SHIB"), "SHIB")

    def test_parse_manual_basis_lines(self):
        text = """
        # comment
        Coinbase:ETH=1920.5
        Robinhood SHIB 0.000012
        CB DOGE = 0.08
        """
        parsed = cb.parse_manual_basis_lines(text)
        self.assertAlmostEqual(parsed["Coinbase"]["ETH"], 1920.5)
        self.assertAlmostEqual(parsed["Robinhood"]["SHIB"], 0.000012)
        self.assertAlmostEqual(parsed["Coinbase"]["DOGE"], 0.08)

    def test_journal_matches_eth_usd_ticker(self):
        rows = [
            {
                "broker": "Coinbase",
                "side": "BUY",
                "ticker": "ETH-USD",
                "price": 2000.0,
                "qty": 1.0,
                "status": "Filled",
                "confirmed": True,
            }
        ]
        self.assertAlmostEqual(
            cb.inventory_vwap_from_journal(rows, "Coinbase", "ETH"), 2000.0
        )

    def test_persist_roundtrip(self):
        raw = {"Robinhood": {"DOGE": 0.12, "SHIB": 0.0}, "Coinbase": {"ETH": 2000}}
        norm = cb.normalize_cache_map(raw)
        self.assertAlmostEqual(norm["Robinhood"]["DOGE"], 0.12)
        self.assertNotIn("SHIB", norm["Robinhood"])
        self.assertAlmostEqual(norm["Coinbase"]["ETH"], 2000.0)
        out = cb.cache_to_persistable({"Robinhood": {"doge": 0.12, "x": -1}})
        self.assertEqual(out["Robinhood"]["DOGE"], 0.12)
        self.assertNotIn("X", out["Robinhood"])


class TestRthEquityPrefer(unittest.TestCase):
    def test_load_seed_file_skips_zero(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"ROBINHOOD": {"SHIB": 0.000015, "AMP": 0.0}}, f)
            path = f.name
        try:
            seeds = cb.load_seed_file(path)
            self.assertAlmostEqual(seeds["Robinhood"]["SHIB"], 0.000015)
            self.assertNotIn("AMP", seeds.get("Robinhood", {}))
        finally:
            import os
            os.unlink(path)

    def test_seed_lookup(self):
        seeds = {"Robinhood": {"DOGE": 0.08}}
        self.assertAlmostEqual(cb.seed_lookup("Robinhood", "DOGE", seeds), 0.08)
        self.assertEqual(cb.seed_lookup("Robinhood", "BTC", seeds), 0.0)

        from scoring import portfolio_buy_rank_adjust

        meta = [{"ticker": "SHIB", "value": 20.0, "is_crypto": True}]
        eq = portfolio_buy_rank_adjust(
            "AAPL",
            held_tickers={"SHIB"},
            holdings_meta=meta,
            portfolio_value=115.0,
            is_crypto=False,
            prefer_equity_rth=True,
        )
        cr = portfolio_buy_rank_adjust(
            "AVAX",
            held_tickers={"SHIB"},
            holdings_meta=meta,
            portfolio_value=115.0,
            is_crypto=True,
            prefer_equity_rth=True,
        )
        self.assertGreater(eq, cr)


if __name__ == "__main__":
    unittest.main()
