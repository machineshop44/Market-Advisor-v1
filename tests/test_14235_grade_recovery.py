"""
1.42.35 — grade recovery: flatten-on-risk, blotter reconcile, small-book focus park,
PDT entry guard, Advisor approve bar / park TTLs.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import activity_log_util as alu
import blotter_reconcile as br
import desk_advisor_ai as dai
import desk_orchestration as do
import pdt_guard as pdt


class TestBlotterReconcile(unittest.TestCase):
    def test_ghost_and_stale_stops(self):
        held = {"AAPL", "MSFT"}
        ghosts = br.ghost_local_tickers(held, ["AAPL", "GHOST", "MSFT"])
        self.assertEqual(ghosts, ["GHOST"])
        stale = br.protective_stale_tickers(
            held,
            [("ROBINHOOD", "AAPL", {}), ("ROBINHOOD", "OLD", {})],
            broker_id="ROBINHOOD",
        )
        self.assertEqual(stale, ["OLD"])

    def test_small_book_focus_park(self):
        self.assertTrue(br.small_book_focus_park_active(190.0, {}))
        self.assertFalse(br.small_book_focus_park_active(900.0, {}))
        self.assertTrue(
            br.small_book_focus_park_active(900.0, {"desk_focus_park_others": True})
        )


class TestFocusParkAuto(unittest.TestCase):
    def test_parks_non_focus_under_500(self):
        settings = {"desk_focus_mode": "auto", "desk_focus_park_others": False}
        self.assertTrue(
            do.focus_parks_buys(
                "Coinbase", "E*TRADE", settings, combined_equity=191.0,
            )
        )
        self.assertFalse(
            do.focus_parks_buys(
                "E*TRADE", "E*TRADE", settings, combined_equity=191.0,
            )
        )
        self.assertFalse(
            do.focus_parks_buys(
                "Coinbase", "E*TRADE", settings, combined_equity=900.0,
            )
        )


class TestPdtEntryGuard(unittest.TestCase):
    def setUp(self):
        pdt.load(force=True)
        pdt._buys.clear()
        pdt._day_trades.clear()
        pdt._rebuy_until.clear()
        pdt._broker_dt_counts.clear()

    def test_blocks_when_slots_exhausted(self):
        settings = {"pdt_guard_enabled": True, "pdt_max_day_trades": 3, "pdt_equity_threshold": 25000}
        for i in range(3):
            pdt._day_trades.append({
                "broker": "Robinhood",
                "ticker": f"T{i}",
                "day": pdt._day_key(),
                "ts": 1.0,
            })
        ok, why = pdt.may_open_equity_buy(
            "Robinhood", "NEW", equity=200.0, settings=settings, is_crypto=False,
        )
        self.assertFalse(ok)
        self.assertIn("PDT entry", why)
        ok_c, _ = pdt.may_open_equity_buy(
            "Robinhood", "BTC", equity=200.0, settings=settings, is_crypto=True,
        )
        self.assertTrue(ok_c)


class TestAdvisorSharpen(unittest.TestCase):
    def test_local_approve_requires_70(self):
        prop = {
            "ticker": "ACHR",
            "broker": "Robinhood",
            "dollars": 8.0,
            "price": 5.6,
            "score": 60.0,
            "engine": "BREAKOUT",
            "asset_type": "stock",
        }
        ctx = {
            "buying_power": 40.0,
            "deployable_bp": 40.0,
            "equity": 70.0,
            "posture": "growth",
            "dd_paused": False,
            "blockers": [],
            "max_affordable_share_price": 40.0,
        }
        out = dai.local_analyze_proposal(prop, ctx)
        self.assertEqual(out["verdict"], dai.VERDICT_WAIT)
        prop["score"] = 75.0
        out2 = dai.local_analyze_proposal(prop, ctx)
        self.assertEqual(out2["verdict"], dai.VERDICT_APPROVE)

    def test_hours_mismatch_park_30m(self):
        spec = alu.advisor_miss_park_spec(
            "Fail: {'non_field_errors': ['Extended hours and market hours mismatch.']}"
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec[1], "hours_mismatch")
        self.assertGreaterEqual(spec[0], 30 * 60)

    def test_overnight_frac_park(self):
        spec = alu.advisor_miss_park_spec(
            "would be stuck overnight — fractional equity sells blocked"
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec[1], "overnight_frac")


class TestFlattenSettingsPresent(unittest.TestCase):
    def test_defaults_in_gui_settings_dict(self):
        # Smoke: settings keys exist in DEFAULTS via import side-effect free check
        import json
        path = os.path.join(ROOT, "settings.example.json")
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        self.assertTrue(s.get("daily_loss_flatten"))
        self.assertTrue(s.get("panic_halt_flatten"))
        self.assertEqual(float(s.get("desk_focus_park_others_auto_under")), 500.0)


if __name__ == "__main__":
    unittest.main()
