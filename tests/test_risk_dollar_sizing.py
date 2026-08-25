"""Risk-$ position sizing helper + stop-distance fallback."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import scoring  # noqa: E402


class RiskDollarSizingTests(unittest.TestCase):
    def setUp(self):
        scoring.reset_execution_feedback()

    def test_risk_dollar_caps_notional(self):
        # equity $10k, risk 0.75%, stop 5% → risk_size = 75 / 0.05 = $1500
        # deployable = 2000 * 0.88 = 1760; aim from 1 focus slot ≈ 1760
        d = scoring.risk_sizing_breakdown(
            equity=10_000,
            buying_power=2_000,
            stop_distance_pct=0.05,
            alloc_ceiling_pct=0.05,
            min_dollars=5.0,
            open_count=0,
            max_open_positions=8,
            target_bp_utilization=88.0,
            sizing_focus_slots=1,
            risk_pct_per_trade=0.75,
            max_open_risk_pct=20.0,
        )
        self.assertIsNone(d["skip_reason"])
        self.assertEqual(d["sizing_mode"], "risk_dollar")
        self.assertAlmostEqual(d["risk_budget"], 75.0, places=1)
        self.assertAlmostEqual(d["risk_size"], 1500.0, places=0)
        # trade capped by risk_size and deployable
        self.assertLessEqual(d["trade"], d["risk_size"] + 0.01)
        self.assertLessEqual(d["trade"], d["deployable"] + 0.01)

    def test_tighter_risk_pct_shrinks_size(self):
        common = dict(
            equity=10_000,
            buying_power=5_000,
            stop_distance_pct=0.04,
            alloc_ceiling_pct=0.10,
            min_dollars=5.0,
            open_count=0,
            max_open_positions=5,
            target_bp_utilization=95.0,
            sizing_focus_slots=1,
            max_open_risk_pct=20.0,
        )
        safer = scoring.calculate_risk_sizing(**common, risk_pct_per_trade=0.50)
        agg = scoring.calculate_risk_sizing(**common, risk_pct_per_trade=1.00)
        self.assertGreater(agg, safer)

    def test_book_heat_caps_new_ticket(self):
        # Remaining heat $20 at 4% stop → notional cap $500
        d = scoring.risk_sizing_breakdown(
            equity=10_000,
            buying_power=8_000,
            stop_distance_pct=0.04,
            alloc_ceiling_pct=0.20,
            min_dollars=5.0,
            open_count=0,
            max_open_positions=3,
            target_bp_utilization=95.0,
            sizing_focus_slots=1,
            risk_pct_per_trade=1.0,  # would allow $2500 notional alone
            open_risk_dollars=580.0,  # 6% of 10k = 600 → $20 left
            max_open_risk_pct=6.0,
        )
        self.assertLessEqual(d["trade"], 500.0 + 1.0)

    def test_stop_fallback_util_sizing(self):
        d = scoring.risk_sizing_breakdown(
            equity=1_000,
            buying_power=200,
            stop_distance_pct=0.0,
            alloc_ceiling_pct=0.10,
            min_dollars=5.0,
            open_count=0,
            max_open_positions=8,
            target_bp_utilization=88.0,
            sizing_focus_slots=4,
            risk_pct_per_trade=0.75,
        )
        self.assertTrue(d["used_stop_fallback"])
        self.assertEqual(d["sizing_mode"], "util_fallback")
        self.assertIsNone(d["skip_reason"])
        self.assertGreaterEqual(d["trade"], 5.0)
        self.assertIn("stop distance unknown", d["sizing_note"])

    def test_posture_profiles_have_risk_pct(self):
        for key in ("safer", "balanced", "aggressive"):
            p = scoring.get_risk_posture_profile(key)
            self.assertIn("risk_pct_per_trade", p)
            self.assertIn("max_open_risk_pct", p)
        self.assertLess(
            scoring.RISK_POSTURE_PROFILES["safer"]["risk_pct_per_trade"],
            scoring.RISK_POSTURE_PROFILES["aggressive"]["risk_pct_per_trade"],
        )

    def test_fill_feedback_throttled(self):
        scoring.reset_execution_feedback()
        note = ""
        for _ in range(5):
            note = scoring.note_fill_slippage(12.0)
        self.assertIn("fill-quality", note)
        fb = scoring.get_execution_feedback()
        self.assertGreater(fb["offset_bump_pct"], 0)
        self.assertLess(fb["size_mult"], 1.0)
        # Immediate second cluster should not thrash (cooldown)
        scoring._fill_feedback_state["recent_slip_bps"] = [12.0] * 5
        note2 = scoring.note_fill_slippage(12.0)
        self.assertEqual(note2, "")


if __name__ == "__main__":
    unittest.main()
