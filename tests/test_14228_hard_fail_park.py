"""1.42.28 — hard-fail advisor park, ghost sells, PDT rebuy cool-down."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import activity_log_util as alu


def test_advisor_miss_park_empty_and_insufficient():
    spec = alu.advisor_miss_park_spec(
        "Buy FAIL [NEAR]: RH crypto buy NEAR returned empty response (None)"
    )
    assert spec and spec[0] >= 7200 and spec[1] == "empty_response"
    spec2 = alu.advisor_miss_park_spec(
        "Fail: {'error': 'INSUFFICIENT_FUND', 'message': 'Insufficient balance'}"
    )
    assert spec2 and spec2[0] >= 7200 and spec2[1] == "insufficient_fund"
    assert alu.advisor_miss_park_spec("regime caution — wait") is None


def test_sell_is_ghost_insufficient():
    assert alu.sell_is_ghost_insufficient(
        "Fail: {'error': 'INSUFFICIENT_FUND', 'message': 'Insufficient balance'}"
    )
    assert not alu.sell_is_ghost_insufficient("Skipped: Dust (below CB min)")


class TestPdtRebuy(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        import pdt_guard as pdt
        self.pdt = pdt
        pdt._buys = {}
        pdt._day_trades = []
        pdt._rebuy_until = {}
        pdt._loaded = True
        self._orig = pdt._state_path
        pdt._state_path = lambda: os.path.join(self._td.name, "pdt.json")

    def tearDown(self):
        self.pdt._state_path = self._orig
        self.pdt.clear_rebuy_block()

    def test_rebuy_block_after_day_trade(self):
        pdt = self.pdt
        now = 1_700_000_000.0
        pdt.note_day_trade_rebuy_block("Robinhood", "AMC", minutes=90, ts=now)
        blocked, why = pdt.rebuy_blocked("Robinhood", "AMC", now=now + 60)
        self.assertTrue(blocked)
        self.assertIn("cool-down", why.lower())
        ok, _ = pdt.rebuy_blocked("Robinhood", "AMC", now=now + 91 * 60)
        self.assertFalse(ok)
