"""ATR exits + portfolio heat snapshot tests."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import scoring  # noqa: E402


class AtrHeatTests(unittest.TestCase):
    def test_atr_adapt_widens_hard_stop(self):
        fees = {
            "hard_stop": -0.035,
            "ttp_trail": 0.010,
            "ttp_arm": 0.020,
            "time_30m_target": 0.035,
            "time_60m_target": 0.030,
            "time_green_roi": 0.025,
        }
        with patch("scoring._atr_pct", return_value=0.04):
            out = scoring.atr_adapt_exit_fees(fees, "AAPL")
        self.assertLess(out["hard_stop"], -0.035)  # more negative = wider
        self.assertGreaterEqual(abs(out["hard_stop"]), 0.035)
        self.assertLessEqual(abs(out["hard_stop"]), 0.035 * scoring.ATR_SIZING_CAP_MULT)
        # Time banks must scale with arm so ATR cannot reintroduce ~1% jump-ships
        scale = abs(out["hard_stop"]) / 0.035
        self.assertAlmostEqual(out["ttp_arm"], 0.020 * scale, places=6)
        self.assertAlmostEqual(out["time_green_roi"], 0.025 * scale, places=6)
        self.assertGreater(out["time_green_roi"], out["ttp_arm"])
        # resolve() also clamps flat banks ≥ arm × FLAT_TIME_BANK_ARM_MULT
        with patch("scoring._atr_pct", return_value=None):
            resolved = scoring.resolve_exit_fees("ROBINHOOD", "AAPL", "stock")
        self.assertGreaterEqual(
            float(resolved["time_green_roi"]),
            float(resolved["ttp_arm"]) * scoring.FLAT_TIME_BANK_ARM_MULT - 1e-12,
        )

    def test_get_stop_distance_matches_atr_rule(self):
        with patch("scoring._atr_pct", return_value=0.04):
            d = scoring.get_stop_distance_pct("ROBINHOOD", "AAPL", "stock")
        self.assertGreaterEqual(d, 0.035)
        self.assertLessEqual(d, 0.035 * scoring.ATR_SIZING_CAP_MULT)

    def test_heat_snapshot_dd_and_risk(self):
        scoring._equity_dd["ROBINHOOD"] = {
            "day": scoring._local_day_key(),
            "day_open": 10000.0,
            "peak": 10000.0,
            "pause_until": __import__("time").time() + 600,
            "pause_reason": "Day drawdown",
        }
        rows = [
            {
                "broker": "Robinhood",
                "broker_id": "ROBINHOOD",
                "equity": 10000.0,
                "buying_power": 2000.0,
                "day_pnl": -100.0,
                "armed": True,
                "holdings": [
                    {"ticker": "AAPL", "value": 1000.0, "asset_type": "stock"},
                ],
            }
        ]
        with patch("scoring._atr_pct", return_value=None):
            snap = scoring.portfolio_heat_snapshot(rows, settings={"daily_loss_limit": 500})
        c = snap["combined"]
        self.assertTrue(c["dd_paused"])
        self.assertGreater(c["open_risk_dollars"], 0)
        self.assertAlmostEqual(c["bp_headroom"], 2000.0 * 0.88, places=0)


if __name__ == "__main__":
    unittest.main()
