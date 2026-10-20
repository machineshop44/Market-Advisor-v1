"""1.42.64 — 10/19: companion health probe paged twice while the desk kept heartbeating.
A live thread must fail twice, 45s apart, before a restart. A dead thread still restarts now."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import monitor


def test_one_slow_probe_waits():
    action, miss = monitor.monitor_probe_action(0, 1000, healthy=False, running=True)
    assert action == "wait" and miss == 1000
    action, miss = monitor.monitor_probe_action(1000, 1020, healthy=False, running=True)
    assert action == "wait" and miss == 1000


def test_second_probe_restarts():
    action, miss = monitor.monitor_probe_action(1000, 1050, healthy=False, running=True)
    assert action == "restart" and miss == 0


def test_dead_thread_restarts_immediately():
    action, _ = monitor.monitor_probe_action(0, 1000, healthy=False, running=False)
    assert action == "restart"


def test_profit_lock_note_is_throttled():
    from activity_log_util import noisy_note_key
    a = noisy_note_key("[E*TRADE] profit lock — day P&L $1.18 gave back ≥40% of +$2.06 peak; no new buys until tomorrow — skipping buy batch")
    b = noisy_note_key("[E*TRADE] profit lock — day P&L $1.18 gave back ≥40% of +$2.06 peak; no new buys until tomorrow — skipping buy batch")
    assert a and a == b


def test_healthy_clears_the_miss():
    action, miss = monitor.monitor_probe_action(1000, 1010, healthy=True, running=True)
    assert action == "ok" and miss == 0
