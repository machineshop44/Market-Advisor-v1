"""1.23.1 fix pack: regime gate helper, protective stop fractional/crypto N/A, RH error format."""
import os
import sys
import unittest
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestEntryRegimeOk(unittest.TestCase):
    def test_override_allows_when_blocked(self):
        import scoring
        with patch("scoring.market_regime_ok", return_value=(False, "DO NOT BUY (Regime: SPY sources disagree — blocked)")):
            ok, why = scoring.entry_regime_ok(is_crypto=False, allow_when_blocked=True)
        self.assertTrue(ok)
        self.assertIn("override", why)

    def test_equity_blocks_when_regime_fails(self):
        import scoring
        with patch("scoring.market_regime_ok", return_value=(False, "DO NOT BUY (Regime: SPY sources disagree — blocked)")):
            ok, why = scoring.entry_regime_ok(is_crypto=False, allow_when_blocked=False)
        self.assertFalse(ok)
        self.assertIn("disagree", why)

    def test_balanced_crypto_requires_btc_gate(self):
        import scoring
        with patch("scoring.crypto_turbulence_ok", return_value=(True, "")), \
             patch("scoring.market_regime_ok", return_value=(False, "blocked")):
            ok, why = scoring.entry_regime_ok(is_crypto=True, posture="balanced", allow_when_blocked=False)
        self.assertFalse(ok)
        self.assertEqual(why, "blocked")

    def test_safer_crypto_requires_btc_gate(self):
        import scoring
        with patch("scoring.crypto_turbulence_ok", return_value=(True, "")), \
             patch("scoring.market_regime_ok", return_value=(False, "DO NOT BUY (Regime: BTC 1H Downtrend)")):
            ok, why = scoring.entry_regime_ok(is_crypto=True, posture="safer", allow_when_blocked=False)
        self.assertFalse(ok)
        self.assertIn("BTC", why)

    def test_aggressive_crypto_skips_btc_gate(self):
        import scoring
        with patch("scoring.crypto_turbulence_ok", return_value=(True, "")), \
             patch("scoring.market_regime_ok", return_value=(False, "blocked")):
            ok, why = scoring.entry_regime_ok(is_crypto=True, posture="aggressive", allow_when_blocked=False)
        self.assertTrue(ok)
        self.assertEqual(why, "")


class TestProtectiveStopFractional(unittest.TestCase):
    def setUp(self):
        import scoring
        scoring._protective_orders = {bid: {} for bid in scoring._KNOWN_BROKER_IDS}

    def test_fractional_not_counted_missing(self):
        import scoring
        health = scoring.protective_stop_health([
            {"broker_id": "ROBINHOOD", "ticker": "AAPL", "value": 50.0, "shares": 0.37, "supports_protective": True},
            {"broker_id": "ROBINHOOD", "ticker": "MSFT", "value": 400.0, "shares": 2.0, "supports_protective": True},
        ])
        self.assertEqual(health["fractional_na_count"], 1)
        self.assertEqual(health["missing_count"], 1)
        self.assertEqual(health["missing"][0]["ticker"], "MSFT")
        self.assertIn("fractional", health["fractional_na"][0]["why"])

    def test_crypto_not_counted_missing(self):
        import scoring
        health = scoring.protective_stop_health([
            {"broker_id": "ROBINHOOD", "ticker": "BONK", "value": 12.0, "shares": 1000, "is_crypto": True, "supports_protective": True},
        ])
        self.assertEqual(health["missing_count"], 0)
        self.assertEqual(health["crypto_na_count"], 1)


class TestRhOrderErrorFormat(unittest.TestCase):
    def test_none_is_not_fail_none(self):
        from broker import RobinhoodAdapter
        msg = RobinhoodAdapter._format_rh_order_error(None, what="crypto sell BONK")
        self.assertNotEqual(msg.lower().strip(), "none")
        self.assertIn("empty response", msg.lower())
        self.assertIn("BONK", msg)

    def test_dict_detail_extracted(self):
        from broker import RobinhoodAdapter
        msg = RobinhoodAdapter._format_rh_order_error(
            {"detail": "quantity too small"}, what="crypto sell BONK"
        )
        self.assertIn("too small", msg)


class TestQtyWholeShares(unittest.TestCase):
    def test_scoring_helper(self):
        import scoring
        self.assertTrue(scoring._qty_is_whole_shares(2))
        self.assertTrue(scoring._qty_is_whole_shares(2.0))
        self.assertFalse(scoring._qty_is_whole_shares(0.37))
        self.assertFalse(scoring._qty_is_whole_shares(1.5))
        self.assertFalse(scoring._qty_is_whole_shares(0))


if __name__ == "__main__":
    unittest.main()
