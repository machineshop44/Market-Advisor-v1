"""Cluster heat + protective health + decision journal."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import journal  # noqa: E402
import scoring  # noqa: E402


class ClusterProtectiveDecisionTests(unittest.TestCase):
    def test_cluster_heat_full(self):
        rows = scoring.cluster_heat_snapshot(["AAPL", "MSFT", "NVDA"])
        mag = next(r for r in rows if r["name"] == "MAG7")
        self.assertGreaterEqual(mag["count"], 2)
        self.assertTrue(mag["full"] or mag["count"] >= scoring.MAX_CLUSTER_POSITIONS)

    def test_protective_health_missing(self):
        scoring._protective_orders["ROBINHOOD"] = {}
        health = scoring.protective_stop_health(
            [
                {
                    "broker_id": "ROBINHOOD",
                    "ticker": "AAPL",
                    "value": 500,
                    "supports_protective": True,
                }
            ]
        )
        self.assertEqual(health["missing_count"], 1)
        self.assertEqual(health["missing"][0]["ticker"], "AAPL")

    def test_decision_journal_roundtrip(self):
        path = Path(journal.DECISION_FILE)
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            journal.log_decision(
                {
                    "broker": "Robinhood",
                    "ticker": "TEST",
                    "action": "SKIP",
                    "score": 55,
                    "reason": "unit_test",
                }
            )
            rows = journal.read_recent_decisions(5)
            self.assertTrue(any(r.get("ticker") == "TEST" for r in rows))
        finally:
            if before:
                path.write_text(before, encoding="utf-8")
            elif path.exists():
                # remove only if we created fresh file with just our line — truncate last line
                lines = path.read_text(encoding="utf-8").splitlines()
                path.write_text(
                    "\n".join(ln for ln in lines if "unit_test" not in ln) + ("\n" if lines else ""),
                    encoding="utf-8",
                )


if __name__ == "__main__":
    unittest.main()
