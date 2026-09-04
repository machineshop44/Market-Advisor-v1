"""RH crypto quantity / symbol parsing (nested amount payloads)."""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from broker import (  # noqa: E402
    _rh_crypto_position_qty,
    _rh_crypto_symbol,
    _rh_parse_qty,
)


class TestRhCryptoQty(unittest.TestCase):
    def test_parse_qty_string(self):
        self.assertAlmostEqual(_rh_parse_qty("0.00123"), 0.00123)

    def test_parse_qty_nested_amount(self):
        self.assertAlmostEqual(_rh_parse_qty({"amount": "0.004", "currency_code": "BTC"}), 0.004)

    def test_position_prefers_available_when_quantity_zero(self):
        pos = {
            "quantity": "0",
            "quantity_available": {"amount": "0.0025"},
            "currency": {"code": "BTC"},
        }
        self.assertAlmostEqual(_rh_crypto_position_qty(pos), 0.0025)
        self.assertEqual(_rh_crypto_symbol(pos), "BTC")

    def test_nested_quantity_dict_does_not_crash(self):
        # Old float(pos["quantity"]) raised TypeError → silent 0 shares
        pos = {
            "quantity": {"amount": "0.001", "currency_code": "BTC"},
            "currency": {"code": "BTC"},
        }
        self.assertAlmostEqual(_rh_crypto_position_qty(pos), 0.001)

    def test_held_for_sells(self):
        pos = {
            "quantity": 0,
            "quantity_held_for_sells": "0.01",
            "currency_code": "ETH",
        }
        self.assertAlmostEqual(_rh_crypto_position_qty(pos), 0.01)
        self.assertEqual(_rh_crypto_symbol(pos), "ETH")


if __name__ == "__main__":
    unittest.main()
