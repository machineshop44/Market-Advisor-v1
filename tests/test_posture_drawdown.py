"""Drawdown pause + posture scale-in band alignment."""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import scoring  # noqa: E402


class PostureDrawdownTests(unittest.TestCase):
    def setUp(self):
        scoring._equity_dd["ROBINHOOD"] = {
            "day": "",
            "day_open": 0.0,
            "peak": 0.0,
            "pause_until": 0.0,
            "pause_reason": "",
            "peak_dd_streak": 0,
        }

    def test_scale_in_bands_above_hard_stop(self):
        bal = scoring.get_scale_in_params("balanced")
        agg = scoring.get_scale_in_params("aggressive")
        # Both bands must sit above typical stock stop floor (~-3.0%)
        self.assertGreater(bal["scale_in_roi_min"], -0.035)
        self.assertGreater(agg["scale_in_roi_min"], -0.035)
        # Aggressive allows deeper / wider adds than balanced
        self.assertLess(agg["scale_in_roi_min"], bal["scale_in_roi_min"])
        self.assertGreater(agg["scale_in_roi_max"], bal["scale_in_roi_max"])
        self.assertEqual(scoring.get_risk_posture_profile("aggressive")["max_single_name_equity_pct"], 20.0)

    def test_day_drawdown_triggers_pause(self):
        scoring.update_equity_drawdown("ROBINHOOD", 10000.0, posture="balanced")
        paused, msg = scoring.update_equity_drawdown(
            "ROBINHOOD", 9400.0, posture="balanced"  # -6% > 5% day pause
        )
        self.assertTrue(paused)
        self.assertIn("Day drawdown", msg)
        allowed, reason = scoring._drawdown_block("ROBINHOOD")
        self.assertFalse(allowed)
        self.assertIn("DO NOT BUY", reason)

    def test_safer_tighter_day_dd(self):
        scoring._equity_dd["ROBINHOOD"] = {
            "day": "", "day_open": 0.0, "peak": 0.0,
            "pause_until": 0.0, "pause_reason": "", "peak_dd_streak": 0,
        }
        scoring.update_equity_drawdown("ROBINHOOD", 10000.0, posture="safer")
        paused, _ = scoring.update_equity_drawdown("ROBINHOOD", 9650.0, posture="safer")  # -3.5%
        self.assertTrue(paused)


if __name__ == "__main__":
    unittest.main()
