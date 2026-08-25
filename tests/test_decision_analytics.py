"""Decision summary + lite posture replay."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import analytics  # noqa: E402


class DecisionAnalyticsTests(unittest.TestCase):
    def test_summarize_and_replay_max_open(self):
        rows = [
            {"action": "BUY", "broker": "Robinhood", "ticker": "A", "score": 70},
            {
                "action": "SKIP",
                "broker": "Robinhood",
                "ticker": "B",
                "reason": "max_open",
                "open_count": 5,
                "max_open": 5,
            },
            {
                "action": "SKIP",
                "broker": "Robinhood",
                "ticker": "C",
                "reason": "concentration:cluster MAG7 full",
                "open_count": 3,
            },
            {
                "action": "ROTATE_SKIP",
                "broker": "Robinhood",
                "ticker": "D",
                "reason": "rotate:no eligible funding name",
            },
            {
                "action": "SCALE_IN_SKIP",
                "broker": "Robinhood",
                "ticker": "ETH",
                "reason": "scale_in:missing cost basis",
            },
        ]
        s = analytics.summarize_decisions(rows)
        self.assertEqual(s["buys"], 1)
        self.assertEqual(s["skips"], 4)
        self.assertEqual(s["rotate_skips"], 1)
        self.assertEqual(s["scale_in_skips"], 1)
        self.assertAlmostEqual(s["buy_rate"], 1 / 5, places=4)

        replay = analytics.lite_posture_decision_replay(rows)
        # Safer max_open=10 → open_count 5 would clear
        safer = replay["postures"]["safer"]
        self.assertGreaterEqual(safer["would_clear_max_open"], 1)
        # Aggressive max_open=5 → open_count 5 would NOT clear (need open_count < max)
        agg = replay["postures"]["aggressive"]
        self.assertEqual(agg["max_open"], 5)


if __name__ == "__main__":
    unittest.main()
