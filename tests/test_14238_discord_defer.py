"""1.42.38/39 — Discord broker tagging + overnight defer/peel honesty."""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestDiscordBrokerTag(unittest.TestCase):
    def test_explicit_wins(self):
        from activity_log_util import resolve_discord_broker_tag

        self.assertEqual(
            resolve_discord_broker_tag("E*TRADE", "[Coinbase] noise"),
            "E*TRADE",
        )

    def test_infer_from_message(self):
        from activity_log_util import resolve_discord_broker_tag

        self.assertEqual(
            resolve_discord_broker_tag(None, "[DD] **[Robinhood]** pause"),
            "Robinhood",
        )
        self.assertEqual(
            resolve_discord_broker_tag("", "Session restored [Coinbase] ok"),
            "Coinbase",
        )

    def test_cycle_only_when_in_cycle(self):
        from activity_log_util import resolve_discord_broker_tag

        self.assertEqual(
            resolve_discord_broker_tag(
                None, "BUY AAPL filled",
                cycle_broker="Coinbase", in_cycle=True,
            ),
            "Coinbase",
        )
        self.assertEqual(
            resolve_discord_broker_tag(
                None, "EOD flatten done",
                cycle_broker="Coinbase", in_cycle=False,
            ),
            "App",
        )

    def test_eod_flatten_label_mixed(self):
        from activity_log_util import eod_flatten_discord_label, discord_broker_from_fills

        tag, label = eod_flatten_discord_label([
            {"broker": "Robinhood"},
            {"broker": "E*TRADE"},
        ])
        self.assertEqual(tag, "App")
        self.assertIn("multi", label.lower())
        tag2, label2 = eod_flatten_discord_label([{"broker": "Robinhood"}])
        self.assertEqual(tag2, "Robinhood")
        self.assertIn("RH", label2)
        self.assertEqual(
            discord_broker_from_fills([{"broker": "E*TRADE"}]),
            "E*TRADE",
        )


class TestEquityBuyDeferSession(unittest.TestCase):
    def test_late_extended_blocks_fractional(self):
        import auto_cycle as ac

        late = {
            "label": "EXTENDED",
            "fractional_ok": False,
            "equity_tradeable": True,
        }
        why = ac.equity_buy_defer_reason(
            "ACHR", 0.4, 8.0, "stock", late, broker_name="Robinhood",
        )
        self.assertIsNotNone(why)
        self.assertIn("extended", why.lower())

    def test_overnight_allows_whole_share(self):
        import auto_cycle as ac

        overnight = {
            "label": "OVERNIGHT",
            "fractional_ok": False,
            "equity_tradeable": True,
        }
        self.assertIsNone(
            ac.equity_buy_defer_reason(
                "ACHR", 2.0, 8.0, "stock", overnight, broker_name="Robinhood",
            )
        )
        self.assertIsNone(
            ac.equity_buy_defer_reason(
                "ACHR", 2.99, 8.0, "stock", overnight, broker_name="Robinhood",
            )
        )

    def test_overnight_blocks_pure_fractional(self):
        import auto_cycle as ac

        overnight = {
            "label": "OVERNIGHT",
            "fractional_ok": False,
            "equity_tradeable": True,
        }
        why = ac.equity_buy_defer_reason(
            "ACHR", 0.5, 8.0, "stock", overnight, broker_name="Robinhood",
        )
        self.assertIsNotNone(why)
        self.assertIn("overnight", why.lower())

    def test_partial_peel_status(self):
        import auto_cycle as ac

        st = "Sell-All partial peel market Filled (2)"
        self.assertTrue(ac.sell_status_is_partial_peel(st, 2.99, 2.0))
        self.assertTrue(ac.sell_status_is_partial_peel("Filled (2)", 2.99, 2.0))
        # Plain short fill leaving a whole share is NOT a peel
        self.assertFalse(ac.sell_status_is_partial_peel("Filled (2)", 3.0, 2.0))


if __name__ == "__main__":
    unittest.main()
