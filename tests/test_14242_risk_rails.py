"""1.42.42 — exits while disarmed, trade-memory retention, heat cap, TTP fee floor,
PDT stop reserve, per-broker loss limit, no averaging down."""
import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pdt_guard
import scoring


# --- TTP fee floor -----------------------------------------------------------

def test_ttp_trigger_floor_keeps_exit_above_fees():
    # Coinbase-like 2.4% RT; peak +3.5%, runner trail 1.8% would exit ~+1.6% gross.
    px = scoring.ttp_trigger_price(103.5, 100.0, 0.018, 0.024)
    assert abs(px - 100.0 * (1 + 0.024 + scoring.TTP_LOCK_MIN_OVER_FEES)) < 1e-9


def test_ttp_trigger_uses_trail_when_it_is_higher():
    px = scoring.ttp_trigger_price(110.0, 100.0, 0.012, 0.024)
    assert abs(px - 110.0 * (1 - 0.012)) < 1e-9


def test_ttp_trigger_no_floor_when_peak_never_cleared_fees():
    px = scoring.ttp_trigger_price(102.0, 100.0, 0.012, 0.024)
    assert abs(px - 102.0 * (1 - 0.012)) < 1e-9


# --- Book heat cap -----------------------------------------------------------

def test_heat_full_skips_new_entry():
    d = scoring.risk_sizing_breakdown(
        1000.0, 800.0, 0.04, 0.10, min_dollars=5.0,
        risk_pct_per_trade=0.75, open_risk_dollars=60.0, max_open_risk_pct=6.0,
    )
    assert d["trade"] == 0.0
    assert "book heat full" in str(d["skip_reason"])


def test_heat_partial_still_caps():
    d = scoring.risk_sizing_breakdown(
        1000.0, 800.0, 0.04, 0.10, min_dollars=5.0,
        risk_pct_per_trade=0.75, open_risk_dollars=58.0, max_open_risk_pct=6.0,
    )
    assert d["risk_size"] <= 2.0 / 0.04 + 0.01


# --- Trade memory retention --------------------------------------------------

def _seed_mem(bid, ticker, last_eval, **extra):
    scoring._portfolio_memory.setdefault(bid, {})[ticker] = {
        "highest": 110.0, "buy_time": last_eval - 600, "last_eval": last_eval, **extra,
    }


def test_idle_broker_gap_does_not_wipe_peak():
    bid = "COINBASE"
    now = time.time()
    scoring._portfolio_memory[bid] = {}
    _seed_mem(bid, "SOL", now - 5000, ttp_partial_done=True)
    scoring._broker_last_holding_eval[bid] = now - 5000
    with mock.patch.object(scoring, "save_state"):
        scoring._auto_detect_sales(bid)
    mem = scoring._portfolio_memory[bid]["SOL"]
    assert mem["highest"] == 110.0 and mem["ttp_partial_done"] is True


def test_restart_with_no_eval_stamp_keeps_memory():
    bid = "ROBINHOOD"
    scoring._portfolio_memory[bid] = {}
    _seed_mem(bid, "AAPL", time.time() - 86400)
    scoring._broker_last_holding_eval.pop(bid, None)
    with mock.patch.object(scoring, "save_state"):
        scoring._auto_detect_sales(bid)
    assert "AAPL" in scoring._portfolio_memory[bid]


def test_active_broker_still_detects_sold_name():
    bid = "ETRADE"
    now = time.time()
    scoring._portfolio_memory[bid] = {}
    _seed_mem(bid, "OLD", now - scoring._sale_detect_timeout_sec - 60)
    scoring._broker_last_holding_eval[bid] = now - 30
    with mock.patch.object(scoring, "save_state"):
        scoring._auto_detect_sales(bid)
    assert "OLD" not in scoring._portfolio_memory[bid]
    assert "OLD" in scoring._cooldown_memory[bid]


def test_configure_sale_detect_floor_and_scale():
    assert scoring.configure_sale_detect(45) == scoring.SALE_DETECT_MIN_SEC
    assert scoring.configure_sale_detect(600) == 2400.0
    scoring.configure_sale_detect(45)


def test_mark_position_closed_clears_memory_and_cools_down():
    bid = "COINBASE"
    scoring._portfolio_memory[bid] = {}
    _seed_mem(bid, "FET", time.time(), ttp_partial_done=True)
    with mock.patch.object(scoring, "save_state"):
        assert scoring.mark_position_closed("Coinbase", "FET-USD", 1.23) is True
    assert "FET" not in scoring._portfolio_memory[bid]
    assert scoring._cooldown_memory[bid]["FET"]["sell_price"] == 1.23


# --- PDT reserve -------------------------------------------------------------

def _pdt_state(buys, day_trades):
    return mock.patch.multiple(
        pdt_guard, _buys=buys, _day_trades=day_trades, _loaded=True,
        save=lambda **k: None,
    )


def test_pdt_reserves_slots_for_names_opened_today():
    today = pdt_guard._day_key()
    buys = {"Robinhood|AMD": [{"day": today, "ts": time.time(), "qty": 1}]}
    trades = [{"broker": "Robinhood", "ticker": "X", "day": today}] * 2
    with _pdt_state(buys, trades), mock.patch.object(pdt_guard, "_gate_day_trade_count", return_value=2):
        ok, why = pdt_guard.may_open_equity_buy("Robinhood", "NVDA", equity=5000.0)
        assert not ok and "reserved" in why
        ok_same, _ = pdt_guard.may_open_equity_buy("Robinhood", "AMD", equity=5000.0)
        assert ok_same


def test_pdt_allows_entry_when_stop_slot_is_free():
    with _pdt_state({}, []), mock.patch.object(pdt_guard, "_gate_day_trade_count", return_value=2):
        ok, _ = pdt_guard.may_open_equity_buy("Robinhood", "NVDA", equity=5000.0)
        assert ok


# --- Per-broker daily loss limit --------------------------------------------

def test_broker_loss_limit_scales_to_broker_equity():
    scoring._equity_dd.setdefault("COINBASE", {})["day_open"] = 200.0
    lim = scoring.broker_day_loss_limit("Coinbase", 60.0, {"risk_posture": "balanced"})
    assert lim < 60.0 and lim > 0
    assert scoring.broker_day_loss_limit("Coinbase", 0.0, {}) == 0.0


# --- No averaging down -------------------------------------------------------

def test_scale_in_refuses_losing_position():
    with mock.patch.object(scoring, "get_scale_in_params", return_value={
        "allow_scale_in": True, "scale_in_size_frac": 0.5, "scale_in_max_adds": 1,
        "scale_in_roi_min": -0.03, "scale_in_roi_max": 0.05, "scale_in_near_pct": 0.02,
        "scale_in_min_score": 0.0,
    }):
        res = scoring.evaluate_scale_in("AAPL", 98.0, 100.0, broker_id="ROBINHOOD")
    assert not res["allowed"] and "averaging down" in res["reason"]


def test_postures_default_scale_in_off():
    for name in ("safer", "balanced", "aggressive", "growth"):
        prof = scoring.get_risk_posture_profile(name)
        assert prof.get("allow_scale_in") is False


# --- Exits keep running after disarm ----------------------------------------

def _stub(armed=False, watch=("Coinbase",), setting=True, auth_dead=False):
    import gui as gui_mod
    s = SimpleNamespace(
        auto_trade_enabled={"Coinbase": armed},
        settings={"manage_exits_when_disarmed": setting},
        _broker_manual_auth_needed={"Coinbase": auth_dead},
        _exit_watch_brokers=set(watch),
        cycle_broker_name="Coinbase",
    )
    return gui_mod.MarketAdvisorGUI._exits_managed, s


def test_exits_managed_after_disarm():
    fn, s = _stub()
    assert fn(s, "Coinbase") is True


def test_exits_not_managed_when_setting_off_or_never_armed_or_auth_dead():
    fn, s = _stub(setting=False)
    assert fn(s, "Coinbase") is False
    fn, s = _stub(watch=())
    assert fn(s, "Coinbase") is False
    fn, s = _stub(auth_dead=True)
    assert fn(s, "Coinbase") is False
