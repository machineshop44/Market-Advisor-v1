"""1.42.27 — consecutive-loss must park advisor props, not re-apply every cycle."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import activity_log_util as alu


def test_insufficient_fund_sell_ttl_is_long():
    ttl = alu.sell_fail_ttl_for_status(
        "Fail: {'error': 'INSUFFICIENT_FUND', 'message': 'Insufficient balance'}"
    )
    assert ttl >= 6 * 3600


def test_empty_response_still_long():
    ttl = alu.sell_fail_ttl_for_status("Fail: RH crypto sell NEAR returned empty response (None)")
    assert ttl >= 7200
