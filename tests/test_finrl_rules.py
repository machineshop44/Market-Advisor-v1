"""FinRL-derived rule gates: hold bias, turbulence, net-of-cost, scale-in mute."""
import os
import sys
import time
import unittest
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestNetOfCostHelpers(unittest.TestCase):
    def test_net_roi_subtracts_rt_fees(self):
        import scoring
        gross = 0.05
        net = scoring.net_roi_after_fees(gross, "ROBINHOOD", "BTC", "cryptocurrency")
        rt = scoring.estimate_round_trip_fee_pct("ROBINHOOD", "BTC", "cryptocurrency")
        self.assertAlmostEqual(net, gross - rt)
        self.assertLess(net, gross)

    def test_min_entry_edge_includes_buffer(self):
        import scoring
        need = scoring.min_entry_edge_pct("COINBASE", "ETH", "cryptocurrency")
        rt = scoring.estimate_round_trip_fee_pct("COINBASE", "ETH", "cryptocurrency")
        self.assertAlmostEqual(need, rt + scoring.MIN_ENTRY_EDGE_OVER_FEES_PCT)
        self.assertGreaterEqual(need, 0.034)  # CB ~2.4% RT + 1%


class TestCryptoTurbulenceAndRegime(unittest.TestCase):
    def test_turbulence_blocks_elevated_atr(self):
        import scoring
        with patch("scoring._atr_pct", return_value=0.04):
            ok, why = scoring.crypto_turbulence_ok()
        self.assertFalse(ok)
        self.assertIn("Turbulence", why)

    def test_turbulence_allows_calm(self):
        import scoring
        with patch("scoring._atr_pct", return_value=0.01):
            ok, why = scoring.crypto_turbulence_ok()
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_balanced_crypto_requires_btc_regime(self):
        import scoring
        with patch("scoring.crypto_turbulence_ok", return_value=(True, "")), \
             patch("scoring.market_regime_ok", return_value=(False, "DO NOT BUY (Regime: BTC 1H Downtrend)")):
            ok, why = scoring.entry_regime_ok(
                is_crypto=True, posture="balanced", allow_when_blocked=False
            )
        self.assertFalse(ok)
        self.assertIn("BTC", why)

    def test_aggressive_crypto_skips_btc_regime_but_not_turbulence(self):
        import scoring
        with patch("scoring.crypto_turbulence_ok", return_value=(True, "")), \
             patch("scoring.market_regime_ok", return_value=(False, "blocked")):
            ok, why = scoring.entry_regime_ok(
                is_crypto=True, posture="aggressive", allow_when_blocked=False
            )
        self.assertTrue(ok)
        with patch("scoring.crypto_turbulence_ok", return_value=(False, "DO NOT BUY (Turbulence: BTC ATR 4.0% elevated — pause new crypto)")):
            ok2, why2 = scoring.entry_regime_ok(
                is_crypto=True, posture="aggressive", allow_when_blocked=False
            )
        self.assertFalse(ok2)
        self.assertIn("Turbulence", why2)


class TestCryptoHoldBias(unittest.TestCase):
    def test_weak_score_blocked(self):
        import scoring
        ok, why = scoring.crypto_new_entry_ok(
            "ROBINHOOD", "SOL", score=40.0, notional=20.0, skip_turbulence=True
        )
        self.assertFalse(ok)
        self.assertIn("Hold bias", why)

    def test_thin_ticket_needs_strong_score(self):
        import scoring
        # Score passes floor but not thin-ticket bar
        ok, why = scoring.crypto_new_entry_ok(
            "ROBINHOOD", "SOL", score=60.0, notional=5.0, skip_turbulence=True
        )
        self.assertFalse(ok)
        self.assertIn("Thin", why)

    def test_strong_thin_ticket_clears_when_edge_ok(self):
        import scoring
        # score 80 → edge (80-40)*0.001 = 4.0%; RH crypto need ≈ 2.9%
        ok, why = scoring.crypto_new_entry_ok(
            "ROBINHOOD", "SOL", score=80.0, notional=5.0, skip_turbulence=True
        )
        self.assertTrue(ok, why)
        self.assertEqual(why, "")

    def test_crypto_cooldown_lengthened(self):
        import scoring
        self.assertGreaterEqual(scoring.CRYPTO_COOLDOWN, 20 * 60)
        self.assertGreaterEqual(scoring.CRYPTO_TRADE_LOCK_SEC, 600)
        self.assertEqual(scoring.trade_lock_seconds(True), scoring.CRYPTO_TRADE_LOCK_SEC)
        self.assertEqual(scoring.trade_lock_seconds(False), scoring.STOCK_TRADE_LOCK_SEC)

    def test_posture_max_buys_tightened(self):
        import scoring
        self.assertEqual(scoring.get_risk_posture_profile("safer")["max_buys_per_cycle"], 1)
        self.assertEqual(scoring.get_risk_posture_profile("balanced")["max_buys_per_cycle"], 1)
        self.assertTrue(scoring.crypto_regime_required("balanced"))


class TestScaleInMute(unittest.TestCase):
    def setUp(self):
        import scoring
        for bid in scoring._KNOWN_BROKER_IDS:
            scoring._scale_in_counts[bid] = {}
            scoring._scale_in_last_ts[bid] = {}

    def test_repeat_scale_in_cooldown(self):
        import scoring
        scoring._scale_in_last_ts["ROBINHOOD"]["AAPL"] = time.time()
        with patch("scoring.find_support_revisit", return_value=(True, 100.0, "near support")), \
             patch("scoring.buy_rank_score", return_value=80.0), \
             patch("scoring.get_stop_distance_pct", return_value=0.035):
            ev = scoring.evaluate_scale_in(
                "AAPL", 98.0, 100.0, broker_id="ROBINHOOD",
                asset_type="stock", is_crypto=False, signal_score=80.0,
                posture="balanced", settings={"allow_scale_in": True},
            )
        self.assertFalse(ev["allowed"])
        self.assertIn("scale-in cooldown", ev["reason"])


class TestRotateHoldBias(unittest.TestCase):
    def test_balanced_fewer_rotates(self):
        import scoring
        p = scoring.opportunity_swap_params("balanced")
        self.assertEqual(p["max_rotates_per_day"], 1)
        self.assertGreaterEqual(p["min_hold_crypto_min"], 90.0)
        self.assertGreaterEqual(p["fee_buffer_pct"], 0.015)


class TestVersion(unittest.TestCase):
    def test_version_patched(self):
        import version
        parts = [int(x) for x in str(version.__version__).split(".")[:3]]
        self.assertGreaterEqual(tuple(parts + [0, 0, 0])[:3], (1, 30, 0))


if __name__ == "__main__":
    unittest.main()
