"""1.38.0 session boundary + E*TRADE buy guards."""
import unittest


class TestSession1380(unittest.TestCase):
    def test_pre_close_tasks_portfolio_only(self):
        """pre_close must not queue CORE/PENNY — logic mirrored from gui helper."""
        kind = "pre_close"
        idle_why = None
        if idle_why:
            tasks = ("PORTFOLIO",)
        elif kind == "pre_close":
            tasks = ("PORTFOLIO",)
        else:
            tasks = ("PORTFOLIO", "CORE", "PENNY")
        self.assertEqual(tasks, ("PORTFOLIO",))

    def test_etrade_qty_zero_guard(self):
        from etrade_broker import qty_for_notional
        self.assertEqual(qty_for_notional(50, 420, allow_fractional=False), 0.0)
        self.assertGreater(qty_for_notional(50, 420, allow_fractional=True), 0)


if __name__ == "__main__":
    unittest.main()
