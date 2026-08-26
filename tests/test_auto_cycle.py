"""auto_cycle extract + IDLE_SKIP journal coverage."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import analytics  # noqa: E402
import auto_cycle as ac  # noqa: E402
import decision_log as dlog  # noqa: E402


class TestAutoCycleRankTrail(unittest.TestCase):
    def test_format_ranked_for_book(self):
        ranked = [
            {"ticker": "ETH", "score": 70, "scale_in": True},
            {"ticker": "BTC", "score": 65},
        ]
        actionable = list(ranked)
        line = ac.format_ranked_for_book_note("Robinhood", actionable, ranked)
        self.assertIn("Ranked 2/2 buys for book", line)
        self.assertIn("ETH(70*SI)", line)
        self.assertIn("BTC(65)", line)

    def test_filter_and_empty_note(self):
        ranked = [
            {"ticker": "ETH", "score": -1000},
            {"ticker": "SOL", "score": 80},
        ]
        actionable = ac.filter_actionable_ranked(ranked)
        self.assertEqual(len(actionable), 1)
        self.assertEqual(actionable[0]["ticker"], "SOL")
        notes = []
        self.assertTrue(ac.should_append_empty_after_rank_filter(notes, ranked))
        notes.append("[RH] SCALE-IN skipped [ETH]: missing cost")
        self.assertFalse(ac.should_append_empty_after_rank_filter(notes, ranked))
        self.assertIn("0/2 actionable", ac.empty_after_rank_filter_note("Coinbase", 2))

    def test_unpack_and_count(self):
        opps, results, buys, dropped = ac.unpack_scan_payload(
            (["o"], [(0, 1, "BUY", "", "")], [{"ticker": "A"}], ["A (held)"])
        )
        self.assertEqual(opps, ["o"])
        self.assertEqual(len(buys), 1)
        self.assertEqual(dropped[0], "A (held)")
        self.assertEqual(ac.count_buy_signals(results), 1)
        self.assertEqual(
            ac.count_buy_signals([(0, 1, "DO NOT BUY", "", "")]), 0
        )
        self.assertEqual(ac.unpack_scan_payload(None), ([], [], [], []))


class TestAutoCycleCoachThrottle(unittest.TestCase):
    def test_throttle_scan_drops(self):
        store = {}
        vis, sup = ac.throttle_scan_drops(
            store, "RH", "CORE", ["ETH (scale-in blocked: cost)", "BTC (held)"],
            now=1000.0, cooldown_sec=780,
        )
        self.assertEqual(len(vis), 2)
        self.assertEqual(sup, 0)
        vis2, sup2 = ac.throttle_scan_drops(
            store, "RH", "CORE", ["ETH (scale-in blocked: cost)"],
            now=1100.0, cooldown_sec=780,
        )
        self.assertEqual(vis2, [])
        self.assertEqual(sup2, 1)

    def test_coach_tip_buckets(self):
        key, tip = ac.coach_tip_for_scan_drops(
            "Robinhood", "CRYPTO", ["LINK (scale-in blocked: missing cost basis)"]
        )
        self.assertIn("no_actionable:missing_cost", key)
        self.assertIn("cost basis", tip)

    def test_regime_idle_coach_tip(self):
        key, tip = ac.regime_idle_coach_tip(
            "Robinhood", "CORE", idle_sec=3700,
            regime_reason="DO NOT BUY (Regime: SPY 1H Downtrend)",
        )
        self.assertIn("regime_idle:blocked", key)
        self.assertIn("61m", tip)
        self.assertIn("Regime", tip)

        key2, tip2 = ac.regime_idle_coach_tip(
            "Robinhood", "CORE", idle_sec=3700, dd_paused=True,
        )
        self.assertIn("regime_idle:dd_pause", key2)
        self.assertIn("drawdown pause", tip2.lower())

        line = ac.format_no_actionable_scan_note(
            "Coinbase", "CRYPTO", 3, visible=["ETH (held)"], suppressed=1,
        )
        self.assertIn("0 actionable", line)
        self.assertIn("ETH (held)", line)
        self.assertIn("1 muted", line)
        self.assertIsNone(
            ac.format_no_actionable_scan_note(
                "Coinbase", "CRYPTO", 2, visible=[], suppressed=2,
            )
        )

    def test_filter_affordable_buy_candidates(self):
        cands = [{"ticker": "ETH", "asset_type": "crypto", "price": 3000, "score": 70}]
        ok, dropped = ac.filter_affordable_buy_candidates(
            cands,
            buying_power=3.0,
            equity=200.0,
            broker_id="ROBINHOOD",
            settings={"min_trade_dollars": 5.0, "target_bp_utilization_pct": 88.0},
        )
        self.assertEqual(len(ok), 0)
        self.assertTrue(dropped[0].startswith("ETH (unaffordable:"))
        ok2, dropped2 = ac.filter_affordable_buy_candidates(
            cands,
            buying_power=100.0,
            equity=200.0,
            broker_id="ROBINHOOD",
            settings={"min_trade_dollars": 5.0, "target_bp_utilization_pct": 88.0},
        )
        self.assertEqual(len(ok2), 1)
        self.assertEqual(dropped2, [])

    def test_locked_capital_summary_goevq(self):
        holdings = [
            {
                "broker": "Robinhood",
                "ticker": "GOEVQ",
                "shares": 10,
                "price": 0.0,
                "value": 0.0,
            }
        ]
        s = ac.locked_capital_summary(holdings)
        self.assertEqual(s["count"], 1)
        self.assertIn("GOEVQ", s["rows"][0]["ticker"])

    def test_effective_book_equity(self):
        self.assertAlmostEqual(ac.effective_book_equity(207.0, 12.5), 194.5)
        self.assertAlmostEqual(ac.effective_book_equity(50.0, 100.0), 0.0)
        self.assertAlmostEqual(ac.effective_book_equity(0.0, 5.0), 0.0)

    def test_scale_in_skip_throttle(self):
        store = {}
        n1 = ac.scale_in_skip_note(
            store, "Robinhood", "ETH", "missing cost", now=100.0, throttle_sec=780,
        )
        self.assertIn("SCALE-IN skipped", n1)
        n2 = ac.scale_in_skip_note(
            store, "Robinhood", "ETH", "missing cost", now=200.0, throttle_sec=780,
        )
        self.assertIsNone(n2)
        ac.clear_scale_in_skip_throttle(store, "Robinhood", "ETH")
        self.assertEqual(store, {})


class TestIdleSkipJournal(unittest.TestCase):
    def test_emit_and_summarize(self):
        captured = []
        dlog.emit_idle_skip(
            lambda **kw: captured.append(kw),
            broker="E*TRADE",
            reason="Sandbox/no BP — buy engines idle",
            engine="CORE",
            bp=0.0,
        )
        self.assertEqual(captured[0]["action"], "IDLE_SKIP")
        self.assertTrue(str(captured[0]["reason"]).startswith("idle:"))
        self.assertEqual(captured[0]["engine"], "CORE")

        rows = [
            captured[0],
            {"action": "BUY", "broker": "Robinhood", "ticker": "A"},
            {"action": "ROTATE_SKIP", "broker": "Robinhood", "ticker": "B", "reason": "rotate:x"},
        ]
        s = analytics.summarize_decisions(rows)
        self.assertEqual(s["idle_skips"], 1)
        self.assertEqual(s["rotate_skips"], 1)
        self.assertEqual(s["skips"], 2)
        self.assertEqual(s["buys"], 1)


class TestCycleBookExtract(unittest.TestCase):
    def test_throttled_buy_skip_and_frac_defer(self):
        store = {}
        notes = []
        self.assertTrue(
            ac.throttled_buy_skip_note(
                store, notes, "Robinhood", "bp_low", "[RH] BP low",
                now=1000.0, cooldown_sec=720,
            )
        )
        self.assertFalse(
            ac.throttled_buy_skip_note(
                store, notes, "Robinhood", "bp_low", "[RH] BP low",
                now=1100.0, cooldown_sec=720,
            )
        )
        defer_store = {}
        self.assertTrue(
            ac.note_frac_buy_defer(
                defer_store, notes, "Robinhood", "AAPL", "overnight", "OVERNIGHT",
            )
        )
        self.assertFalse(
            ac.note_frac_buy_defer(
                defer_store, notes, "Robinhood", "AAPL", "overnight", "OVERNIGHT",
            )
        )

    def test_rh_equity_sell_defer(self):
        closed = {"equity_tradeable": False, "fractional_ok": False, "label": "WEEKEND"}
        self.assertEqual(
            ac.rh_equity_sell_defer_reason("AAPL", 2, 10, "stock", closed),
            "equity markets closed",
        )
        self.assertIsNone(
            ac.rh_equity_sell_defer_reason("ETH", 0.5, 2000, "cryptocurrency", closed)
        )
        ext = {"equity_tradeable": True, "fractional_ok": False, "label": "OVERNIGHT"}
        self.assertIn(
            "fractional",
            ac.rh_equity_sell_defer_reason("AAPL", 0.5, 50, "stock", ext) or "",
        )

    def test_rotate_and_portfolio_notes(self):
        self.assertIn("skipped", ac.format_rotate_skip_note("Coinbase", "no edge"))
        self.assertIn("fund SOL", ac.format_rotate_sell_note(
            "Coinbase", "ETH", "SOL", roi=0.02, fund_score=40, candidate_score=70, reason="swap",
        ))
        self.assertIn("failed", ac.format_rotate_sell_failed_note("RH", "AAPL", "Fail: x"))
        self.assertIn("Freed", ac.format_rotate_freed_note("RH", "AAPL", "$100.00", "$200.00"))
        floor_note = ac.format_rotate_floor_clear_note(
            "Robinhood", "XLM", bp=4.35, floor=5.0, label="RH crypto floor",
        )
        self.assertIn("[ROTATE] freeing BP", floor_note)
        self.assertIn("4.35", floor_note)
        self.assertIn("5.00", floor_note)
        self.assertIn("XLM", floor_note)
        self.assertIn("OK", ac.format_scale_in_ok_note("ETH", "in band"))

        actionable, deferred, notes = ac.partition_portfolio_sells(
            [
                {"broker": "Robinhood", "ticker": "AAPL", "shares": 0.5, "price": 10, "type": "stock"},
                {"broker": "Robinhood", "ticker": "MSFT", "shares": 2, "price": 100, "type": "stock"},
            ],
            broker_name="Robinhood",
            session={"equity_tradeable": True, "fractional_ok": False, "label": "OVERNIGHT"},
            sell_fail_should_skip=lambda b, t: False,
            rh_defer_reason_fn=ac.rh_equity_sell_defer_reason,
            note_deferred_fn=lambda broker, tick, defer, label, notes_tmp: notes_tmp.append(
                f"[{broker}] Deferring [{tick}] — {defer}"
            ),
        )
        self.assertEqual(len(actionable), 1)
        self.assertEqual(actionable[0]["ticker"], "MSFT")
        self.assertIn("AAPL", deferred)
        trail = ac.format_portfolio_scored_note(
            "Robinhood", 2, actionable_n=1, deferred=deferred, first_defer_this_session=True,
        )
        self.assertIn("deferring", trail)

    def test_etrade_home_chips(self):
        chip, tip, col = ac.etrade_home_env_chip(
            environment="live", live_trading=True, buying_power=0.0, min_trade_dollars=5,
        )
        self.assertIn("stops N/A", chip)
        self.assertIn("$0", chip)
        self.assertIn("funding", tip.lower())
        bp_txt, bp_tip = ac.etrade_bp_label(0.0, environment="live")
        self.assertIn("$0.00", bp_txt)
        self.assertIn("verify", bp_tip.lower())
        self.assertEqual(ac.format_cost_basis_display(0, broker_name="Coinbase"), "cost ?")
        self.assertEqual(
            ac.count_unknown_cost_holdings(
                [{"broker": "Coinbase", "ticker": "BTC", "cost": 0},
                 {"broker": "Coinbase", "ticker": "ETH", "cost": 2000}],
                broker_name="Coinbase",
            ),
            1,
        )

    def test_portfolio_heat_extract_helpers(self):
        assets = [
            {"broker": "Robinhood", "ticker": "GOEVQ", "price": 0.01, "value": 0.5, "shares": 50},
            {"broker": "Coinbase", "ticker": "BTC", "price": 50000, "value": 50, "shares": 0.001},
        ]
        by_b = ac.holdings_by_broker_from_assets(assets, ("Robinhood", "Coinbase"))
        self.assertEqual(len(by_b["Robinhood"]), 1)
        totals = {
            "Robinhood": {"p_val": 100.0, "bp": 20.0},
            "Coinbase": {"p_val": 80.0, "bp": 10.0},
        }
        rows = ac.build_portfolio_heat_rows(
            totals, by_b, {"Robinhood": 95.0, "Coinbase": 75.0},
            {"Robinhood": True, "Coinbase": False},
            ("Robinhood", "Coinbase"),
        )
        self.assertEqual(len(rows), 2)
        rh = next(r for r in rows if r["broker"] == "Robinhood")
        self.assertLess(rh["equity"], rh["raw_equity"])
        snap = {"combined": {"open_risk_dollars": 5, "open_risk_pct": 2.5, "bp_headroom": 30, "day_pnl": 5}}
        label = ac.format_portfolio_heat_label(
            snap, rows, money_fn=lambda x: f"${x:.2f}", currency_fn=lambda x: f"${x:.2f}",
        )
        self.assertIn("Locked", label)
        self.assertIn("Open risk", label)

    def test_wave3_portfolio_monitor_helpers(self):
        self.assertTrue(
            ac.sell_status_should_backoff(
                "Skipped: RH cannot trade GOEVQ via API (OTC/delisted)"
            )
        )
        self.assertFalse(ac.sell_status_should_backoff("Filled"))
        locked = ac.build_monitor_locked_capital(
            {"Robinhood": 12.5},
            {},
            ("Robinhood", "Coinbase"),
        )
        self.assertAlmostEqual(locked["by_broker"]["Robinhood"]["value"], 12.5)
        self.assertAlmostEqual(locked["total"], 12.5)
        locked2 = ac.build_monitor_locked_capital(
            {"Robinhood": {"value": 3.0, "count": 1}},
            {},
            ("Robinhood",),
        )
        self.assertEqual(locked2["by_broker"]["Robinhood"]["count"], 1)
        assets = [{"ticker": "GOEVQ", "shares": 1, "cost": 1.49, "type": "stock"}]
        results = [(0, 0.01, "SELL (TTP)", "stock", None)]
        sells = ac.portfolio_sells_from_scored(assets, results, "Robinhood")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["ticker"], "GOEVQ")
        filtered, dropped = ac.drop_locked_portfolio_sells(
            sells,
            [{"broker": "Robinhood", "ticker": "GOEVQ", "shares": 1, "price": 0.01}],
        )
        self.assertEqual(filtered, [])
        self.assertIn("GOEVQ", dropped)


if __name__ == "__main__":
    unittest.main()
