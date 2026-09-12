"""
Broker parity matrix — RH session rules must not silently block ET/CB.

Run: python -m pytest tests/test_broker_parity.py -q
"""
import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import auto_cycle as ac  # noqa: E402


# Living matrix: gates that apply per broker (True = gate may block that venue).
# Update when adding a new cross-broker skip — tests assert the dangerous cells.
PARITY_MATRIX = {
    # gate_name: {Robinhood, Coinbase, E*TRADE}
    "rh_overnight_frac_buy_defer": {"Robinhood": True, "Coinbase": False, "E*TRADE": False},
    "rh_session_fractional_ok_for_orders": {"Robinhood": True, "Coinbase": False, "E*TRADE": False},
    "rh_equity_sell_defer": {"Robinhood": True, "Coinbase": False, "E*TRADE": False},
    "equity_session_buy_engines": {"Robinhood": True, "Coinbase": False, "E*TRADE": True},
    "crypto_engines": {"Robinhood": True, "Coinbase": True, "E*TRADE": False},
    "fee_gate_new_entry": {"Robinhood": True, "Coinbase": True, "E*TRADE": True},
    "live_trading_kill_switch": {"Robinhood": False, "Coinbase": True, "E*TRADE": True},
    "et_sandbox_zero_bp_park": {"Robinhood": False, "Coinbase": False, "E*TRADE": True},
}


class TestBrokerParityMatrix(unittest.TestCase):
    def test_matrix_keys_complete(self):
        for gate, row in PARITY_MATRIX.items():
            self.assertEqual(
                set(row),
                {"Robinhood", "Coinbase", "E*TRADE"},
                msg=f"{gate} missing a broker column",
            )

    def test_late_ext_fractional_flags(self):
        late = {
            "label": "EXTENDED",
            "use_ext": True,
            "market_hours": "extended_hours",
            "fractional_ok": False,
        }
        for broker, expect_rh_frac in PARITY_MATRIX["rh_session_fractional_ok_for_orders"].items():
            flags = ac.order_session_flags(broker, late)
            if expect_rh_frac:
                self.assertFalse(flags["allow_fractional"], broker)
            else:
                self.assertTrue(flags["allow_fractional"], broker)

    def test_buy_defer_matrix(self):
        sess = {"label": "REGULAR", "fractional_ok": True}
        for broker, applies in PARITY_MATRIX["rh_overnight_frac_buy_defer"].items():
            why = ac.equity_buy_defer_reason(
                "TEST", 0.25, 40.0, "stock", sess, broker_name=broker,
            )
            if applies:
                self.assertIsNotNone(why, broker)
            else:
                self.assertIsNone(why, broker)


class TestSilentExceptLongLived(unittest.TestCase):
    """Regression: monitor serve loop must not swallow exits without clearing state."""

    def test_monitor_is_running_requires_alive_thread(self):
        import monitor

        # Fresh module state after other tests may have stopped — ensure clean
        monitor.stop_monitor()
        self.assertFalse(monitor.is_running())
        # describe_runtime exposes thread_alive for companion UI / watchdog
        rt = monitor.describe_runtime()
        self.assertIn("thread_alive", rt)
        self.assertFalse(rt["running"])


if __name__ == "__main__":
    unittest.main()
