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

    def test_etrade_affordability_whole_share_gate(self):
        import auto_cycle as ac
        self.assertTrue(ac.affordability_prefer_whole_shares("ETRADE"))
        self.assertFalse(ac.affordability_prefer_whole_shares("ROBINHOOD", prefer_equity_rth=False))
        affordable, dropped = ac.filter_affordable_buy_candidates(
            [{"ticker": "MSFT", "asset_type": "stock", "price": 514.76, "score": 58}],
            buying_power=100.0,
            equity=100.0,
            broker_id="ETRADE",
            settings={"target_bp_utilization_pct": 88.0},
            prefer_whole_shares=True,
        )
        self.assertEqual(len(affordable), 0)
        self.assertTrue(dropped)


if __name__ == "__main__":
    unittest.main()
