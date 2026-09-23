"""1.42.30 — hours-mismatch park + advisor apply serialization helpers."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import activity_log_util as alu


def test_hours_mismatch_parks():
    spec = alu.advisor_miss_park_spec(
        "Fail: {'non_field_errors': ['Extended hours and market hours mismatch.']}"
    )
    assert spec and spec[1] == "hours_mismatch"
    assert spec[0] >= 600


def test_rotate_cap_parks():
    spec = alu.advisor_miss_park_spec(
        "[Robinhood] [ROTATE] skipped — daily rotate cap (2/2)"
    )
    assert spec and spec[1] == "rotate_cap"
