"""1.42.16 — PDT / consecutive-loss / session size / working-order rails."""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestPdtGuard(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        import pdt_guard as pdt
        self.pdt = pdt
        pdt._buys = {}
        pdt._day_trades = []
        pdt._loaded = True
        self._orig_path = pdt._state_path
        pdt._state_path = lambda: os.path.join(self._td.name, "pdt.json")

    def tearDown(self):
        self.pdt._state_path = self._orig_path

    def test_blocks_fourth_day_trade_under_25k(self):
        pdt = self.pdt
        settings = {"pdt_guard_enabled": True, "pdt_max_day_trades": 3, "pdt_equity_threshold": 25000}
        for i in range(3):
            t = f"T{i}"
            pdt.record_buy("E*TRADE", t, qty=1)
            self.assertTrue(pdt.record_sell("E*TRADE", t, qty=1))
        self.assertEqual(pdt.count_day_trades("E*TRADE"), 3)
        pdt.record_buy("E*TRADE", "NEW", qty=1)
        ok, why = pdt.may_complete_day_trade(
            "E*TRADE", "NEW", equity=500.0, settings=settings, urgent=False,
        )
        self.assertFalse(ok)
        self.assertIn("PDT", why)
        ok_u, _ = pdt.may_complete_day_trade(
            "E*TRADE", "NEW", equity=500.0, settings=settings, urgent=True,
        )
        self.assertTrue(ok_u)

    def test_crypto_ignored(self):
        pdt = self.pdt
        pdt.record_buy("Robinhood", "BTC", is_crypto=True, qty=0.01)
        self.assertFalse(pdt.would_be_day_trade("Robinhood", "BTC"))
        self.assertFalse(pdt.record_sell("Robinhood", "BTC", is_crypto=True, qty=0.01))


class TestLossStreak(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        import loss_streak as ls
        self.ls = ls
        ls._streak = {}
        ls._loaded = True
        self._orig = ls._state_path
        ls._state_path = lambda: os.path.join(self._td.name, "ls.json")

    def tearDown(self):
        self.ls._state_path = self._orig

    def test_trip_pause_and_block_buys(self):
        ls = self.ls
        now = 1_700_000_000.0
        for _ in range(3):
            ls.record_exit_result("Robinhood", was_loss=True, now=now)
        tripped, msg = ls.maybe_trip_pause(
            "Robinhood", max_losses=3, pause_minutes=45, now=now,
        )
        self.assertTrue(tripped)
        self.assertIn("pausing", msg)
        paused, why = ls.buys_paused("Robinhood", now=now + 60)
        self.assertTrue(paused)
        self.assertIn("pause", why)
        cleared, _ = ls.buys_paused("Robinhood", now=now + 46 * 60)
        self.assertFalse(cleared)

    def test_win_resets_streak(self):
        ls = self.ls
        ls.record_exit_result("Coinbase", was_loss=True)
        ls.record_exit_result("Coinbase", was_loss=True)
        ls.record_exit_result("Coinbase", was_loss=False)
        self.assertEqual(ls.streak_count("Coinbase"), 0)

    def test_pause_remaining_sec(self):
        ls = self.ls
        now = 1_700_000_000.0
        for _ in range(3):
            ls.record_exit_result("Coinbase", was_loss=True, now=now)
        ls.maybe_trip_pause("Coinbase", max_losses=3, pause_minutes=45, now=now)
        rem = ls.pause_remaining_sec("Coinbase", now=now + 5 * 60)
        self.assertGreater(rem, 39 * 60)
        self.assertLessEqual(rem, 40 * 60)
        self.assertEqual(ls.pause_remaining_sec("Coinbase", now=now + 50 * 60), 0.0)


class TestWorkingOrders(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        import working_orders as wo
        self.wo = wo
        wo._orders = {}
        wo._loaded = True
        self._orig = wo._state_path
        wo._state_path = lambda: os.path.join(self._td.name, "wo.json")

    def tearDown(self):
        self.wo._state_path = self._orig

    def test_open_notional_and_resolve(self):
        wo = self.wo
        wo.register(
            broker="E*TRADE", order_id="1", side="BUY", ticker="ABC",
            dollars=40.0, status="pending",
        )
        self.assertEqual(wo.open_notional("E*TRADE"), 40.0)
        wo.resolve("E*TRADE", "1", "filled")
        self.assertEqual(wo.open_notional("E*TRADE"), 0.0)

    def test_should_book_fill(self):
        wo = self.wo
        self.assertTrue(wo.should_book_fill("Filled @ $10", spent=10))
        self.assertFalse(wo.should_book_fill("Submitted — pending fill", spent=0))


class TestSessionSizeMult(unittest.TestCase):
    def test_open_half_and_last_30m(self):
        import auto_cycle as ac
        from datetime import timezone
        try:
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
        except Exception:
            et = timezone.utc
        open_t = datetime(2026, 3, 10, 9, 45, tzinfo=et)
        mid = datetime(2026, 3, 10, 12, 0, tzinfo=et)
        late = datetime(2026, 3, 10, 15, 45, tzinfo=et)
        m1, w1 = ac.equity_session_size_mult(open_t)
        m2, _ = ac.equity_session_size_mult(mid)
        m3, w3 = ac.equity_session_size_mult(late)
        self.assertEqual(m1, 0.5)
        self.assertIn("half", w1)
        self.assertEqual(m2, 1.0)
        self.assertEqual(m3, 0.0)
        self.assertIn("last 30m", w3)


class TestInterleave(unittest.TestCase):
    def test_round_robin(self):
        import auto_cycle as ac
        q = [("RH", 1), ("RH", 2), ("ET", 1), ("CB", 1), ("RH", 3)]
        out = ac.interleave_tasks_by_broker(q)
        brokers = [x[0] for x in out]
        self.assertEqual(brokers[:3], ["RH", "ET", "CB"])


if __name__ == "__main__":
    unittest.main()
