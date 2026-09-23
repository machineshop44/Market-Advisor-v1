"""1.42.31 — overnight/hours-mismatch sell backoff + buy hours TTL."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import activity_log_util as alu
import auto_cycle as ac


def test_overnight_sell_backoff():
    st = "Skipped: Overnight/late session — RH blocks fractional equity sells"
    assert ac.sell_status_should_backoff(st)
    assert alu.sell_fail_ttl_for_status(st) >= 2 * 3600


def test_no_quote_sell_backoff():
    st = "Skipped: No RH crypto quote for AMP (session soft-dead or API gap)"
    assert ac.sell_status_should_backoff(st)
    assert alu.sell_fail_ttl_for_status(st) >= 2 * 3600


def test_hours_mismatch_buy_ttl():
    st = "Fail: {'non_field_errors': ['Extended hours and market hours mismatch.']}"
    assert ac.buy_status_should_backoff(st)
    assert alu.buy_fail_ttl_for_status(st) >= 30 * 60
    assert alu.sell_fail_ttl_for_status(st) >= 30 * 60
