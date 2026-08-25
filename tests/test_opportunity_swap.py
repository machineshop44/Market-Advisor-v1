"""Unit tests for opportunity-swap / capital rotation picker."""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import scoring  # noqa: E402


def _seed_hold(broker_id, ticker, *, buy_age_min=120.0, highest=None, price=100.0):
    bid = scoring._normalize_broker_id(broker_id)
    scoring._portfolio_memory.setdefault(bid, {})
    scoring._portfolio_memory[bid][str(ticker).upper()] = {
        "highest": float(highest if highest is not None else price),
        "buy_time": time.time() - float(buy_age_min) * 60.0,
        "last_eval": time.time(),
    }


def _clear_mem():
    for bid in list(scoring._portfolio_memory.keys()):
        scoring._portfolio_memory[bid] = {}


class OpportunitySwapTests(unittest.TestCase):
    def tearDown(self):
        _clear_mem()

    def test_safer_posture_disabled(self):
        p = scoring.opportunity_swap_params("safer")
        self.assertFalse(p["enabled"])
        self.assertFalse(scoring.opportunity_swap_enabled("safer"))
        self.assertIsNone(
            scoring.pick_rotation_funding(
                "NVDA",
                90,
                False,
                [
                    {
                        "ticker": "AAPL",
                        "price": 100,
                        "avg_cost": 100,
                        "value": 500,
                        "is_crypto": False,
                        "shares": 5,
                    }
                ],
                posture="safer",
                broker_id="ROBINHOOD",
                score_fn=lambda t, is_crypto=False: 40.0,
                skip_regime_check=True,
            )
        )

    def test_roi_floor_rejects_deep_red(self):
        _seed_hold("ROBINHOOD", "AAPL", buy_age_min=200, price=90)
        fund = scoring.pick_rotation_funding(
            "NVDA",
            80,
            False,
            [
                {
                    "ticker": "AAPL",
                    "price": 95.0,
                    "avg_cost": 100.0,
                    "value": 500,
                    "is_crypto": False,
                    "shares": 5,
                }
            ],
            posture="balanced",
            broker_id="ROBINHOOD",
            block_reason="low_bp",
            score_fn=lambda t, is_crypto=False: 40.0 if t == "AAPL" else 80.0,
            skip_regime_check=True,
        )
        self.assertIsNone(fund)

    def test_score_gap_required(self):
        _seed_hold("ROBINHOOD", "AAPL", buy_age_min=200, price=100)
        fund = scoring.pick_rotation_funding(
            "NVDA",
            45,
            False,
            [
                {
                    "ticker": "AAPL",
                    "price": 100.0,
                    "avg_cost": 100.0,
                    "value": 500,
                    "is_crypto": False,
                    "shares": 5,
                }
            ],
            posture="balanced",
            broker_id="ROBINHOOD",
            score_fn=lambda t, is_crypto=False: 40.0,
            skip_regime_check=True,
        )
        self.assertIsNone(fund)
        fund2 = scoring.pick_rotation_funding(
            "NVDA",
            55,
            False,
            [
                {
                    "ticker": "AAPL",
                    "price": 100.0,
                    "avg_cost": 100.0,
                    "value": 500,
                    "is_crypto": False,
                    "shares": 5,
                }
            ],
            posture="balanced",
            broker_id="ROBINHOOD",
            score_fn=lambda t, is_crypto=False: 40.0,
            skip_regime_check=True,
        )
        self.assertIsNotNone(fund2)
        self.assertEqual(fund2["ticker"], "AAPL")

    def test_ttp_armed_veto(self):
        _seed_hold("ROBINHOOD", "AAPL", buy_age_min=200, highest=110.0, price=108.0)
        fund = scoring.pick_rotation_funding(
            "NVDA",
            90,
            False,
            [
                {
                    "ticker": "AAPL",
                    "price": 108.0,
                    "avg_cost": 100.0,
                    "value": 500,
                    "is_crypto": False,
                    "shares": 5,
                }
            ],
            posture="aggressive",
            broker_id="ROBINHOOD",
            score_fn=lambda t, is_crypto=False: 30.0,
            skip_regime_check=True,
        )
        self.assertIsNone(fund)

    def test_min_hold_blocks_fresh(self):
        _seed_hold("ROBINHOOD", "AAPL", buy_age_min=10, price=100)
        fund = scoring.pick_rotation_funding(
            "NVDA",
            90,
            False,
            [
                {
                    "ticker": "AAPL",
                    "price": 100.0,
                    "avg_cost": 100.0,
                    "value": 500,
                    "is_crypto": False,
                    "shares": 5,
                }
            ],
            posture="aggressive",
            broker_id="ROBINHOOD",
            score_fn=lambda t, is_crypto=False: 20.0,
            skip_regime_check=True,
        )
        self.assertIsNone(fund)

    def test_cluster_swap_prefers_cluster_member(self):
        _seed_hold("ROBINHOOD", "ETH", buy_age_min=120, price=2000)
        _seed_hold("ROBINHOOD", "AAPL", buy_age_min=200, price=100)
        holdings = [
            {
                "ticker": "ETH",
                "price": 2000.0,
                "avg_cost": 2000.0,
                "value": 400,
                "is_crypto": True,
                "shares": 0.2,
            },
            {
                "ticker": "AAPL",
                "price": 100.0,
                "avg_cost": 100.0,
                "value": 500,
                "is_crypto": False,
                "shares": 5,
            },
        ]
        fund = scoring.pick_rotation_funding(
            "SOL",
            90,
            True,
            holdings,
            posture="aggressive",
            broker_id="ROBINHOOD",
            block_reason="cluster BTC_BETA full (BTC, ETH)",
            score_fn=lambda t, is_crypto=False: 25.0,
            skip_regime_check=True,
        )
        self.assertIsNotNone(fund)
        self.assertEqual(fund["ticker"], "ETH")

    def test_mark_opportunity_swap_exit(self):
        _seed_hold("ROBINHOOD", "SOL", buy_age_min=60, price=100)
        scoring.mark_opportunity_swap_exit("ROBINHOOD", "SOL")
        mem = scoring._holding_mem("ROBINHOOD", "SOL")
        self.assertEqual(mem.get("exit_reason"), "opportunity_swap")

    def test_aggressive_allows_deeper_roi_than_balanced(self):
        _seed_hold("ROBINHOOD", "AAPL", buy_age_min=200, price=98)
        h = [
            {
                "ticker": "AAPL",
                "price": 98.0,
                "avg_cost": 100.0,
                "value": 500,
                "is_crypto": False,
                "shares": 5,
            }
        ]
        self.assertIsNone(
            scoring.pick_rotation_funding(
                "NVDA",
                90,
                False,
                h,
                posture="balanced",
                broker_id="ROBINHOOD",
                score_fn=lambda t, is_crypto=False: 20.0,
                skip_regime_check=True,
            )
        )
        fund = scoring.pick_rotation_funding(
            "NVDA",
            90,
            False,
            h,
            posture="aggressive",
            broker_id="ROBINHOOD",
            score_fn=lambda t, is_crypto=False: 20.0,
            skip_regime_check=True,
        )
        self.assertIsNotNone(fund)


class RotateToClearFloorTests(unittest.TestCase):
    """Desk/algo parity: rotate to clear RH crypto floor — never lower the floor."""

    def tearDown(self):
        _clear_mem()
        scoring._rotate_day_counts.clear()

    def test_broker_min_notional_rh_crypto(self):
        self.assertAlmostEqual(scoring.RH_CRYPTO_MIN_NOTIONAL, 5.0)
        self.assertAlmostEqual(
            scoring.broker_min_notional("ROBINHOOD", is_crypto=True), 5.0
        )
        self.assertGreaterEqual(
            scoring.broker_min_notional("ROBINHOOD", is_crypto=True), 5.0
        )

    def test_under_floor_bp_rotates_when_funder_clears(self):
        """BP $4.35 + stronger XLM vs weak hold → pick funder that frees ≥ shortfall."""
        scoring._rotate_day_counts.clear()
        # Small green ROI so TTP is not armed (still ≥ aggressive roi_floor)
        _seed_hold("ROBINHOOD", "DOGE", buy_age_min=200, price=0.121)
        fund = scoring.pick_rotation_funding(
            "XLM",
            90,
            True,
            [
                {
                    "ticker": "DOGE",
                    "price": 0.121,
                    "avg_cost": 0.12,
                    "value": 40.0,
                    "is_crypto": True,
                    "shares": 330.0,
                    "asset_type": "cryptocurrency",
                }
            ],
            posture="aggressive",
            broker_id="ROBINHOOD",
            block_reason="rh_crypto_floor",
            need_dollars=5.0,
            current_bp=4.35,
            score_fn=lambda t, is_crypto=False: 20.0 if t == "DOGE" else 90.0,
            skip_regime_check=True,
        )
        self.assertIsNotNone(fund)
        self.assertEqual(fund["ticker"], "DOGE")
        self.assertTrue(fund.get("clears_shortfall"))
        self.assertGreaterEqual(float(fund["value"]), 5.0 - 4.35)

    def test_no_funder_large_enough_rejects(self):
        """Eligible-on-gates but tiny hold cannot free enough for floor."""
        scoring._rotate_day_counts.clear()
        _seed_hold("ROBINHOOD", "DOGE", buy_age_min=200, price=0.121)
        fund = scoring.pick_rotation_funding(
            "XLM",
            90,
            True,
            [
                {
                    "ticker": "DOGE",
                    "price": 0.121,
                    "avg_cost": 0.12,
                    "value": 0.40,
                    "is_crypto": True,
                    "shares": 3.0,
                    "asset_type": "cryptocurrency",
                }
            ],
            posture="aggressive",
            broker_id="ROBINHOOD",
            block_reason="rh_crypto_floor",
            need_dollars=5.0,
            current_bp=4.35,
            score_fn=lambda t, is_crypto=False: 20.0 if t == "DOGE" else 90.0,
            skip_regime_check=True,
        )
        self.assertIsNone(fund)
        why = scoring.last_rotation_reject_reason().lower()
        self.assertIn("floor", why)
        self.assertIn("funder", why)

    def test_never_implies_sub_five_ticket(self):
        """Floor constant stays ≥ $5 — we do not lower broker min to fit BP."""
        self.assertGreaterEqual(scoring.RH_CRYPTO_MIN_NOTIONAL, 5.0)
        scoring._rotate_day_counts.clear()
        _seed_hold("ROBINHOOD", "ETH", buy_age_min=200, price=2020)
        fund = scoring.pick_rotation_funding(
            "SOL",
            90,
            True,
            [
                {
                    "ticker": "ETH",
                    "price": 2020.0,
                    "avg_cost": 2000.0,
                    "value": 202.0,
                    "is_crypto": True,
                    "shares": 0.1,
                    "asset_type": "cryptocurrency",
                }
            ],
            posture="aggressive",
            broker_id="ROBINHOOD",
            need_dollars=scoring.RH_CRYPTO_MIN_NOTIONAL,
            current_bp=3.0,
            score_fn=lambda t, is_crypto=False: 20.0 if t == "ETH" else 90.0,
            skip_regime_check=True,
        )
        self.assertIsNotNone(fund)
        self.assertGreaterEqual(
            3.0 + float(fund["value"]), scoring.RH_CRYPTO_MIN_NOTIONAL
        )


if __name__ == "__main__":
    unittest.main()
