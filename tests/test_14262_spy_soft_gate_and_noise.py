"""1.42.62 — 10/14–10/15 audit: soft SPY gate for small-book equities, buy-note throttles,
re-arm Discord after a REAUTH alert."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import scoring
from activity_log_util import noisy_note_key


def test_soft_gate_allows_mild_dip():
    assert scoring.spy_soft_regime_ok(lambda: (-0.002, -0.004))[0]


def test_soft_gate_blocks_clear_downtrend():
    ok, why = scoring.spy_soft_regime_ok(lambda: (-0.006, -0.002))
    assert not ok and "Regime" in why and "1H trend" in why
    ok, why = scoring.spy_soft_regime_ok(lambda: (0.001, -0.011))
    assert not ok and "on the day" in why


def test_soft_gate_falls_back_to_strict_when_dark(monkeypatch):
    monkeypatch.setattr(scoring, "market_regime_ok", lambda is_crypto=False: (False, "strict"))
    assert scoring.spy_soft_regime_ok(lambda: None) == (False, "strict")


def test_entry_regime_uses_soft_for_equities_only(monkeypatch):
    monkeypatch.setattr(scoring, "spy_soft_regime_ok", lambda snapshot_fn=None: (True, "soft"))
    monkeypatch.setattr(scoring, "market_regime_ok", lambda is_crypto=False: (False, "strict"))
    monkeypatch.setattr(scoring, "equity_regime_required", lambda posture=None: True)
    assert scoring.entry_regime_ok(False, "growth", ticker="SOFI") == (True, "soft")
    # BTC-proxy equities keep the strict BTC gate
    assert scoring.entry_regime_ok(False, "growth", ticker="MSTR") == (False, "strict")


def test_hold_bias_and_regime_notes_are_throttled():
    a = noisy_note_key("[Robinhood] Hold bias skip [BONK]: DO NOT BUY (Fee gate: est edge 3.42% < need 3.65% RT+edge)")
    b = noisy_note_key("[Robinhood] Hold bias skip [BONK]: DO NOT BUY (Fee gate: est edge 3.40% < need 3.65% RT+edge)")
    assert a and a == b
    assert noisy_note_key("[Robinhood] Regime blocked buy [SOFI]: DO NOT BUY (Regime: SPY -0.61% vs 1H trend)")


def test_rh_crypto_spread_judged_beyond_markup():
    scoring.configure_entry_filters({"liquidity_gate_enabled": True})
    rh_quote = (100.0, 101.95)  # ~1.94% — RH's built-in markup, every coin on 10/15
    assert scoring.entry_liquidity_block("XLM", is_crypto=True, bid_ask=rh_quote, broker_id="ROBINHOOD") == ""
    assert "Wide spread" in scoring.entry_liquidity_block("XLM", is_crypto=True, bid_ask=rh_quote, broker_id="COINBASE")
    wide = scoring.entry_liquidity_block("XLM", is_crypto=True, bid_ask=(100.0, 104.0), broker_id="ROBINHOOD")
    assert "beyond" in wide and "RH markup" in wide
    # stocks on RH unaffected
    assert "Wide spread" in scoring.entry_liquidity_block("XYZ", is_crypto=False, bid_ask=(10.0, 10.2), broker_id="ROBINHOOD")


def test_etrade_404_after_renew_keeps_session():
    import etrade_broker
    from etrade_client import ETradeAPIError

    class Client:
        access_token = "t"
        access_token_secret = "s"

        def get_balance(self, key):
            raise ETradeAPIError("GET balance failed (404)", status_code=404)

        def renew_access_token(self):
            pass

    b = etrade_broker.ETradeAdapter.__new__(etrade_broker.ETradeAdapter)
    b.client = Client()
    b.environment = "live"
    b.account_id_key = "k"
    b.token_expires_at = None
    b.is_connected = True
    orig = etrade_broker.store_etrade_secret
    etrade_broker.store_etrade_secret = lambda *a, **k: None
    try:
        ok, why = b.ensure_session()
    finally:
        etrade_broker.store_etrade_secret = orig
    assert not ok and "session kept" in why and b.is_connected
    import gui
    assert not gui._is_manual_auth_failure(why)

    class Dead(Client):
        def get_balance(self, key):
            raise ETradeAPIError("GET balance failed (401)", status_code=401)

    b.client = Dead()
    etrade_broker.store_etrade_secret = lambda *a, **k: None
    try:
        ok, why = b.ensure_session()
    finally:
        etrade_broker.store_etrade_secret = orig
    assert not ok and "Reauthorization required" in why and not b.is_connected


def test_no_buys_line_names_advisor_handoff():
    from activity_log_util import explain_no_buys_after_rank

    notes = []
    line = explain_no_buys_after_rank(
        notes, [], buys_done=0, orig_n=1, ranked_n=1, broker_name="Coinbase",
        advisor_tickers=["ATOM"],
    )
    assert "sent to advisor: ATOM" in line and "empty after filter" not in line


def test_reauth_alert_reopens_rearm_discord():
    import gui

    class Stub:
        _buy_skip_throttle = {"E*TRADE:rearm_discord": 123.0, "other": 1.0}

    s = Stub()
    gui.MarketAdvisorGUI._reopen_rearm_discord(s, "E*TRADE")
    assert "E*TRADE:rearm_discord" not in s._buy_skip_throttle
    assert "other" in s._buy_skip_throttle
