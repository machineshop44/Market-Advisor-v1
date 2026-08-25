"""Bar/OHLCV walk-forward math + fractional share policy helpers."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import analytics  # noqa: E402


def _bar(day, o, h, lo, c):
    return {
        "ts": datetime(2026, 8, day, 16, 0, 0),
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "volume": 1e6,
    }


def _fill(ts, side, ticker, price, qty, *, fee=0.1):
    return {
        "timestamp": ts,
        "broker": "Robinhood",
        "side": side,
        "ticker": ticker,
        "price": price,
        "qty": qty,
        "dollars": price * qty,
        "status": "Filled",
        "confirmed": True,
        "fee_est": fee,
    }


class SimulateBarRoundTripTests(unittest.TestCase):
    def test_stop_exit(self):
        bars = [
            _bar(5, 100, 101, 99, 100.5),
            _bar(6, 100, 100.5, 96, 97),  # low hits 3.5% stop (~96.5)
            _bar(7, 97, 98, 96, 97.5),
        ]
        entry = datetime(2026, 8, 5, 10, 0, 0)
        sim = analytics.simulate_bar_round_trip(bars, entry, 10.0, stop_pct=0.035, fee_dollars=1.0)
        self.assertIsNotNone(sim)
        self.assertEqual(sim["exit_reason"], "stop")
        self.assertAlmostEqual(sim["entry_px"], 100.0)
        self.assertAlmostEqual(sim["exit_px"], 96.5)
        self.assertAlmostEqual(sim["realized_pnl"], (96.5 - 100.0) * 10.0)
        self.assertAlmostEqual(sim["net_after_fees"], sim["realized_pnl"] - 1.0)

    def test_signal_exit(self):
        bars = [
            _bar(5, 50, 51, 49, 50.5),
            _bar(6, 51, 53, 50.5, 52),
            _bar(7, 52, 54, 51.5, 53),
        ]
        entry = datetime(2026, 8, 5, 9, 0, 0)
        exit_ts = datetime(2026, 8, 7, 9, 0, 0)
        sim = analytics.simulate_bar_round_trip(
            bars, entry, 2.0, exit_ts=exit_ts, stop_pct=0.20, fee_dollars=0.0,
        )
        self.assertEqual(sim["exit_reason"], "signal")
        self.assertAlmostEqual(sim["exit_px"], 53.0)
        self.assertAlmostEqual(sim["realized_pnl"], (53.0 - 50.0) * 2.0)

    def test_eod_when_no_exit(self):
        bars = [_bar(5, 10, 11, 9.5, 10.5), _bar(6, 10.5, 11, 10, 10.8)]
        sim = analytics.simulate_bar_round_trip(
            bars, datetime(2026, 8, 5, 8, 0, 0), 1.0, stop_pct=0.50,
        )
        self.assertEqual(sim["exit_reason"], "eod")
        self.assertAlmostEqual(sim["exit_px"], 10.8)


class BarWalkForwardTests(unittest.TestCase):
    def test_folds_with_injected_bars(self):
        rows = []
        for i, day in enumerate((5, 6, 7)):
            rows.append(_fill(f"2026-08-0{day}T10:00:00", "BUY", "AAA", 100.0, 1.0))
            rows.append(_fill(f"2026-08-0{day}T15:00:00", "SELL", "AAA", 105.0, 1.0))

        def fake_fetch(ticker, **kwargs):
            # Rising bars so stop never hits; signal exit on sell day close
            return [
                {
                    "ts": datetime(2026, 8, d, 16, 0, 0),
                    "open": 100.0 + (d - 5),
                    "high": 110.0,
                    "low": 99.0,
                    "close": 104.0 + (d - 5),
                    "volume": 1e6,
                }
                for d in range(5, 10)
            ]

        wf = analytics.bar_walk_forward_replay(
            rows, n_folds=3, stop_pct=0.20, bar_fetcher=fake_fetch,
        )
        self.assertEqual(wf["mode"], "bar_ohlcv")
        self.assertGreaterEqual(wf["n_trades"], 2)
        self.assertGreaterEqual(wf["oos_steps"], 1)
        self.assertTrue(any("OHLCV" in a or "bar" in a.lower() for a in wf["assumptions"]))
        self.assertIsNotNone(wf.get("oos_net_sum"))
        self.assertIn("AAA", wf.get("symbols") or [])
        self.assertIn("AAA", wf.get("by_symbol") or {})

    def test_multi_symbol_and_broker_fee_source(self):
        rows = [
            _fill("2026-08-05T10:00:00", "BUY", "AAA", 100.0, 1.0),
            _fill("2026-08-05T15:00:00", "SELL", "AAA", 105.0, 1.0),
            _fill("2026-08-06T10:00:00", "BUY", "BBB", 50.0, 2.0),
            _fill("2026-08-06T15:00:00", "SELL", "BBB", 55.0, 2.0),
            {
                "timestamp": "2026-08-07T10:00:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "CCC",
                "price": 10.0,
                "qty": 5.0,
                "dollars": 50.0,
                "status": "Filled",
                "confirmed": True,
                "fee_paid": 0.12,
            },
            {
                "timestamp": "2026-08-07T15:00:00",
                "broker": "Robinhood",
                "side": "SELL",
                "ticker": "CCC",
                "price": 11.0,
                "qty": 5.0,
                "dollars": 55.0,
                "status": "Filled",
                "confirmed": True,
                "fee_paid": 0.12,
            },
        ]

        def fake_fetch(ticker, **kwargs):
            base = {"AAA": 100.0, "BBB": 50.0, "CCC": 10.0}.get(ticker, 10.0)
            return [
                {
                    "ts": datetime(2026, 8, d, 16, 0, 0),
                    "open": base,
                    "high": base * 1.1,
                    "low": base * 0.95,
                    "close": base * 1.05,
                    "volume": 1e6,
                }
                for d in range(5, 10)
            ]

        wf = analytics.bar_walk_forward_replay(
            rows, n_folds=3, stop_pct=0.50, bar_fetcher=fake_fetch,
        )
        self.assertGreaterEqual(len(wf.get("symbols") or []), 2)
        self.assertTrue(any(t.get("fee_source") == "broker" for t in (wf.get("trades") or [])))
        self.assertTrue(any("Multi-symbol" in a for a in (wf.get("assumptions") or [])))

    def test_too_few_candidates(self):
        wf = analytics.bar_walk_forward_replay(
            [_fill("2026-08-05T10:00:00", "BUY", "X", 10, 1)],
            bar_fetcher=lambda *a, **k: [],
        )
        self.assertEqual(wf.get("n_trades", 0), 0)
        self.assertEqual(wf.get("oos_steps", 0), 0)


class FractionalPolicyTests(unittest.TestCase):
    def test_prefer_whole_when_affordable(self):
        r = analytics.apply_fractional_share_policy(150.0, 40.0, prefer_whole_shares=True)
        self.assertEqual(r["policy"], "whole_shares")
        self.assertEqual(r["qty"], 3.0)
        self.assertTrue(r["whole_shares"])

    def test_sub1_ttp_only(self):
        r = analytics.apply_fractional_share_policy(
            25.0, 100.0, prefer_whole_shares=True, allow_fractional_ttp_only=True, min_dollars=5.0,
        )
        self.assertEqual(r["policy"], "fractional_ttp_only")
        self.assertAlmostEqual(r["qty"], 0.25)

    def test_sub1_blocked(self):
        r = analytics.apply_fractional_share_policy(
            25.0, 100.0, prefer_whole_shares=True, allow_fractional_ttp_only=False,
        )
        self.assertEqual(r["policy"], "skip")


class ShadowGuardrailTests(unittest.TestCase):
    def test_adverse_triggers_tighten(self):
        rows = []
        for i in range(6):
            rows.append({
                "timestamp": f"2026-08-05T10:0{i}:00",
                "broker": "Robinhood",
                "side": "BUY",
                "ticker": "A",
                "price": 100,
                "qty": 1,
                "dollars": 100,
                "status": "Filled",
                "confirmed": True,
                "slippage_bps": 12.0,
            })
        g = analytics.evaluate_shadow_guardrail(rows, adverse_rate_threshold=0.5, min_samples=4)
        self.assertTrue(g["tighten"])
        self.assertLess(g["size_mult"], 1.0)


if __name__ == "__main__":
    unittest.main()
