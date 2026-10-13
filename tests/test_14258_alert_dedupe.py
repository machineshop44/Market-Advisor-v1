"""1.42.58 — watchdog Discord dedupe and per-coin RH quote backoff (10/9–10/13 feed)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import desk_watchdog as dw
from activity_log_util import buy_fail_ttl_for_status


def test_watchdog_drops_reauth_and_slow_cycle():
    items = [
        {"code": "reauth", "severity": "critical"},
        {"code": "reauth_log", "severity": "critical"},
        {"code": "cycle_slow", "severity": "warn"},
        {"code": "panic_halt", "severity": "critical"},
    ]
    assert [s["code"] for s in dw.discord_worthy_snags(items)] == ["panic_halt"]


def test_watchdog_keeps_stall_and_errors():
    items = [{"code": "cycle_stall"}, {"code": "buy_batch_error"}]
    assert len(dw.discord_worthy_snags(items)) == 2


def test_no_rh_crypto_quote_backs_off_three_hours():
    st = "Skipped: No RH crypto quote for AMP (session soft-dead or API gap)"
    assert buy_fail_ttl_for_status(st) == 3 * 3600
