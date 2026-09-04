import os
import sys
import unittest
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from trader_context import build_trader_context, format_regime_chip, format_trader_digest


class TestTraderContextRegime(unittest.TestCase):
    def test_auto_ready_false_when_regime_blocks_balanced(self):
        with patch(
            "trader_context.entry_regime_ok",
            side_effect=[
                (False, "DO NOT BUY (Regime: SPY sources disagree — blocked)"),
                (True, ""),
            ],
        ):
            ctx = build_trader_context(
                "E*TRADE",
                equity=100.0,
                buying_power=100.0,
                settings={"risk_posture": "balanced", "auto_scale_growth": False},
                armed=True,
                connected=True,
            )
        self.assertTrue(ctx.get("can_place_new_buy"))
        self.assertTrue(ctx.get("regime_blocks_entry"))
        self.assertFalse(ctx.get("auto_ready"))

    def test_auto_ready_true_when_growth_skips_spy(self):
        with patch(
            "trader_context.entry_regime_ok",
            return_value=(True, ""),
        ):
            ctx = build_trader_context(
                "E*TRADE",
                equity=120.0,
                buying_power=90.0,
                settings={"risk_posture": "growth"},
                armed=True,
                connected=True,
            )
        self.assertFalse(ctx.get("regime_blocks_entry"))
        self.assertTrue(ctx.get("auto_ready"))

    def test_format_regime_chip_growth_skipped(self):
        label, tip, color = format_regime_chip({
            "posture": "growth",
            "regime": {"equity_ok": False, "equity_reason": "blocked", "crypto_ok": True},
        })
        self.assertIn("SPY skipped", label)
        self.assertIn("#2E7D32", color)

    def test_digest_shows_regime_flag(self):
        digest = format_trader_digest({
            "E*TRADE": {
                "summary": "E*TRADE: $100 BP",
                "regime_blocks_entry": True,
                "can_place_new_buy": True,
                "auto_ready": False,
            }
        })
        self.assertIn("[REGIME]", digest)


if __name__ == "__main__":
    unittest.main()
