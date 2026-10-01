"""1.42.46 — focus only on brokers that can trade now; E*TRADE request budget and no blind order resends."""
import os
import sys
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
import requests

import auto_cycle as ac
import etrade_client as ec


# --- E*TRADE HTTP ------------------------------------------------------------

def _client():
    c = ec.ETradeClient("k", "s", environment="live")
    c.access_token, c.access_token_secret = "t", "ts"
    c._oauth = lambda *a, **k: None
    return c


def _resp(status=200, body=b"{}"):
    r = mock.Mock()
    r.status_code = status
    r.content = body
    r.text = body.decode()
    r.headers = {"Content-Type": "application/json"}
    r.json.return_value = {}
    return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ec.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ec, "_MIN_REQUEST_GAP_SEC", 0.0)


def test_get_retries_after_read_timeout():
    c = _client()
    c.session.request = mock.Mock(side_effect=[requests.exceptions.ReadTimeout("slow"), _resp()])
    assert c.get("/v1/accounts/list") == {}
    assert c.session.request.call_count == 2


def test_place_order_not_resent_after_read_timeout():
    c = _client()
    c.session.request = mock.Mock(side_effect=requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(requests.exceptions.ReadTimeout):
        c.post_xml("/v1/accounts/abc/orders/place", "<x/>")
    assert c.session.request.call_count == 1


def test_place_order_5xx_raises_unknown_state_without_resend():
    c = _client()
    c.session.request = mock.Mock(return_value=_resp(502, b"bad gateway"))
    with pytest.raises(ec.ETradeAPIError, match="order state unknown"):
        c.post_xml("/v1/accounts/abc/orders/place", "<x/>")
    assert c.session.request.call_count == 1


def test_place_order_retries_when_connection_never_opened():
    c = _client()
    c.session.request = mock.Mock(side_effect=[requests.exceptions.ConnectTimeout("no route"), _resp()])
    assert c.post_xml("/v1/accounts/abc/orders/place", "<x/>") == {}
    assert c.session.request.call_count == 2


def test_preview_5xx_still_retries():
    c = _client()
    c.session.request = mock.Mock(side_effect=[_resp(503, b"busy"), _resp()])
    assert c.post_xml("/v1/accounts/abc/orders/preview", "<x/>") == {}
    assert c.session.request.call_count == 2


def test_request_budget_stops_retry_storm(monkeypatch):
    c = _client()
    clock = iter([0.0, 0.0, 0.0, 60.0, 60.0, 60.0, 60.0, 60.0])
    monkeypatch.setattr(ec.time, "time", lambda: next(clock, 60.0))
    c.session.request = mock.Mock(side_effect=requests.exceptions.ReadTimeout("hung"))
    with pytest.raises(requests.exceptions.ReadTimeout):
        c.get("/v1/accounts/abc/portfolio")
    assert c.session.request.call_count < ec._MAX_RETRIES


def test_http_timeout_fits_inside_stall_watchdog():
    connect, read = ec._HTTP_TIMEOUT
    assert connect + read < 45
    assert ec._REQUEST_BUDGET_SEC < 180


# --- E*TRADE stop wording ----------------------------------------------------

def test_etrade_live_chip_reports_gtc_stops():
    chip, tip, _ = ac.etrade_home_env_chip(environment="live", live_trading=True, buying_power=100.0)
    assert "GTC stops" in chip
    assert "N/A" not in chip and "N/A" not in tip


# --- Desk focus --------------------------------------------------------------

def _gui_stub(*, session_active, et_ok, focus_ctx=None):
    import gui as gui_mod

    brokers = {
        "Robinhood": SimpleNamespace(supports_crypto=True, supports_equities=True),
        "Coinbase": SimpleNamespace(supports_crypto=True, supports_equities=False),
        "E*TRADE": SimpleNamespace(supports_crypto=False, supports_equities=True,
                                   supports_extended_hours=False),
    }
    s = SimpleNamespace(
        brokers=brokers,
        settings={"desk_focus_mode": "auto", "desk_preferred_primary": "E*TRADE"},
        is_equity_session_active=lambda: session_active,
        get_equity_session_info=lambda: {"label": "REGULAR" if et_ok else "EXTENDED"},
    )
    s._broker_supports = lambda name, attr: gui_mod.MarketAdvisorGUI._broker_supports(s, name, attr)
    s._broker_buy_session_open = lambda name: gui_mod.MarketAdvisorGUI._broker_buy_session_open(s, name)
    ctx = focus_ctx if focus_ctx is not None else {
        n: {"can_place_new_buy": True, "deployable_bp": bp}
        for n, bp in (("Robinhood", 60.0), ("Coinbase", 17.0), ("E*TRADE", 99.0))
    }
    s._get_trader_context_map = lambda **_: ctx
    return gui_mod.MarketAdvisorGUI, s


def test_focus_skips_etrade_outside_regular_hours():
    cls, s = _gui_stub(session_active=True, et_ok=False)
    with mock.patch.object(ac, "etrade_equity_session_ok", return_value=(False, "extended")):
        focus = cls._desk_focus_broker(s)
    assert focus == "Robinhood"


def test_focus_skips_etrade_overnight():
    cls, s = _gui_stub(session_active=False, et_ok=False)
    assert cls._desk_focus_broker(s) == "Robinhood"


def test_focus_keeps_etrade_in_regular_session():
    cls, s = _gui_stub(session_active=True, et_ok=True)
    with mock.patch.object(ac, "etrade_equity_session_ok", return_value=(True, "")):
        assert cls._desk_focus_broker(s) == "E*TRADE"


def _rest_stub(focus):
    import gui as gui_mod

    cls, s = _gui_stub(session_active=True, et_ok=True)
    s._desk_focus_broker = lambda: focus
    s._launch_equity_total = lambda: 183.0
    s._last_balance_totals = {}
    s._profit_guard_rest_reason = lambda: ""
    s._dd_paused_for_broker = lambda _b: False
    s._buy_engines_idle_reason = lambda _b: ""
    return gui_mod.MarketAdvisorGUI._buy_engines_should_rest, s


def test_equity_focus_does_not_park_other_brokers_crypto():
    fn, s = _rest_stub("E*TRADE")
    assert fn(s, "Coinbase", engine="CRYPTO") == (False, "")
    assert fn(s, "Robinhood", engine="CRYPTO") == (False, "")


def test_equity_focus_still_parks_other_equity_engines():
    fn, s = _rest_stub("E*TRADE")
    rest, why = fn(s, "Robinhood")
    assert rest and "Desk focus on E*TRADE" in why


def test_crypto_focus_does_not_park_other_crypto_venue():
    fn, s = _rest_stub("Robinhood")
    assert fn(s, "Coinbase", engine="CRYPTO") == (False, "")


def test_focus_cache_fallback_respects_session():
    cls, s = _gui_stub(session_active=False, et_ok=False, focus_ctx={})
    s._desk_focus_broker_cache = "E*TRADE"
    assert cls._desk_focus_broker(s) is None
    s._desk_focus_broker_cache = "Robinhood"
    assert cls._desk_focus_broker(s) == "Robinhood"


def test_focus_filter_error_excludes_broker_instead_of_failing_open():
    cls, s = _gui_stub(session_active=True, et_ok=True)

    def boom(name):
        if name == "E*TRADE":
            raise RuntimeError("session lookup failed")
        return True

    s._broker_buy_session_open = boom
    assert cls._desk_focus_broker(s) == "Robinhood"


# --- 1.42.47: Grok tester findings -------------------------------------------

def test_place_order_429_not_resent():
    c = _client()
    c.session.request = mock.Mock(return_value=_resp(429, b"slow down"))
    with pytest.raises(ec.ETradeAPIError, match="order state unknown"):
        c.post_xml("/v1/accounts/abc/orders/place", "<x/>")
    assert c.session.request.call_count == 1


def test_read_timeout_shrinks_to_remaining_budget(monkeypatch):
    c = _client()
    now = {"t": 0.0}
    monkeypatch.setattr(ec.time, "time", lambda: now["t"])
    seen = []

    def hang(*_a, timeout=None, **_k):
        seen.append(timeout[1])
        now["t"] += timeout[1]
        raise requests.exceptions.ReadTimeout("hung")

    c.session.request = mock.Mock(side_effect=hang)
    with pytest.raises(requests.exceptions.ReadTimeout):
        c.get("/v1/accounts/abc/portfolio")
    assert now["t"] <= ec._REQUEST_BUDGET_SEC + 2.0
    assert seen[-1] <= seen[0]


def test_etrade_buys_whole_shares_only():
    from etrade_broker import ETradeAdapter
    assert ETradeAdapter().supports_fractional_equities is False


def test_crypto_coach_message_drops_share_price_clause():
    import desk_orchestration as do
    msg = do.zero_signal_coach_message(
        broker="Robinhood", engine="CRYPTO", session_label="OVERNIGHT", max_afford_share=57, cycles=12,
    )
    assert "/share" not in msg and "spread" in msg


def _nag_stub(*, intent=True, auth_dead=True, armed=False):
    import gui as gui_mod
    sent = []
    s = SimpleNamespace(
        paper_mode=False,
        brokers={"E*TRADE": object()},
        settings={"etrade_arm_intent": intent},
        _broker_manual_auth_needed={"E*TRADE": auth_dead},
        auto_trade_enabled={"E*TRADE": armed},
        log_event=lambda *_: None,
        send_discord_alert=lambda msg, **kw: sent.append((msg, kw)),
    )
    return gui_mod.MarketAdvisorGUI._maybe_etrade_preopen_reauth_nag, s, sent


def _et_ts(hh, mm, day="2026-10-02"):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    ).timestamp()


def test_preopen_reauth_nag_fires_once():
    fn, s, sent = _nag_stub()
    fn(s, _et_ts(8, 50))
    fn(s, _et_ts(9, 5))
    assert len(sent) == 1 and sent[0][1]["prefix"] == "[REAUTH]"


def test_preopen_reauth_nag_quiet_when_not_needed():
    for kw in ({"intent": False}, {"auth_dead": False}, {"armed": True}):
        fn, s, sent = _nag_stub(**kw)
        fn(s, _et_ts(8, 50))
        assert sent == []
    fn, s, sent = _nag_stub()
    fn(s, _et_ts(10, 0))
    fn(s, _et_ts(8, 50, day="2026-10-03"))
    assert sent == []
