"""Walk-forward fee-aware journal replay + fill quality + paper/live shadow."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import analytics  # noqa: E402


def _fill(ts, side, ticker, price, qty, *, paper=False, fee=0.1, slip=None):
    row = {
        "timestamp": ts,
        "broker": "Robinhood",
        "side": side,
        "ticker": ticker,
        "price": price,
        "qty": qty,
        "dollars": price * qty,
        "status": "[PAPER] Filled" if paper else "Filled",
        "confirmed": True,
        "paper": paper,
        "fee_est": fee,
        "quote_price": price,
        "fill_price": price if slip is None else price * (1.0 + slip / 10000.0),
    }
    if slip is not None:
        row["slippage_bps"] = slip
    return row


class WalkForwardReplayTests(unittest.TestCase):
    def test_walk_forward_splits_and_sums_oos(self):
        rows = []
        # 6 fills → 3 folds of 2
        for i in range(3):
            day = 5 + i
            rows.append(_fill(f"2026-08-0{day}T10:00:00", "BUY", "AAPL", 100.0, 1.0, fee=0.1))
            rows.append(_fill(f"2026-08-0{day}T11:00:00", "SELL", "AAPL", 110.0, 1.0, fee=0.1))
        wf = analytics.walk_forward_fee_replay(rows, n_folds=3)
        self.assertGreaterEqual(wf["n_folds"], 2)
        self.assertGreaterEqual(wf["oos_steps"], 1)
        self.assertIn("assumptions", wf)
        self.assertTrue(any("journal" in a.lower() for a in wf["assumptions"]))
        # Each closed round-trip ≈ $10 − fees
        self.assertAlmostEqual(wf["overall"]["realized_pnl"], 30.0, places=1)
        self.assertIsNotNone(wf.get("oos_net_sum"))

    def test_walk_forward_too_few_fills(self):
        wf = analytics.walk_forward_fee_replay([_fill("2026-08-05T10:00:00", "BUY", "X", 10, 1)])
        self.assertEqual(wf["walk_forward"] if "walk_forward" in wf else [], wf.get("walk_forward", []))
        self.assertEqual(wf.get("oos_steps", 0), 0)

    def test_fill_quality_adverse_rate(self):
        rows = [
            _fill("2026-08-05T10:00:00", "BUY", "A", 100, 1, slip=10.0),
            _fill("2026-08-05T10:01:00", "BUY", "B", 100, 1, slip=-2.0),
            _fill("2026-08-05T10:02:00", "SELL", "A", 110, 1, slip=8.0),
        ]
        fq = analytics.summarize_fill_quality(rows)
        self.assertEqual(fq["samples"], 3)
        self.assertAlmostEqual(fq["avg_slippage_bps"], (10 - 2 + 8) / 3.0, places=2)
        self.assertEqual(fq["adverse_count"], 2)
        self.assertAlmostEqual(fq["adverse_rate"], 2 / 3, places=4)

    def test_paper_live_shadow(self):
        rows = [
            _fill("2026-08-05T10:00:00", "BUY", "A", 100, 1, paper=True, fee=0.05),
            _fill("2026-08-05T11:00:00", "SELL", "A", 105, 1, paper=True, fee=0.05),
            _fill("2026-08-05T12:00:00", "BUY", "B", 50, 2, paper=False, fee=0.1),
            _fill("2026-08-05T13:00:00", "SELL", "B", 55, 2, paper=False, fee=0.1),
        ]
        shadow = analytics.compare_paper_live(rows)
        self.assertTrue(shadow["both_modes"])
        self.assertEqual(shadow["paper"]["fills"], 2)
        self.assertEqual(shadow["live"]["fills"], 2)
        self.assertAlmostEqual(shadow["paper"]["realized_pnl"], 5.0, places=1)
        self.assertAlmostEqual(shadow["live"]["realized_pnl"], 10.0, places=1)


if __name__ == "__main__":
    unittest.main()
