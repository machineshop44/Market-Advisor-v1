"""Unit tests for 1.35.0 discovery/research lift."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestDeskRadar(unittest.TestCase):
    def test_upsert_dedupe_and_alert(self):
        import desk_radar

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "desk_radar.json")
            with patch.object(desk_radar, "_RADAR_PATH", path):
                desk_radar.upsert_candidates(
                    [{"ticker": "ETH", "score": 70, "asset_type": "Crypto"}],
                    engine="CRYPTO",
                    broker="Coinbase",
                )
                desk_radar.upsert_candidates(
                    [{"ticker": "ETH", "score": 75, "asset_type": "Crypto"}],
                    engine="CRYPTO",
                    broker="Coinbase",
                )
                top = desk_radar.top_radar(5)
                self.assertEqual(len(top), 1)
                self.assertEqual(top[0]["ticker"], "ETH")
                self.assertGreaterEqual(float(top[0]["score"]), 75)
                alert = desk_radar.latest_signal_alert(top)
                self.assertIsNotNone(alert)
                self.assertIn("ETH", alert["id"])

    def test_merge_breakout_universe(self):
        import desk_radar

        out = desk_radar.merge_breakout_universe(
            ["SOUN", "BAD1"],
            ["SOUN", "PLUG"],
            ["RKLB", "AAXYZ"],
            max_total=10,
        )
        syms = [r["symbol"] for r in out]
        self.assertEqual(syms.count("SOUN"), 1)
        self.assertIn("PLUG", syms)
        self.assertIn("RKLB", syms)
        self.assertNotIn("AAXYZ", syms)
        types = {r["symbol"]: r["type"] for r in out}
        self.assertEqual(types["SOUN"], "Finviz Breakout")
        self.assertEqual(types["PLUG"], "RH Top Mover")


class TestExplainGate(unittest.TestCase):
    def test_explain_gate_from_recommendation(self):
        from scoring import explain_gate_from_recommendation

        self.assertIn("Gate open", explain_gate_from_recommendation("BUY (MTF Confirmed | RSI: 40)"))
        self.assertIn("overbought", explain_gate_from_recommendation("DO NOT BUY (RSI Overbought: 75)").lower())
        self.assertIn("regime", explain_gate_from_recommendation("DO NOT BUY (Regime: BTC weak)").lower())


class TestRelativeStrengthHelper(unittest.TestCase):
    def test_pct_change_from_closes(self):
        from scoring import _pct_change_from_closes

        self.assertAlmostEqual(_pct_change_from_closes([100.0, 110.0]), 0.1)
        self.assertIsNone(_pct_change_from_closes([10.0]))


if __name__ == "__main__":
    unittest.main()
