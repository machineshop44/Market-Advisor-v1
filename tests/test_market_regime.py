"""market_regime_ok fail-closed / failover paths."""
import os
import sys
import time
import unittest
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestMarketRegimeFailClosed(unittest.TestCase):
    def setUp(self):
        import scoring
        scoring._regime_last_good = {}
        scoring._broker_price_samples = {}
        scoring._broker_hourly_closes = {}
        scoring._broker_last_sample_ts = {}

    def test_yahoo_fail_broker_fail_no_last_good_blocks(self):
        import scoring
        with patch("scoring._yahoo_regime_vote", return_value=(False, False, None, "yahoo error")), \
             patch("scoring._broker_regime_vote", return_value=(False, False, "broker disconnected/no quote")), \
             patch("scoring.save_state"):
            ok, why = scoring.market_regime_ok(is_crypto=False)
        self.assertFalse(ok)
        self.assertIn("unavailable", why.lower())
        self.assertIn("DO NOT BUY", why)

    def test_yahoo_fail_uses_last_good_ok(self):
        import scoring
        scoring._regime_last_good["SPY"] = {
            "ok": True,
            "ts": time.time(),
            "source": "yahoo",
        }
        with patch("scoring._yahoo_regime_vote", return_value=(False, False, None, "yahoo empty")), \
             patch("scoring._broker_regime_vote", return_value=(False, False, "broker disconnected/no quote")), \
             patch("scoring.save_state"):
            ok, why = scoring.market_regime_ok(is_crypto=False)
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_yahoo_fail_uses_last_good_downtrend(self):
        import scoring
        scoring._regime_last_good["BTC-USD"] = {
            "ok": False,
            "ts": time.time(),
            "source": "yahoo+broker",
        }
        with patch("scoring._yahoo_regime_vote", return_value=(False, False, None, "yahoo error")), \
             patch("scoring._broker_regime_vote", return_value=(False, False, "broker disconnected/no quote")), \
             patch("scoring.save_state"):
            ok, why = scoring.market_regime_ok(is_crypto=True)
        self.assertFalse(ok)
        self.assertIn("last-good", why.lower())
        self.assertIn("DO NOT BUY", why)

    def test_expired_last_good_fail_closed(self):
        import scoring
        scoring._regime_last_good["SPY"] = {
            "ok": True,
            "ts": time.time() - (scoring.REGIME_LAST_GOOD_TTL + 60),
            "source": "yahoo",
        }
        with patch("scoring._yahoo_regime_vote", return_value=(False, False, None, "yahoo error")), \
             patch("scoring._broker_regime_vote", return_value=(False, False, "broker disconnected/no quote")), \
             patch("scoring.save_state"):
            ok, why = scoring.market_regime_ok(is_crypto=False)
        self.assertFalse(ok)
        self.assertIn("unavailable", why.lower())

    def test_broker_alone_ok_allows(self):
        import scoring
        with patch("scoring._yahoo_regime_vote", return_value=(False, False, None, "yahoo empty")), \
             patch("scoring._broker_regime_vote", return_value=(True, True, "broker hourly EMA")), \
             patch("scoring.save_state"):
            ok, why = scoring.market_regime_ok(is_crypto=False)
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_broker_alone_down_blocks(self):
        import scoring
        with patch("scoring._yahoo_regime_vote", return_value=(False, False, None, "yahoo empty")), \
             patch("scoring._broker_regime_vote", return_value=(True, False, "broker trend down")), \
             patch("scoring.save_state"):
            ok, why = scoring.market_regime_ok(is_crypto=False)
        self.assertFalse(ok)
        self.assertIn("DO NOT BUY", why)

    def test_sources_disagree_fail_closed(self):
        import scoring
        with patch("scoring._yahoo_regime_vote", return_value=(True, True, 100.0, "yahoo 1H")), \
             patch("scoring._broker_regime_vote", return_value=(True, False, "broker live vs yahoo EMA")), \
             patch("scoring.save_state"):
            ok, why = scoring.market_regime_ok(is_crypto=False)
        self.assertFalse(ok)
        self.assertIn("disagree", why.lower())


class TestBtcProxyRegime(unittest.TestCase):
    def test_uses_btc_regime_helpers(self):
        import scoring
        self.assertTrue(scoring.uses_btc_regime("BTC", True))
        self.assertTrue(scoring.uses_btc_regime("ETH", False))
        self.assertTrue(scoring.uses_btc_regime("IBIT", False))
        self.assertTrue(scoring.uses_btc_regime("MSTR", False))
        self.assertFalse(scoring.uses_btc_regime("AAPL", False))
        self.assertFalse(scoring.uses_btc_regime("SPY", False))

    def test_entry_regime_ibit_votes_btc_not_spy(self):
        import scoring
        with patch("scoring.crypto_turbulence_ok", return_value=(True, "")), \
             patch("scoring.market_regime_ok", return_value=(True, "btc ok")) as mreg:
            ok, why = scoring.entry_regime_ok(
                is_crypto=False, posture="balanced", ticker="IBIT",
            )
        self.assertTrue(ok)
        mreg.assert_called_once_with(is_crypto=True)
        self.assertEqual(why, "btc ok")

    def test_entry_regime_aapl_votes_spy(self):
        import scoring
        with patch("scoring.market_regime_ok", return_value=(True, "spy ok")) as mreg:
            ok, why = scoring.entry_regime_ok(
                is_crypto=False, posture="balanced", ticker="AAPL",
            )
        self.assertTrue(ok)
        mreg.assert_called_once_with(is_crypto=False)
        self.assertEqual(why, "spy ok")

    def test_entry_regime_growth_skips_spy_gate(self):
        import scoring
        with patch("scoring.market_regime_ok", return_value=(False, "DO NOT BUY (Regime: SPY 1H Downtrend)")) as mreg:
            ok, why = scoring.entry_regime_ok(
                is_crypto=False, posture="growth", ticker="AAPL",
            )
        self.assertTrue(ok)
        mreg.assert_not_called()
        self.assertEqual(why, "")

    def test_small_book_prefers_breakouts(self):
        import scoring
        self.assertTrue(scoring.small_book_prefers_breakouts(120.0, {}))
        self.assertTrue(scoring.small_book_prefers_breakouts(800.0, {"risk_posture": "growth"}))
        self.assertFalse(scoring.small_book_prefers_breakouts(800.0, {"risk_posture": "balanced"}))


if __name__ == "__main__":
    unittest.main()
