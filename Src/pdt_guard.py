"""
Pattern Day Trader (PDT) guard for sub-$25k equity accounts.

FINRA-style: 4+ day trades in 5 business days on a margin account under $25k
can flag / restrict the account. Crypto is excluded.

A day trade = buy + sell (full or partial close) of the same equity on the same
US/Eastern calendar day.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_STATE_NAME = "pdt_day_trades.json"
_buys: dict[str, list[dict[str, Any]]] = {}  # key broker|TICKER -> [{day, ts, qty}]
_day_trades: list[dict[str, Any]] = []
_rebuy_until: dict[str, float] = {}  # key broker|TICKER -> until ts (persisted)
_loaded = False

DEFAULT_EQUITY_THRESHOLD = 25000.0
DEFAULT_MAX_DAY_TRADES = 3


def _et_now(ts: Optional[float] = None) -> datetime:
    if ZoneInfo is None:
        return datetime.utcfromtimestamp(ts or time.time())
    dt = datetime.fromtimestamp(ts or time.time(), tz=ZoneInfo("America/New_York"))
    return dt


def _day_key(ts: Optional[float] = None) -> str:
    return _et_now(ts).strftime("%Y-%m-%d")


def _state_path() -> str:
    try:
        from scoring import STATE_DIR

        base = STATE_DIR
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    return os.path.join(str(base), _STATE_NAME)


def load(force: bool = False) -> None:
    global _buys, _day_trades, _rebuy_until, _loaded
    if _loaded and not force:
        return
    path = _state_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _buys = raw.get("buys") or {}
            _day_trades = list(raw.get("day_trades") or [])
            rebuy_raw = raw.get("rebuy_until") or {}
            _rebuy_until = {
                str(k): float(v)
                for k, v in rebuy_raw.items()
                if v is not None
            }
        else:
            _buys, _day_trades, _rebuy_until = {}, [], {}
    except Exception:
        _buys, _day_trades, _rebuy_until = {}, [], {}
    _loaded = True


def save(*, now: Optional[float] = None) -> None:
    load()
    path = _state_path()
    # Drop expired rebuy blocks before write
    ts_now = float(now if now is not None else time.time())
    dead = [k for k, until in list(_rebuy_until.items()) if float(until or 0) <= ts_now]
    for k in dead:
        _rebuy_until.pop(k, None)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "buys": _buys,
                    "day_trades": _day_trades[-200:],
                    "rebuy_until": _rebuy_until,
                },
                f,
                indent=2,
            )
    except Exception as e:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "pdt_guard save failed (%s): %s", path, e
            )
        except Exception:
            pass


def _bk(broker: str, ticker: str) -> str:
    return f"{str(broker)}|{str(ticker or '').upper().replace('-USD', '')}"


def _is_business_day(d: datetime) -> bool:
    return d.weekday() < 5


def _business_days_back(from_day: str, n: int = 5) -> set[str]:
    """Return set of up to n business-day keys ending at from_day (inclusive)."""
    out = set()
    try:
        cur = datetime.strptime(from_day, "%Y-%m-%d")
    except Exception:
        return {from_day}
    guard = 0
    while len(out) < n and guard < 20:
        if cur.weekday() < 5:
            out.add(cur.strftime("%Y-%m-%d"))
        cur -= timedelta(days=1)
        guard += 1
    return out


def record_buy(
    broker: str,
    ticker: str,
    *,
    is_crypto: bool = False,
    qty: float = 0.0,
    ts: Optional[float] = None,
) -> None:
    if is_crypto:
        return
    load()
    day = _day_key(ts)
    key = _bk(broker, ticker)
    lst = _buys.setdefault(key, [])
    lst.append({"day": day, "ts": float(ts or time.time()), "qty": float(qty or 0)})
    # Keep recent only
    _buys[key] = lst[-20:]
    save()


def record_sell(
    broker: str,
    ticker: str,
    *,
    is_crypto: bool = False,
    qty: float = 0.0,
    ts: Optional[float] = None,
    force: bool = False,
) -> bool:
    """
    If a same-day buy exists, record a day trade and return True.
    force=True still records (for logging) even when caller will allow the sell.
    """
    if is_crypto:
        return False
    load()
    day = _day_key(ts)
    key = _bk(broker, ticker)
    buys = _buys.get(key) or []
    same_day = [b for b in buys if str(b.get("day")) == day]
    if not same_day:
        return False
    _day_trades.append({
        "broker": str(broker),
        "ticker": str(ticker).upper().replace("-USD", ""),
        "day": day,
        "ts": float(ts or time.time()),
        "qty": float(qty or 0),
        "forced": bool(force),
    })
    # Consume one same-day buy marker
    for i, b in enumerate(buys):
        if str(b.get("day")) == day:
            buys.pop(i)
            break
    _buys[key] = buys
    save()
    return True


def count_day_trades(broker: Optional[str] = None, *, lookback_business_days: int = 5) -> int:
    load()
    today = _day_key()
    window = _business_days_back(today, lookback_business_days)
    n = 0
    for dt in _day_trades:
        if str(dt.get("day")) not in window:
            continue
        if broker and str(dt.get("broker")) != str(broker):
            continue
        n += 1
    return n


def would_be_day_trade(broker: str, ticker: str, *, ts: Optional[float] = None) -> bool:
    load()
    day = _day_key(ts)
    key = _bk(broker, ticker)
    return any(str(b.get("day")) == day for b in (_buys.get(key) or []))


def pdt_applies(equity: float, settings: Optional[dict] = None) -> bool:
    s = settings or {}
    if not bool(s.get("pdt_guard_enabled", True)):
        return False
    try:
        thresh = float(s.get("pdt_equity_threshold", DEFAULT_EQUITY_THRESHOLD) or DEFAULT_EQUITY_THRESHOLD)
    except (TypeError, ValueError):
        thresh = DEFAULT_EQUITY_THRESHOLD
    try:
        eq = float(equity or 0)
    except (TypeError, ValueError):
        eq = 0.0
    # Unknown/zero equity: still apply (fail closed) — balance glitch must not open PDT.
    if eq <= 0:
        return True
    return eq < thresh


def max_day_trades(settings: Optional[dict] = None) -> int:
    s = settings or {}
    try:
        return max(0, int(s.get("pdt_max_day_trades", DEFAULT_MAX_DAY_TRADES) or DEFAULT_MAX_DAY_TRADES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_DAY_TRADES


def _gate_day_trade_count(broker: Optional[str] = None) -> int:
    """Local journal max'd with broker overlay so chip and gates agree."""
    used, source = effective_day_trade_count(broker)
    local_used = count_day_trades(broker)
    if source == "broker":
        return max(used, local_used)
    return local_used


def may_complete_day_trade(
    broker: str,
    ticker: str,
    *,
    equity: float,
    settings: Optional[dict] = None,
    urgent: bool = False,
) -> tuple[bool, str]:
    """
    Gate a same-day equity sell that would count as a day trade.
    urgent=True (hard stop / EOD flatten / panic) always allowed.
    """
    if urgent:
        return True, ""
    if not would_be_day_trade(broker, ticker):
        return True, ""
    if not pdt_applies(equity, settings):
        return True, ""
    used = _gate_day_trade_count(broker)
    cap = max_day_trades(settings)
    if used >= cap:
        return (
            False,
            f"PDT guard — {used}/{cap} day trades in 5 sessions "
            f"(equity ${float(equity):.0f} < ${float((settings or {}).get('pdt_equity_threshold', DEFAULT_EQUITY_THRESHOLD)):.0f}); "
            f"hold overnight or sell as hard-stop/EOD only",
        )
    return True, ""


def may_open_equity_buy(
    broker: str,
    ticker: str,
    *,
    equity: float,
    settings: Optional[dict] = None,
    is_crypto: bool = False,
) -> tuple[bool, str]:
    """
    Gate NEW discretionary equity entries when PDT slots are exhausted.
    Crypto always allowed. When remaining day-trades is 0, block new equity
    names so the desk is not forced into overnight risk without an exit slot.
    Same-day re-entry of a counted day-trade name stays on rebuy_blocked.
    """
    if is_crypto:
        return True, ""
    if not pdt_applies(equity, settings):
        return True, ""
    used = _gate_day_trade_count(broker)
    cap = max_day_trades(settings)
    if used < cap:
        return True, ""
    return (
        False,
        f"PDT entry guard — {used}/{cap} day trades used; "
        f"no discretionary equity buys until tomorrow "
        f"(equity ${float(equity):.0f})",
    )


# Optional broker-reported day-trade count (RH). Local journal remains fallback.
_broker_dt_counts: dict[str, dict[str, Any]] = {}  # broker -> {count, source, ts}


def set_broker_day_trade_count(broker: str, count: Optional[int], *, source: str = "broker") -> None:
    global _broker_dt_counts
    bn = str(broker or "")
    if not bn:
        return
    if count is None:
        _broker_dt_counts.pop(bn, None)
        return
    try:
        n = max(0, int(count))
    except (TypeError, ValueError):
        return
    _broker_dt_counts[bn] = {
        "count": n,
        "source": str(source or "broker"),
        "ts": time.time(),
    }


def effective_day_trade_count(broker: Optional[str] = None) -> tuple[int, str]:
    """Prefer fresh broker count when set; else local journal. Returns (count, source)."""
    bn = str(broker or "")
    entry = _broker_dt_counts.get(bn) if bn else None
    if entry and (time.time() - float(entry.get("ts") or 0)) < 6 * 3600:
        return int(entry.get("count") or 0), str(entry.get("source") or "broker")
    return count_day_trades(broker), "local"


def snapshot(broker: Optional[str] = None, *, equity: float = 0, settings: Optional[dict] = None) -> dict:
    used, source = effective_day_trade_count(broker)
    # Keep may_* gates on local journal for safety; chip can show broker overlay.
    local_used = count_day_trades(broker)
    gate_used = max(used, local_used) if source == "broker" else local_used
    cap = max_day_trades(settings)
    applies = pdt_applies(equity, settings)
    return {
        "enabled": bool((settings or {}).get("pdt_guard_enabled", True)),
        "applies": applies,
        "day_trades": gate_used,
        "max": cap,
        "remaining": max(0, cap - gate_used) if applies else None,
        "equity": float(equity or 0),
        "source": source,
        "local_day_trades": local_used,
        "broker_day_trades": used if source == "broker" else None,
    }


# --- Same-day re-entry cool-down after a counted day-trade exit ---


def note_day_trade_rebuy_block(
    broker: str,
    ticker: str,
    *,
    minutes: float = 90.0,
    ts: Optional[float] = None,
) -> float:
    """Block re-buying this equity until now+minutes. Returns until-ts."""
    load()
    key = _bk(broker, ticker)
    base = float(ts if ts is not None else time.time())
    until = base + max(5.0, float(minutes or 90.0)) * 60.0
    prev = float(_rebuy_until.get(key) or 0)
    _rebuy_until[key] = max(prev, until)
    # Pass the same clock used for `until` so save() does not prune a block
    # that is still live in the caller's timeline (tests + clock skew).
    try:
        save(now=base)
    except Exception:
        pass
    return float(_rebuy_until.get(key) or until)


def rebuy_blocked(
    broker: str,
    ticker: str,
    *,
    now: Optional[float] = None,
) -> tuple[bool, str]:
    """True when a same-day day-trade exit still blocks re-entry."""
    load()
    ts = float(now if now is not None else time.time())
    key = _bk(broker, ticker)
    until = float(_rebuy_until.get(key) or 0)
    if until <= ts:
        _rebuy_until.pop(key, None)
        return False, ""
    mins = max(1, int((until - ts) / 60.0))
    return True, f"PDT re-entry cool-down ({mins}m left after day-trade exit)"


def clear_rebuy_block(broker: Optional[str] = None, ticker: Optional[str] = None) -> None:
    load()
    if broker is None:
        _rebuy_until.clear()
        save()
        return
    if ticker is None:
        prefix = f"{str(broker)}|"
        dead = [k for k in _rebuy_until if k.startswith(prefix)]
        for k in dead:
            _rebuy_until.pop(k, None)
        save()
        return
    _rebuy_until.pop(_bk(broker, ticker), None)
    save()
