"""Scoring: hard-stop cooldown, loss-streak pause, ATR sizing widen."""
import os
import sys
import time
import unittest
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestHardStopCooldown(unittest.TestCase):
    def setUp(self):
        import scoring
        for bid in scoring._KNOWN_BROKER_IDS:
            scoring._cooldown_memory[bid] = {}
            scoring._portfolio_memory[bid] = {}
            scoring._loss_streak[bid] = {"events": [], "pause_until": 0.0}

    def test_hard_stop_doubles_lockout(self):
        import scoring
        scoring._apply_cooldown("ROBINHOOD", "AAPL", sell_price=100.0, reason="hard_stop")
        allowed, reason = scoring._check_hysteresis("AAPL", 101.0, is_crypto=False, broker_id="ROBINHOOD")
        self.assertFalse(allowed)
        self.assertIn("Cooldown", reason)
        # Normal stock cooldown is 20m; hard-stop uses 2x → still locked at ~1m elapsed
        self.assertGreaterEqual(scoring.STOCK_COOLDOWN * scoring.HARD_STOP_COOLDOWN_MULT, 30 * 60)

    def test_loss_streak_pauses_new_buys(self):
        import scoring
        now = time.time()
        with patch("scoring.time.time", return_value=now):
            for _ in range(scoring.LOSS_STREAK_TRIGGER):
                scoring._record_hard_stop_streak("COINBASE")
            allowed, reason = scoring._loss_streak_block("COINBASE")
            self.assertFalse(allowed)
            self.assertIn("Loss-streak", reason)
            # Entry path also blocked
            ok, msg = scoring._check_hysteresis("BTC", 50000.0, is_crypto=True, broker_id="COINBASE")
            self.assertFalse(ok)
            self.assertIn("Loss-streak", msg)


class TestAtrSizingStop(unittest.TestCase):
    def test_sizing_widens_toward_atr(self):
        import scoring
        # Fee-profile base (no ATR) — don't call live yfinance
        with patch("scoring._atr_pct", return_value=None):
            base = scoring.get_stop_distance_pct(
                "ROBINHOOD", ticker="SPY", asset_type="stock"
            )
        atr = 0.04
        with patch("scoring._atr_pct", return_value=atr):
            widened = scoring.get_stop_distance_pct(
                "ROBINHOOD", ticker="SPY", asset_type="stock", for_sizing=True
            )
            # Exits share the same ATR widen (for_sizing is informational only)
            exit_d = scoring.get_stop_distance_pct(
                "ROBINHOOD", ticker="SPY", asset_type="stock", for_sizing=False
            )
        expected = min(max(base, atr * scoring.ATR_SIZING_MULT), base * scoring.ATR_SIZING_CAP_MULT)
        self.assertGreater(widened, base)
        self.assertLessEqual(widened, base * scoring.ATR_SIZING_CAP_MULT)
        self.assertAlmostEqual(widened, expected)
        self.assertAlmostEqual(exit_d, widened)

    def test_evaluate_holding_tags_hard_stop(self):
        import scoring
        for bid in scoring._KNOWN_BROKER_IDS:
            scoring._cooldown_memory[bid] = {}
            scoring._portfolio_memory[bid] = {}
            scoring._loss_streak[bid] = {"events": [], "pause_until": 0.0}
        with patch("scoring.save_state"):
            action = scoring.evaluate_holding(
                "META", avg_cost=100.0, broker_id="ROBINHOOD",
                asset_type="stock", live_price=96.0,  # -4% < -3.5% hard stop
            )
        self.assertIn("Hard Stop", action)
        cool = scoring._cooldown_memory["ROBINHOOD"].get("META") or {}
        self.assertEqual(cool.get("reason"), "hard_stop")

    def test_evaluate_holding_dust_basis_not_mega_roi_sell(self):
        """Dust RH cost_bases must not invent TTP / time-green mega-wins."""
        import scoring
        for bid in scoring._KNOWN_BROKER_IDS:
            scoring._cooldown_memory[bid] = {}
            scoring._portfolio_memory[bid] = {}
            scoring._loss_streak[bid] = {"events": [], "pause_until": 0.0}
        with patch("scoring.save_state"):
            action = scoring.evaluate_holding(
                "SOL", avg_cost=0.0007, broker_id="ROBINHOOD",
                asset_type="cryptocurrency", live_price=150.0,
            )
        self.assertNotIn("TTP Armed", action)
        self.assertNotIn("TTP Triggered", action)
        self.assertNotIn("Time-Green", action)
        self.assertNotIn("Time-Stop", action)
        self.assertIn("HOLD", action)
        self.assertIn("Unknown Cost", action)
        self.assertNotRegex(action, r"\+[0-9]{4,}")

    def test_usable_holding_cost_dust(self):
        import scoring
        self.assertEqual(scoring._usable_holding_cost(0.001, 100.0), 0.0)
        self.assertAlmostEqual(scoring._usable_holding_cost(99.0, 100.0), 99.0)
        self.assertEqual(scoring._usable_holding_cost(0, 100.0), 0.0)


if __name__ == "__main__":
    unittest.main()
