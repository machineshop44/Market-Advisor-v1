"""1.42.52 — 10/2–10/5 audit: session-minute stale clock, weekend crypto edge, opening-range
gate, stale-stop grace, noisy-note throttle key, RH no-quote backoff."""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import auto_cycle
import blotter_reconcile
import scoring
from activity_log_util import noisy_note_key

ET = ZoneInfo("America/New_York")


def _ts(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


def test_session_minutes_skip_weekend():
    # LCID: bought Fri 10/2 16:11 ET (after close), sold Mon 10/5 09:33 as "Stale > 3h".
    mins = scoring.equity_session_minutes_between(_ts(2026, 10, 2, 16, 11), _ts(2026, 10, 5, 9, 33))
    assert 2.0 <= mins <= 4.0


def test_session_minutes_intraday():
    mins = scoring.equity_session_minutes_between(_ts(2026, 10, 5, 10, 0), _ts(2026, 10, 5, 12, 0))
    assert abs(mins - 120.0) < 1.0


def test_session_minutes_overnight_counts_only_rth():
    # Mon 15:00 → Tue 10:00 = 60 min Monday + 30 min Tuesday
    mins = scoring.equity_session_minutes_between(_ts(2026, 10, 5, 15, 0), _ts(2026, 10, 6, 10, 0))
    assert abs(mins - 90.0) < 1.0


def test_weekend_crypto_needs_more_edge(monkeypatch):
    monkeypatch.setattr(scoring, "min_entry_edge_pct", lambda *a, **k: 0.04)
    monkeypatch.setattr(scoring, "estimated_signal_edge_pct", lambda s, is_crypto=False: 0.05)
    monkeypatch.setattr(scoring, "_is_et_weekend", lambda now=None: False)
    ok, _ = scoring.new_entry_clears_fees_ok("COINBASE", "SOL-USD", 95, is_crypto=True)
    assert ok
    monkeypatch.setattr(scoring, "_is_et_weekend", lambda now=None: True)
    ok, why = scoring.new_entry_clears_fees_ok("COINBASE", "SOL-USD", 95, is_crypto=True)
    assert not ok and "weekend" in why
    ok, _ = scoring.new_entry_clears_fees_ok(
        "COINBASE", "SOL-USD", 95, is_crypto=True, settings={"crypto_weekend_edge_mult": 1.0},
    )
    assert ok
    # Equities unaffected by the weekend multiplier
    ok, _ = scoring.new_entry_clears_fees_ok("ETRADE", "AMC", 95, is_crypto=False)
    assert ok


def test_opening_range_block():
    blocked, why = auto_cycle.equity_opening_range_block(datetime(2026, 10, 5, 9, 33, tzinfo=ET))
    assert blocked and why
    blocked, _ = auto_cycle.equity_opening_range_block(datetime(2026, 10, 5, 9, 50, tzinfo=ET))
    assert not blocked
    blocked, _ = auto_cycle.equity_opening_range_block(
        datetime(2026, 10, 5, 9, 33, tzinfo=ET), settings={"equity_open_no_entry_min": 0},
    )
    assert not blocked
    # Saturday — no session
    blocked, _ = auto_cycle.equity_opening_range_block(datetime(2026, 10, 3, 9, 33, tzinfo=ET))
    assert not blocked


def test_protective_stale_grace_skips_fresh_stop():
    now = time.time()
    rows = [
        ("COINBASE", "AERO-USD", {"order_id": "a", "set_at": now - 30}),
        ("COINBASE", "BCH-USD", {"order_id": "b", "set_at": now - 3600}),
        ("COINBASE", "XRP-USD", {"order_id": "c"}),
    ]
    stale = blotter_reconcile.protective_stale_tickers(
        set(), rows, broker_id="COINBASE", now=now, grace_sec=300,
    )
    names = {str(x).upper() for x in stale}
    assert not any("AERO" in n for n in names)
    assert any("BCH" in n for n in names)
    assert any("XRP" in n for n in names)


def test_noisy_note_key():
    k1 = noisy_note_key("[Robinhood] Session size [AMP]: crypto weekend x0.5  $12.34")
    k2 = noisy_note_key("[Robinhood] Session size [AMP]: crypto weekend x0.5  $9.87")
    assert k1 and k1 == k2
    assert noisy_note_key("[E*TRADE] Bought AMC 10 @ 4.12") is None


def test_rh_no_quote_backs_off():
    assert auto_cycle.buy_status_should_backoff(
        "Skipped: No RH crypto quote for AMP (session overnight)"
    )


def test_etrade_reauth_quiet_weekend_window():
    q = auto_cycle.etrade_reauth_quiet
    assert q(datetime(2026, 10, 2, 23, 30, tzinfo=ET))       # Fri night token-expiry nudge
    assert q(datetime(2026, 10, 3, 12, 0, tzinfo=ET))        # Saturday
    assert q(datetime(2026, 10, 4, 23, 30, tzinfo=ET))       # Sunday night
    assert q(datetime(2026, 10, 5, 0, 5, tzinfo=ET))         # Mon after midnight expiry
    assert not q(datetime(2026, 10, 5, 8, 45, tzinfo=ET))    # Mon pre-open nag
    assert not q(datetime(2026, 10, 5, 23, 30, tzinfo=ET))   # Mon night (Tue is a session)
    assert not q(datetime(2026, 10, 6, 3, 0, tzinfo=ET))     # Tue early
    assert not q(
        datetime(2026, 10, 3, 12, 0, tzinfo=ET), settings={"etrade_reauth_quiet_off_days": False},
    )


def test_overnight_intent_message_says_as_planned():
    dec, why = auto_cycle.et_eod_overnight_decision(
        "AMC", price=4.00, avg_cost=4.03, has_broker_stop=True, earnings_next=False,
        settings={"et_eod_flatten_mode": "smart"}, overnight_intent=True,
    )
    assert dec == "hold" and "as planned" in why
