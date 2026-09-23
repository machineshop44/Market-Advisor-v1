"""1.42.32 — overnight mixed-lot peel, DD Discord episode, watchdog quieting."""
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_rh_defer_pure_frac_only_overnight():
    import auto_cycle as ac

    overnight = {"equity_tradeable": True, "fractional_ok": False, "label": "OVERNIGHT"}
    # Pure fractional still deferred
    why = ac.rh_equity_sell_defer_reason("ACHR", 0.5, 8.0, "stock", overnight)
    assert why and "fractional" in why
    # Mixed lot must proceed so broker can peel whole-share floor
    assert ac.rh_equity_sell_defer_reason("ACHR", 2.99, 8.0, "stock", overnight) is None
    # Whole shares proceed
    assert ac.rh_equity_sell_defer_reason("ACHR", 3.0, 8.0, "stock", overnight) is None


def test_dd_episode_silent_renew():
    import scoring as sc

    bid = "COINBASE_TEST_DD_EP"
    sc._equity_dd.pop(bid, None)
    # First trip → Discord signal (True)
    t1, msg1 = sc.update_equity_drawdown(
        bid, 100.0, posture="balanced",
        settings={"day_dd_pause_pct": 0.06, "peak_dd_pause_pct": 0.50, "dd_pause_minutes": 30},
    )
    assert t1 is False  # first call sets day_open=100, no drawdown yet
    t2, msg2 = sc.update_equity_drawdown(
        bid, 90.0, posture="balanced",
        settings={"day_dd_pause_pct": 0.06, "peak_dd_pause_pct": 0.50, "dd_pause_minutes": 30},
    )
    assert t2 is True
    assert "Day drawdown" in (msg2 or "")
    # Expire pause artificially
    sc._equity_dd[bid]["pause_until"] = time.time() - 1
    # Still underwater → silent renew
    t3, msg3 = sc.update_equity_drawdown(
        bid, 89.0, posture="balanced",
        settings={"day_dd_pause_pct": 0.06, "peak_dd_pause_pct": 0.50, "dd_pause_minutes": 30},
    )
    assert t3 is False
    assert float(sc._equity_dd[bid]["pause_until"]) > time.time()
    sc._equity_dd.pop(bid, None)


def test_watchdog_quiet_dd_and_ranked():
    import desk_watchdog as dw

    status = {
        "brokers": {
            "Coinbase": {
                "armed": True,
                "connected": True,
                "dd_pause": True,
                "dd_reason": "Day drawdown -8.0%",
                "reauth_needed": False,
            },
            "E*TRADE": {
                "armed": True,
                "connected": True,
                "reauth_needed": True,  # stale after reconnect
            },
        },
        "etrade": {"reauth_needed": False},
        "recent_log": [
            "[Coinbase] Ranked 3 buys — top: BTC(70)",
            "[DD] [Coinbase] Day drawdown -8.0% — pausing new buys (30m); auto-trader stays armed",
            "[E*TRADE] token expired — reauth required",
        ],
    }
    report = dw.scan_snags(status)
    codes = {s.get("code") for s in report.get("snags") or []}
    # ranked + dd are INFO — not Discord WARN
    warn_codes = {
        s.get("code")
        for s in (report.get("snags") or [])
        if s.get("severity") == dw.SEV_WARN
    }
    crit_codes = {
        s.get("code")
        for s in (report.get("snags") or [])
        if s.get("severity") == dw.SEV_CRITICAL
    }
    assert "ranked_then_stop" not in warn_codes
    assert "dd_pause" not in warn_codes
    assert "dd_pause_log" not in warn_codes
    # Connected+armed E*TRADE with stale reauth_needed must not CRITICAL
    assert "reauth" not in crit_codes
    # Stale log reauth dropped when no live reauth needed
    assert "reauth_log" not in codes or "reauth_log" not in crit_codes


def test_snag_alert_key_stable():
    import desk_watchdog as dw

    a = {"code": "dd_pause", "broker": "Coinbase", "message": "Day drawdown -8.3%"}
    b = {"code": "dd_pause", "broker": "Coinbase", "message": "Day drawdown -9.8%"}
    assert dw.snag_alert_key(a) == dw.snag_alert_key(b)
