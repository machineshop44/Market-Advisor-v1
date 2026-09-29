"""1.42.40 — 9/25–9/29 log audit: IONQ locked/DD, advisor miss loop, exits, rotate, watchdog."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import activity_log_util as alu
import auto_cycle as ac
import cost_basis as cb
import desk_watchdog as dw


def test_ionq_is_not_bankruptcy_q():
    for t in ("IONQ", "TQQQ", "SQQQ", "QQQ", "IONQ-USD"):
        assert not ac.is_bankruptcy_q_ticker(t), t
    assert ac.is_bankruptcy_q_ticker("GOEVQ")


def test_ionq_holding_not_locked():
    row = {"ticker": "IONQ", "shares": 0.3455, "price": 47.15, "value": 16.29, "type": "stock"}
    locked, why = ac.classify_locked_holding(row, broker_name="Robinhood")
    assert not locked, why
    goevq = {"ticker": "GOEVQ", "shares": 10, "price": 0.01, "value": 0.1, "type": "stock"}
    assert ac.classify_locked_holding(goevq, broker_name="Robinhood")[0]


def test_ionq_sell_not_dropped_as_locked():
    holdings = [{"ticker": "IONQ", "shares": 0.3455, "price": 44.64, "broker": "Robinhood"}]
    kept, dropped = ac.drop_locked_portfolio_sells([{"ticker": "IONQ"}], holdings)
    assert dropped == [] and len(kept) == 1


def test_otc_filter_keeps_ionq_scoring():
    items = [(0, "IONQ", 0.3, 47.0, "stock"), (1, "GOEVQ", 10, 1.0, "stock")]
    kept, skipped = ac.filter_otc_portfolio_items(items, broker_name="Robinhood")
    assert skipped == ["GOEVQ"]
    assert [i[1] for i in kept] == ["IONQ"]


def test_ionq_not_subtracted_from_effective_equity():
    holdings = [{"ticker": "IONQ", "shares": 0.3455, "price": 47.15, "value": 16.29,
                 "broker": "Robinhood", "type": "stock"}]
    assert ac.locked_value_from_holdings(holdings) == 0.0


def test_advisor_miss_reason_skips_dollars_cap_note():
    notes = [
        "[Robinhood] Advisor dollars cap [AMP]: $7.25",
        "[Robinhood] No buys executed after rank (1/1 candidate(s)) — AMP: Skipped: "
        "No RH crypto quote for AMP (session soft-dead or API gap)",
    ]
    why = alu.advisor_miss_reason(notes, [])
    assert "No RH crypto quote" in why
    assert alu.advisor_miss_park_spec(why)[1] == "no_quote"


def test_advisor_miss_reason_default_when_only_info():
    why = alu.advisor_miss_reason(["[Robinhood] Advisor dollars cap [AMP]: $7.25"], [])
    assert "dollars cap" not in why.lower()


def test_policy_filtered_parks():
    spec = alu.advisor_miss_park_spec(
        "[Coinbase] No buys executed after rank (1/1 candidate(s)) — policy / size / empty after filter"
    )
    assert spec and spec[1] == "policy_filtered"


def test_repeat_miss_parks_on_third():
    times, park = alu.advisor_repeat_miss_spec([], 1000.0)
    assert park is None
    times, park = alu.advisor_repeat_miss_spec(times, 1007.0)
    assert park is None
    times, park = alu.advisor_repeat_miss_spec(times, 1014.0)
    assert park and park[1] == "repeat_miss" and times == []


def test_repeat_miss_window_expires():
    times, _ = alu.advisor_repeat_miss_spec([], 0.0)
    times, _ = alu.advisor_repeat_miss_spec(times, 10.0)
    times, park = alu.advisor_repeat_miss_spec(times, 5000.0)
    assert park is None and times == [5000.0]


def test_empty_response_sell_retries_fast():
    st = "Fail: RH crypto sell BONK returned empty response (None) — auth expired"
    assert alu.sell_fail_ttl_for_status(st) <= 15 * 60


def test_cb_hold_not_released_retries_quickly():
    st = "Fail: Coinbase hold not released (available 0.05 < position 68.6) — retry shortly"
    assert alu.sell_fail_ttl_for_status(st) <= 5 * 60


def test_rotate_would_trip_loss_guard():
    assert ac.rotate_would_trip_loss_guard(-0.0137, 2, 3)
    assert not ac.rotate_would_trip_loss_guard(-0.0137, 1, 3)
    assert not ac.rotate_would_trip_loss_guard(-0.003, 2, 3)
    assert not ac.rotate_would_trip_loss_guard(0.02, 2, 3)


def test_broker_dust_avg_rejected_without_mark():
    cost, src = cb.resolve_holding_cost(
        broker_cost=0.01997172, tracked_cache=0.0, journal_vwap=11.22, last_known=0.0, mark=0.0,
    )
    assert src == "journal_vwap" and abs(cost - 11.22) < 1e-9


def test_broker_avg_still_wins_when_sane():
    cost, src = cb.resolve_holding_cost(
        broker_cost=11.10, tracked_cache=11.22, journal_vwap=0.0, last_known=0.0, mark=0.0,
    )
    assert src == "broker" and cost == 11.10


def test_watchdog_header_uses_new_item_severity():
    items = [{"code": "missing_stop", "severity": dw.SEV_WARN, "message": "x"}]
    assert dw.alert_header_severity(items) == dw.SEV_WARN
    items.append({"code": "reauth", "severity": dw.SEV_CRITICAL, "message": "y"})
    assert dw.alert_header_severity(items) == dw.SEV_CRITICAL
