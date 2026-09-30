"""
Day profit lock-in: once a broker's day P&L clears an activation level, pause new
buys for the rest of the ET day if it gives back too much of the peak.

Standard day-trader rail ("don't turn a green day red"): exits keep running;
only discretionary entries stop.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

DEFAULT_ACTIVATE_PCT = 1.5   # % of day-open equity before the lock arms
DEFAULT_GIVEBACK_PCT = 40.0  # % of peak day P&L that may be given back
MIN_ACTIVATE_DOLLARS = 2.0   # micro books: don't arm on pennies

_STATE_NAME = "profit_lock.json"
_state: dict[str, dict] = {}  # broker -> {day, peak, locked, reason}
_loaded = False


def _state_path() -> str:
    try:
        from scoring import STATE_DIR
        base = STATE_DIR
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    return os.path.join(str(base), _STATE_NAME)


def load(force: bool = False) -> None:
    global _state, _loaded
    if _loaded and not force:
        return
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        _state = {str(k): v for k, v in (raw or {}).items() if isinstance(v, dict)}
    except Exception:
        _state = {}
    _loaded = True


def save() -> None:
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2)
    except Exception:
        pass


def _day_key(ts: Optional[float] = None) -> str:
    from datetime import datetime
    t = ts or time.time()
    if ZoneInfo is None:
        return datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(t, tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _cfg(settings: Optional[dict]) -> tuple[bool, float, float]:
    s = settings or {}
    enabled = bool(s.get("profit_lock_enabled", True))
    try:
        act = float(s.get("profit_lock_activate_pct", DEFAULT_ACTIVATE_PCT))
    except (TypeError, ValueError):
        act = DEFAULT_ACTIVATE_PCT
    try:
        give = float(s.get("profit_lock_giveback_pct", DEFAULT_GIVEBACK_PCT))
    except (TypeError, ValueError):
        give = DEFAULT_GIVEBACK_PCT
    return enabled, max(0.25, min(20.0, act)), max(10.0, min(90.0, give))


def update(
    broker: str,
    day_pnl: float,
    day_open_equity: float,
    settings: Optional[dict] = None,
    *,
    now: Optional[float] = None,
) -> tuple[bool, str]:
    """Feed a trusted day-P&L read. Returns (tripped_now, message)."""
    load()
    enabled, act_pct, give_pct = _cfg(settings)
    b = str(broker)
    day = _day_key(now)
    st = _state.get(b)
    if not st or st.get("day") != day:
        st = {"day": day, "peak": 0.0, "locked": False, "reason": ""}
        _state[b] = st
    if not enabled:
        return False, ""
    try:
        pnl = float(day_pnl or 0.0)
        eq = float(day_open_equity or 0.0)
    except (TypeError, ValueError):
        return False, ""
    prev_peak = float(st.get("peak") or 0.0)
    st["peak"] = max(prev_peak, pnl)
    if st["peak"] > prev_peak + 0.5:
        save()
    activate = max(MIN_ACTIVATE_DOLLARS, eq * act_pct / 100.0) if eq > 0 else MIN_ACTIVATE_DOLLARS
    peak = float(st["peak"])
    if st.get("locked") or peak < activate:
        return False, ""
    floor = peak * (1.0 - give_pct / 100.0)
    if pnl > floor:
        return False, ""
    st["locked"] = True
    st["reason"] = (
        f"profit lock — day P&L ${pnl:.2f} gave back ≥{give_pct:.0f}% of "
        f"+${peak:.2f} peak; no new buys until tomorrow"
    )
    save()
    return True, st["reason"]


def buys_paused(broker: str, *, now: Optional[float] = None) -> tuple[bool, str]:
    load()
    st = _state.get(str(broker)) or {}
    if st.get("day") != _day_key(now) or not st.get("locked"):
        return False, ""
    return True, str(st.get("reason") or "profit lock")


def clear(broker: Optional[str] = None) -> None:
    load()
    if broker is None:
        _state.clear()
    else:
        _state.pop(str(broker), None)
    save()
