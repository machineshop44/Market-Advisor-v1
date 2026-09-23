"""
Working-order ledger — track order_id → fill/cancel before booking basis/BP.

Lightweight honesty layer (joint-audit P0). Does not replace broker confirm_order;
it records what we believe is still open so restarts and UI can see pending risk.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

_STATE_NAME = "working_orders.json"
_orders: dict[str, dict[str, Any]] = {}
_loaded = False


def _state_path() -> str:
    try:
        from scoring import STATE_DIR

        base = STATE_DIR
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    return os.path.join(str(base), _STATE_NAME)


def load(force: bool = False) -> None:
    global _orders, _loaded
    if _loaded and not force:
        return
    path = _state_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                _orders = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
            else:
                _orders = {}
        else:
            _orders = {}
    except Exception:
        _orders = {}
    _loaded = True


def save() -> None:
    load()
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_orders, f, indent=2)
    except Exception:
        pass


def _key(broker: str, order_id: str) -> str:
    return f"{str(broker)}|{str(order_id)}"


def register(
    *,
    broker: str,
    order_id: str,
    side: str,
    ticker: str,
    qty: float = 0.0,
    dollars: float = 0.0,
    status: str = "submitted",
) -> None:
    """Book a newly submitted order (not yet confirmed filled)."""
    load()
    oid = str(order_id or "").strip()
    if not oid:
        return
    _orders[_key(broker, oid)] = {
        "broker": str(broker),
        "order_id": oid,
        "side": str(side or "").upper(),
        "ticker": str(ticker or "").upper(),
        "qty": float(qty or 0),
        "dollars": float(dollars or 0),
        "status": str(status or "submitted"),
        "ts": time.time(),
    }
    save()


def resolve(broker: str, order_id: str, state: str) -> None:
    """Mark filled / cancelled / rejected and drop from open ledger."""
    load()
    oid = str(order_id or "").strip()
    if not oid:
        return
    k = _key(broker, oid)
    entry = _orders.pop(k, None)
    if entry is not None:
        # Keep a short trail under terminal key for debugging (optional — drop to keep file small)
        pass
    save()


def mark_status(broker: str, order_id: str, status: str) -> None:
    load()
    oid = str(order_id or "").strip()
    if not oid:
        return
    k = _key(broker, oid)
    if k not in _orders:
        return
    _orders[k]["status"] = str(status or "")
    _orders[k]["ts"] = time.time()
    st = str(status or "").upper()
    if any(x in st for x in ("FILL", "CANCEL", "REJECT", "EXPIRED")):
        _orders.pop(k, None)
    save()


def open_orders(broker: Optional[str] = None) -> list[dict]:
    load()
    out = []
    for v in _orders.values():
        if broker and str(v.get("broker")) != str(broker):
            continue
        out.append(dict(v))
    return out


def open_notional(broker: Optional[str] = None) -> float:
    """Sum of dollars on open BUY working orders (BP tied estimate)."""
    expire_stale()
    total = 0.0
    for o in open_orders(broker):
        if str(o.get("side") or "").upper() != "BUY":
            continue
        try:
            total += float(o.get("dollars") or 0)
        except (TypeError, ValueError):
            pass
    return total


def expire_stale(*, ttl_sec: float = 7200.0, now: Optional[float] = None) -> int:
    """Drop working orders older than ttl (default 2h) so BP reserve cannot stick forever."""
    load()
    ts_now = float(now if now is not None else time.time())
    ttl = max(300.0, float(ttl_sec or 7200.0))
    dead = []
    for k, v in list(_orders.items()):
        try:
            age = ts_now - float(v.get("ts") or 0)
        except (TypeError, ValueError):
            age = ttl + 1
        if age >= ttl:
            dead.append(k)
    for k in dead:
        _orders.pop(k, None)
    if dead:
        save()
    return len(dead)


def should_book_fill(status: str, *, spent: float = 0.0) -> bool:
    """True only when status clearly indicates a filled order (basis/stop safe)."""
    st = str(status or "")
    if "Fail" in st or "Skipped" in st:
        return False
    if "[PAPER]" in st:
        return True
    if "Filled" not in st:
        return False
    try:
        return float(spent or 0) > 0 or "Sell" in st or "SELL" in st.upper()
    except (TypeError, ValueError):
        return "Filled" in st


def is_working_unfilled(status: str) -> bool:
    """Delegate to auto_cycle helper when available."""
    try:
        from auto_cycle import buy_order_is_working_unfilled

        return bool(buy_order_is_working_unfilled(status))
    except Exception:
        st = str(status or "").lower()
        return "pending fill" in st or ("submitted" in st and "filled" not in st)
