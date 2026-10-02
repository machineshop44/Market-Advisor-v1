import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))

import auto_cycle
import loss_streak
import scoring

ET = ZoneInfo("America/New_York")


def _et(h, m, day=2):
    return datetime(2026, 10, day, h, m, tzinfo=ET)


def test_etrade_entry_blocked_inside_flatten_window():
    blk, why = auto_cycle.etrade_entry_near_flatten_block(_et(15, 17), settings={})
    assert blk is True
    assert "EOD flatten" in why


def test_etrade_entry_allowed_before_window():
    blk, _ = auto_cycle.etrade_entry_near_flatten_block(_et(14, 30), settings={})
    assert blk is False


def test_etrade_entry_window_off_when_flatten_disabled():
    blk, _ = auto_cycle.etrade_entry_near_flatten_block(
        _et(15, 30), settings={"et_flatten_before_close": False},
    )
    assert blk is False


def test_etrade_entry_window_zero_disables():
    blk, _ = auto_cycle.etrade_entry_near_flatten_block(
        _et(15, 30), settings={"et_no_entry_before_flatten_min": 0},
    )
    assert blk is False


def test_etrade_entry_window_respects_early_close():
    # Day after Thanksgiving 2026 = Nov 27, close 13:00 → flatten 12:50
    blk, _ = auto_cycle.etrade_entry_near_flatten_block(
        datetime(2026, 11, 27, 12, 0, tzinfo=ET), settings={},
    )
    assert blk is True


def test_small_book_peak_recovers_when_flat(monkeypatch):
    monkeypatch.setattr(scoring, "save_state", lambda force=False: None)
    scoring._equity_dd["COINBASE"] = {
        "day": "", "day_open": 20.3, "peak": 20.35,
        "pause_until": 9e12, "pause_reason": "Peak drawdown -14.2% ≤ −14%",
        "peak_dd_streak": 2, "dd_episode_active": True,
    }
    ok, msg = scoring.maybe_recover_peak_for_cash_heavy_book(
        "COINBASE", 17.46, 17.46, 0.0, settings={},
    )
    assert ok is True
    assert scoring._equity_dd["COINBASE"]["pause_until"] == 0.0
    assert "reset" in msg.lower()


def test_loss_streak_expires_after_24h(monkeypatch):
    monkeypatch.setattr(loss_streak, "save", lambda: None)
    monkeypatch.setattr(loss_streak, "_loaded", True)
    monkeypatch.setattr(loss_streak, "_streak", {})
    t0 = 1_000_000.0
    loss_streak.record_exit_result("Coinbase", was_loss=True, now=t0)
    loss_streak.record_exit_result("Coinbase", was_loss=True, now=t0 + 86400)
    loss_streak.record_exit_result("Coinbase", was_loss=True, now=t0 + 4 * 86400)
    assert loss_streak.streak_count("Coinbase") == 1
    tripped, _ = loss_streak.maybe_trip_pause("Coinbase", max_losses=3, now=t0 + 4 * 86400)
    assert tripped is False


def test_loss_streak_counts_within_window(monkeypatch):
    monkeypatch.setattr(loss_streak, "save", lambda: None)
    monkeypatch.setattr(loss_streak, "_loaded", True)
    monkeypatch.setattr(loss_streak, "_streak", {})
    t0 = 1_000_000.0
    for i in range(3):
        loss_streak.record_exit_result("Robinhood", was_loss=True, now=t0 + i * 3600)
    assert loss_streak.streak_count("Robinhood") == 3


class _FakeCB:
    def __init__(self, seq):
        self.seq = list(seq)

    def _available_base_qty(self, ticker):
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]


def test_coinbase_stop_qty_uses_fee_net_balance():
    from gui import MarketAdvisorGUI
    q = MarketAdvisorGUI._coinbase_stop_qty(_FakeCB([0.0521621]), "BCH", 0.05222, delay=0)
    assert abs(q - 0.0521621) < 1e-9


def test_coinbase_stop_qty_falls_back_haircut_when_unknown():
    from gui import MarketAdvisorGUI
    q = MarketAdvisorGUI._coinbase_stop_qty(_FakeCB([0.0]), "BCH", 0.05222, tries=2, delay=0)
    assert abs(q - 0.05222 * 0.99) < 1e-12
