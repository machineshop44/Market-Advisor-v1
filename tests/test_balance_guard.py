"""Tests for flaky equity wipe guards (false MAX DAILY LOSS)."""
import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestBalanceGuard(unittest.TestCase):
    def test_near_zero_vs_baseline_is_suspicious(self):
        from balance_guard import balance_reading_is_suspicious, is_near_zero_wipe

        self.assertTrue(balance_reading_is_suspicious(0.0, 87.16, 87.09))
        self.assertTrue(is_near_zero_wipe(0.0, 87.16, 87.09))
        self.assertFalse(balance_reading_is_suspicious(86.5, 87.16, 87.09))
        self.assertFalse(is_near_zero_wipe(0.0, 0.0, None))

    def test_near_zero_wipe_never_accepted(self):
        from balance_guard import decide_suspicious_equity

        # Reproduce 2026-08-03 log: two $0 reads must NOT become trusted wipe
        d1 = decide_suspicious_equity(0.0, 87.16, 87.09, last_trusted=87.16, bad_streak=0)
        self.assertEqual(d1["action"], "keep")
        self.assertFalse(d1["trusted"])
        d2 = decide_suspicious_equity(0.0, 87.16, 87.09, last_trusted=87.16, bad_streak=d1["streak"])
        self.assertEqual(d2["action"], "keep")
        self.assertFalse(d2["trusted"])
        # Even after many streaks — still keep (near-zero vs substantial baseline)
        d_many = decide_suspicious_equity(
            0.0, 0.0, 87.09, last_trusted=87.16, bad_streak=10, holdings_count=0
        )
        self.assertEqual(d_many["action"], "keep")
        self.assertFalse(d_many["trusted"])

    def test_holdings_block_collapse(self):
        from balance_guard import decide_suspicious_equity

        d = decide_suspicious_equity(
            0.0, 87.16, 87.09, last_trusted=87.16, holdings_count=3, bad_streak=5
        )
        self.assertEqual(d["action"], "keep")
        self.assertIn("holding", d["reason"].lower())

    def test_large_non_zero_needs_multiple_confirms(self):
        from balance_guard import decide_suspicious_equity, LARGE_COLLAPSE_CONFIRM_READS

        # $100 → $20 is a large collapse but not near-zero wipe
        d1 = decide_suspicious_equity(20.0, 100.0, 100.0, last_trusted=100.0, bad_streak=0)
        self.assertEqual(d1["action"], "keep")
        streak = d1["streak"]
        for _ in range(LARGE_COLLAPSE_CONFIRM_READS - 2):
            d = decide_suspicious_equity(
                20.0, 100.0, 100.0, last_trusted=100.0, bad_streak=streak
            )
            self.assertEqual(d["action"], "keep")
            streak = d["streak"]
        d_ok = decide_suspicious_equity(
            20.0, 100.0, 100.0, last_trusted=100.0, bad_streak=streak
        )
        self.assertEqual(d_ok["action"], "accept")
        self.assertTrue(d_ok["trusted"])


class TestEtradeHoldingsShape(unittest.TestCase):
    def test_normalize_returns_list_not_dict(self):
        from etrade_broker import normalize_etrade_holdings, parse_etrade_balances, parse_etrade_quote_price

        payload = {
            "PortfolioResponse": {
                "AccountPortfolio": {
                    "Position": {
                        "Product": {"symbol": "SPY"},
                        "quantity": "2",
                        "Quick": {"lastTrade": "500"},
                        "costPerShare": "490",
                        "marketValue": "1000",
                    }
                }
            }
        }
        holdings = normalize_etrade_holdings(payload)
        self.assertIsInstance(holdings, list)
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["ticker"], "SPY")
        self.assertEqual(holdings[0]["shares"], 2.0)

        # GUI-style iteration must not TypeError
        for a in holdings:
            self.assertIn("ticker", a)
            _ = a["ticker"]

    def test_malformed_xml_leaves_do_not_explode(self):
        from etrade_broker import normalize_etrade_holdings, parse_etrade_balances, parse_etrade_quote_price

        # XML parser sometimes yields strings instead of nested dicts
        self.assertEqual(normalize_etrade_holdings({"PortfolioResponse": "oops"}), [])
        self.assertEqual(parse_etrade_balances({"BalanceResponse": "oops"}), (0.0, 0.0))
        self.assertEqual(parse_etrade_quote_price({"QuoteResponse": "oops"}), 0.0)
        self.assertEqual(parse_etrade_quote_price("not-a-dict"), 0.0)

        bal = {
            "BalanceResponse": {
                "Computed": {"CashBuyingPower": "41.25"},
                "RealTimeValues": {"totalAccountValue": "87.16"},
            }
        }
        eq, bp = parse_etrade_balances(bal)
        self.assertAlmostEqual(eq, 87.16)
        self.assertAlmostEqual(bp, 41.25)

        # Sandbox-style aliases (settledCash / marginBuyingPower)
        sandbox = {
            "BalanceResponse": {
                "Computed": {"settledCash": "12.50", "MarginBuyingPower": "0"},
                "RealTimeValues": {"totalAccountValue": "12.50"},
            }
        }
        eq2, bp2 = parse_etrade_balances(sandbox)
        self.assertAlmostEqual(eq2, 12.50)
        self.assertAlmostEqual(bp2, 12.50)

        cash_nested = {
            "BalanceResponse": {
                "Cash": {"fundsForTrading": "33.00"},
                "RealTimeValues": {"totalAccountValue": "40.00"},
            }
        }
        eq3, bp3 = parse_etrade_balances(cash_nested)
        self.assertAlmostEqual(eq3, 40.00)
        self.assertAlmostEqual(bp3, 33.00)

    def test_adapter_holdings_are_list(self):
        from etrade_broker import ETradeAdapter
        from unittest.mock import MagicMock

        et = ETradeAdapter()
        et.is_connected = True
        et.account_id_key = "acct"
        et.client = MagicMock()
        et.client.get_portfolio.return_value = {
            "PortfolioResponse": {
                "AccountPortfolio": {
                    "Position": [
                        {"Product": {"symbol": "AAPL"}, "quantity": 1, "price": 100, "costPerShare": 90},
                        {"Product": {"symbol": "MSFT"}, "quantity": 2, "price": 200, "costPerShare": 180},
                    ]
                }
            }
        }
        holdings = et.get_current_holdings()
        self.assertIsInstance(holdings, list)
        tickers = {h["ticker"] for h in holdings}
        self.assertEqual(tickers, {"AAPL", "MSFT"})


    def test_product_string_leaf(self):
        from etrade_broker import normalize_etrade_holdings

        payload = {
            "PortfolioResponse": {
                "AccountPortfolio": {
                    "Position": {"Product": "SPY", "quantity": "1", "price": "100"}
                }
            }
        }
        holdings = normalize_etrade_holdings(payload)
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["ticker"], "SPY")


class TestKeepEquityFloor(unittest.TestCase):
    def test_reference_prefers_baseline_when_painted_zero(self):
        from balance_guard import reference_equity

        # After a false $0 accept, old_p is 0 but day baseline remains
        self.assertAlmostEqual(reference_equity(0.0, 87.09, None), 87.09)
        self.assertAlmostEqual(reference_equity(0.0, 87.09, 86.5), 87.09)


class TestDayLossTripConfirm(unittest.TestCase):
    """Reproduce 2026-08-04: heartbeat +$1.15 then single-read −$9.43 halt."""

    def test_moderate_underread_that_trips_limit_is_suspicious(self):
        from balance_guard import (
            balance_reading_is_suspicious,
            day_loss_trip_is_suspicious,
        )

        baseline = 87.66082026534956
        last = 88.81  # heartbeat equity / +$1.15
        glitch = baseline - 9.43  # ~78.23 — Loss: -$9.43 in Discord
        self.assertTrue(
            day_loss_trip_is_suspicious(glitch, last, baseline, last, loss_limit=8.0)
        )
        self.assertTrue(
            balance_reading_is_suspicious(glitch, last, baseline, last, loss_limit=8.0)
        )
        # Same print without a loss limit is not this class of suspicion
        self.assertFalse(
            day_loss_trip_is_suspicious(glitch, last, baseline, last, loss_limit=0.0)
        )

    def test_day_loss_trip_needs_three_confirms(self):
        from balance_guard import DAY_LOSS_TRIP_CONFIRM_READS, decide_suspicious_equity

        baseline = 87.66
        last = 88.81
        glitch = 78.23
        streak = 0
        for i in range(DAY_LOSS_TRIP_CONFIRM_READS - 1):
            d = decide_suspicious_equity(
                glitch,
                last,
                baseline,
                last_trusted=last,
                bad_streak=streak,
                loss_limit=8.0,
            )
            self.assertEqual(d["action"], "keep", msg=f"read {i+1}")
            self.assertFalse(d["trusted"])
            self.assertIn("day-loss trip", d["reason"].lower())
            streak = d["streak"]
        d_ok = decide_suspicious_equity(
            glitch,
            last,
            baseline,
            last_trusted=last,
            bad_streak=streak,
            loss_limit=8.0,
        )
        self.assertEqual(d_ok["action"], "accept")
        self.assertTrue(d_ok["trusted"])

    def test_gradual_tick_across_limit_still_halts(self):
        """Real grind to the limit: tiny drop across −$8 must not need confirms."""
        from balance_guard import day_loss_trip_is_suspicious

        baseline = 100.0
        # Already −$7.95 on last good; one more $0.10 tick → −$8.05
        self.assertFalse(
            day_loss_trip_is_suspicious(
                91.95, 92.05, baseline, last_trusted=92.05, loss_limit=8.0
            )
        )

    def test_already_past_limit_does_not_delay(self):
        from balance_guard import day_loss_trip_is_suspicious

        baseline = 100.0
        # Last trusted already below limit; further drop is not a "new" trip
        self.assertFalse(
            day_loss_trip_is_suspicious(
                80.0, 90.0, baseline, last_trusted=90.0, loss_limit=8.0
            )
        )


    def test_holdings_do_not_block_day_loss_trip_confirm(self):
        from balance_guard import decide_suspicious_equity

        # Real mark-to-market while still holding names must be confirmable
        d1 = decide_suspicious_equity(
            78.23,
            88.81,
            87.66,
            last_trusted=88.81,
            holdings_count=4,
            bad_streak=0,
            loss_limit=8.0,
        )
        self.assertEqual(d1["action"], "keep")
        self.assertIn("day-loss trip", d1["reason"].lower())
        d3 = decide_suspicious_equity(
            78.23,
            88.81,
            87.66,
            last_trusted=88.81,
            holdings_count=4,
            bad_streak=2,
            loss_limit=8.0,
        )
        self.assertEqual(d3["action"], "accept")
        self.assertTrue(d3["trusted"])


if __name__ == "__main__":
    unittest.main()
