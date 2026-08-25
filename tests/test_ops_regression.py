"""Ops regression scars: trail, sell backoff, ET idle, activity log rotate, cost basis."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import activity_log_util as alu  # noqa: E402
import analytics  # noqa: E402
import decision_log as dlog  # noqa: E402
from broker import RobinhoodAdapter, _rh_crypto_avg_cost  # noqa: E402


class TestNoBuysAfterRankTrail(unittest.TestCase):
    def test_appends_when_notes_lack_outcome(self):
        notes = ["[Robinhood] Ranked 2/2 buys for book — top: ETH(70)"]
        skips = ["ETH: scale-in (missing cost basis)", "buying power/risk size too low ($0.00)"]
        line = alu.explain_no_buys_after_rank(
            notes, skips,
            buys_done=0, orig_n=2, ranked_n=2, broker_name="Robinhood",
        )
        self.assertIsNotNone(line)
        self.assertIn("No buys executed after rank", line)
        self.assertIn("scale-in", line)
        self.assertEqual(notes[-1], line)

    def test_skips_when_outcome_already_present(self):
        notes = ["[RH] SCALE-IN skipped [ETH]: missing cost basis"]
        line = alu.explain_no_buys_after_rank(
            notes, ["ETH: scale-in"],
            buys_done=0, orig_n=1, ranked_n=0, broker_name="Robinhood",
        )
        self.assertIsNone(line)
        self.assertEqual(len(notes), 1)

    def test_noop_when_buys_done(self):
        notes = []
        self.assertIsNone(
            alu.explain_no_buys_after_rank(
                notes, [], buys_done=1, orig_n=2, ranked_n=1, broker_name="Coinbase",
            )
        )


class TestSellFailBackoff(unittest.TestCase):
    def test_fail_none_backoff_bonk_style(self):
        store = {}
        status = "Fail: " + RobinhoodAdapter._format_rh_order_error(
            None, what="crypto sell BONK"
        )
        self.assertNotIn("Fail:None", status.replace(" ", ""))
        already, note = alu.record_sell_fail_backoff(
            store, "Robinhood", "BONK", status, now=1_000.0, ttl_sec=1800,
        )
        self.assertFalse(already)
        self.assertIn("BONK", note)
        self.assertIn("backing off", note)
        self.assertTrue(
            alu.sell_fail_should_skip(store, "Robinhood", "BONK", now=1_100.0, ttl_sec=1800)
        )
        # Duplicate reason within TTL → suppress
        already2, note2 = alu.record_sell_fail_backoff(
            store, "Robinhood", "BONK", status, now=1_200.0, ttl_sec=1800,
        )
        self.assertTrue(already2)
        self.assertIsNone(note2)
        # TTL expiry clears skip
        self.assertFalse(
            alu.sell_fail_should_skip(store, "Robinhood", "BONK", now=3_000.0, ttl_sec=1800)
        )

    def test_reason_change_relogs(self):
        store = {}
        alu.record_sell_fail_backoff(
            store, "Robinhood", "BONK", "Fail: dust", now=100.0, ttl_sec=1800,
        )
        already, note = alu.record_sell_fail_backoff(
            store, "Robinhood", "BONK", "Fail: auth", now=200.0, ttl_sec=1800,
        )
        self.assertFalse(already)
        self.assertIn("auth", note)


class TestEtradeSandboxIdle(unittest.TestCase):
    def test_sandbox_zero_bp_idles(self):
        self.assertTrue(
            dlog.etrade_sandbox_no_bp(
                paper_mode=False, connected=True,
                environment="sandbox", buying_power=0.0, min_trade_dollars=5.0,
            )
        )
        why = dlog.buy_engines_idle_reason_for(
            "E*TRADE",
            paper_mode=False,
            etrade_connected=True,
            etrade_environment="sandbox",
            etrade_buying_power=0.0,
        )
        self.assertIn("Sandbox/no BP", why)

    def test_live_or_paper_not_idle_sandbox_helper(self):
        # sandbox helper itself is sandbox-only
        self.assertFalse(
            dlog.etrade_sandbox_no_bp(
                paper_mode=False, connected=True,
                environment="live", buying_power=0.0,
            )
        )
        self.assertFalse(
            dlog.etrade_sandbox_no_bp(
                paper_mode=True, connected=True,
                environment="sandbox", buying_power=0.0,
            )
        )
        self.assertIsNone(
            dlog.buy_engines_idle_reason_for("Robinhood", paper_mode=False)
        )

    def test_live_zero_bp_parks_buys(self):
        self.assertTrue(
            dlog.etrade_live_zero_bp(
                paper_mode=False, connected=True,
                environment="live", buying_power=0.0, min_trade_dollars=5.0,
            )
        )
        why = dlog.buy_engines_idle_reason_for(
            "E*TRADE",
            paper_mode=False,
            etrade_connected=True,
            etrade_environment="live",
            etrade_buying_power=0.0,
        )
        self.assertIn("Live/$0 BP", why)
        note = dlog.etrade_path_honesty_note(
            environment="sandbox", live_trading=False, buying_power=0.0,
        )
        self.assertIn("sandbox", note.lower())
        self.assertIn("parked", note.lower())


class TestActivityLogRotate(unittest.TestCase):
    def test_rotate_keeps_tail(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "activity_log.txt")
            with open(path, "w", encoding="utf-8") as f:
                for i in range(120):
                    f.write(f"line-{i}\n")
            changed = alu.rotate_activity_log_if_needed(
                path, force=True, max_bytes=10, max_lines=50, keep_lines=20,
            )
            self.assertTrue(changed)
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 20)
            self.assertEqual(lines[0], "line-100")
            self.assertEqual(lines[-1], "line-119")

    def test_rotate_archives_head(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "activity_log.txt")
            with open(path, "w", encoding="utf-8") as f:
                for i in range(30):
                    f.write(f"line-{i}\n")
            self.assertTrue(
                alu.rotate_activity_log_if_needed(
                    path, force=True, max_bytes=10, max_lines=10, keep_lines=10, archive=True,
                )
            )
            arch_dir = Path(alu.activity_log_archive_dir(path))
            self.assertTrue(arch_dir.is_dir())
            archives = list(arch_dir.glob("activity_log-*.txt"))
            self.assertEqual(len(archives), 1)
            head = archives[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(head[0], "line-0")
            self.assertEqual(head[-1], "line-19")
            # Second rotate should add another archive, never delete the first
            with open(path, "a", encoding="utf-8") as f:
                for i in range(30, 50):
                    f.write(f"line-{i}\n")
            self.assertTrue(
                alu.rotate_activity_log_if_needed(
                    path, force=True, max_bytes=10, max_lines=10, keep_lines=10, archive=True,
                )
            )
            self.assertGreaterEqual(len(list(arch_dir.glob("activity_log-*.txt"))), 2)

    def test_under_threshold_noop(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "activity_log.txt")
            Path(path).write_text("a\nb\n", encoding="utf-8")
            self.assertFalse(
                alu.rotate_activity_log_if_needed(
                    path, force=False, max_bytes=10_000_000, max_lines=50_000, keep_lines=5000,
                )
            )


class TestCostBasisHonesty(unittest.TestCase):
    def test_rh_cost_bases_list(self):
        pos = {"cost_bases": [{"direct_cost_basis": "200.0"}]}
        self.assertAlmostEqual(_rh_crypto_avg_cost(pos, 2.0), 100.0)

    def test_rh_nested_amount(self):
        pos = {"cost_bases": [{"direct_cost_basis": {"amount": "50.0", "currency_code": "USD"}}]}
        self.assertAlmostEqual(_rh_crypto_avg_cost(pos, 2.0), 25.0)

    def test_rh_unknown_returns_zero(self):
        self.assertEqual(_rh_crypto_avg_cost({"mark_price": "3000"}, 1.0), 0.0)
        self.assertEqual(_rh_crypto_avg_cost({}, 0), 0.0)

    def test_rh_dust_vs_mark_returns_zero(self):
        # RH cost_bases total ≈ $0.0007 invents mega-ROI vs live ~$100 mark
        pos = {
            "cost_bases": [{"direct_cost_basis": "0.0007"}],
            "mark_price": "100.0",
        }
        self.assertEqual(_rh_crypto_avg_cost(pos, 1.0), 0.0)
        self.assertEqual(_rh_crypto_avg_cost(pos, 1.0, mark_price=100.0), 0.0)

    def test_rh_sane_cost_kept(self):
        pos = {"cost_bases": [{"direct_cost_basis": "95.0"}], "mark_price": "100.0"}
        self.assertAlmostEqual(_rh_crypto_avg_cost(pos, 1.0), 95.0)

    def test_rh_empty_cost_bases_falls_through_to_singular(self):
        # robin_stocks documents top-level cost_basis; empty cost_bases[] must not block it
        pos = {
            "cost_bases": [],
            "cost_basis": "12.0",
            "mark_price": "0.000012",
        }
        self.assertAlmostEqual(
            _rh_crypto_avg_cost(pos, 1_000_000, mark_price=0.000012),
            0.000012,
        )

    def test_rh_singular_cost_basis_string(self):
        pos = {"cost_basis": "25.5"}
        self.assertAlmostEqual(_rh_crypto_avg_cost(pos, 2_000_000), 0.00001275)

    def test_cb_position_avg_entry_price(self):
        from broker import _cb_position_avg_entry
        cost, mark = _cb_position_avg_entry({
            "asset": "ETH",
            "total_balance_crypto": 0.5,
            "total_balance_fiat": 1000.0,
            "average_entry_price": {"value": "1900", "currency": "USD"},
        })
        self.assertAlmostEqual(cost, 1900.0)
        self.assertAlmostEqual(mark, 2000.0)

    def test_cb_position_cost_basis_total(self):
        from broker import _cb_position_avg_entry
        cost, _mark = _cb_position_avg_entry({
            "asset": "DOGE",
            "total_balance_crypto": 100.0,
            "cost_basis": {"value": "8.0", "currency": "USD"},
        })
        self.assertAlmostEqual(cost, 0.08)

    def test_cb_dust_rejected(self):
        from broker import _cb_position_avg_entry
        cost, _ = _cb_position_avg_entry({
            "asset": "LINK",
            "total_balance_crypto": 1.0,
            "total_balance_fiat": 15.0,
            "average_entry_price": {"value": "0.001", "currency": "USD"},
        })
        self.assertEqual(cost, 0.0)

    def test_cb_holdings_skip_subdollar_dust(self):
        from broker import CoinbaseAdapter
        cb = CoinbaseAdapter()
        cb.is_connected = True
        cb._fetch_all_accounts = lambda: [
            {
                "currency": "DUST",
                "available_balance": {"value": "0.0001"},
                "hold": {"value": "0"},
            },
            {
                "currency": "ETH",
                "available_balance": {"value": "0.05"},
                "hold": {"value": "0"},
            },
        ]
        cb._cb_spot_avg_costs = lambda: {
            "DUST": {"cost": 0.0, "mark": 0.5},
            "ETH": {"cost": 2000.0, "mark": 3000.0},
        }
        cb.position_is_dust = lambda *a, **k: (False, "")
        rows = cb.get_current_holdings()
        tickers = {r["ticker"] for r in rows}
        self.assertIn("ETH", tickers)
        self.assertNotIn("DUST", tickers)

    def test_usable_avg_cost_helper(self):
        from broker import usable_avg_cost, cost_basis_is_dust
        self.assertTrue(cost_basis_is_dust(0.001, 50.0))
        self.assertEqual(usable_avg_cost(0.001, 50.0), 0.0)
        self.assertAlmostEqual(usable_avg_cost(49.0, 50.0), 49.0)

    def test_rh_whole_share_penny_not_dust(self):
        """GOEVQ-style: 10 shares @ $0.0011 is whole-share sellable, not fractional dust."""
        from broker import RobinhoodAdapter
        rh = RobinhoodAdapter()
        dust, why = rh.position_is_dust("GOEVQ", 10.0, 0.0011, "Ready (Stock)")
        self.assertFalse(dust, why)
        dust_frac, _ = rh.position_is_dust("AAPL", 0.01, 50.0, "Ready (Stock)")
        self.assertTrue(dust_frac)

    def test_format_none_not_fail_none(self):
        msg = RobinhoodAdapter._format_rh_order_error(None, what="crypto sell BONK")
        self.assertNotEqual(msg.lower().strip(), "none")
        self.assertIn("empty response", msg.lower())


class TestDecisionJournalActions(unittest.TestCase):
    def test_rotate_and_scale_in_skip_in_summary(self):
        rows = [
            dlog.build_decision_row(
                broker="Robinhood", ticker="ETH", action="ROTATE_SKIP",
                reason="rotate:no eligible funding name", score=80, is_crypto=True,
            ),
            dlog.build_decision_row(
                broker="Robinhood", ticker="ETH", action="SCALE_IN_SKIP",
                reason="scale_in:missing cost basis", score=75, is_crypto=True,
            ),
            {"action": "BUY", "broker": "Robinhood", "ticker": "SOL"},
        ]
        s = analytics.summarize_decisions(rows)
        self.assertEqual(s["rotate_skips"], 1)
        self.assertEqual(s["scale_in_skips"], 1)
        self.assertEqual(s["skips"], 2)
        self.assertEqual(s["buys"], 1)

    def test_emit_helpers(self):
        captured = []
        dlog.emit_rotate_skip(
            lambda **kw: captured.append(kw),
            broker="Coinbase", ticker="BTC", reason="max rotate/day",
            score=90, is_crypto=True, open_count=3, max_open=3,
        )
        dlog.emit_scale_in_skip(
            lambda **kw: captured.append(kw),
            broker="Robinhood", ticker="ETH", reason="missing cost basis",
            score=70, is_crypto=True,
        )
        dlog.emit_idle_skip(
            lambda **kw: captured.append(kw),
            broker="E*TRADE", reason="Sandbox/no BP — buy engines idle",
            engine="PENNY",
        )
        self.assertEqual(captured[0]["action"], "ROTATE_SKIP")
        self.assertTrue(str(captured[0]["reason"]).startswith("rotate:"))
        self.assertEqual(captured[1]["action"], "SCALE_IN_SKIP")
        self.assertIn("missing cost basis", captured[1]["reason"])
        self.assertEqual(captured[2]["action"], "IDLE_SKIP")
        self.assertTrue(str(captured[2]["reason"]).startswith("idle:"))


if __name__ == "__main__":
    unittest.main()
