"""
Desk radar — persistent top scored names across CRYPTO / BREAKOUT / CORE.

Used by Home glance, monitor companion alerts, and scanner explain-why.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

_RADAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desk_radar.json")
_MAX_ITEMS = 24
_MAX_AGE_SEC = 6 * 3600


def _now() -> float:
    return time.time()


def load_radar() -> list[dict]:
    if not os.path.exists(_RADAR_PATH):
        return []
    try:
        with open(_RADAR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict)]
    except Exception:
        return []


def save_radar(items: list[dict]) -> None:
    try:
        payload = {"updated_at": _now(), "items": list(items)[:_MAX_ITEMS]}
        with open(_RADAR_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=0)
    except Exception:
        pass


def upsert_candidates(
    candidates: list[dict] | None,
    *,
    engine: str,
    broker: str = "",
    max_keep: int = _MAX_ITEMS,
) -> list[dict]:
    """
    Merge scored buy candidates into the radar (higher score wins same ticker+engine).
    Candidate keys: ticker, score, reason/action optional, asset_type optional.
    """
    now = _now()
    items = [x for x in load_radar() if (now - float(x.get("ts") or 0)) < _MAX_AGE_SEC]
    by_key: dict[tuple, dict] = {}
    for it in items:
        key = (str(it.get("ticker") or "").upper(), str(it.get("engine") or "").upper())
        if key[0]:
            by_key[key] = it

    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        ticker = str(c.get("ticker") or c.get("symbol") or "").upper().replace("-USD", "")
        if not ticker:
            continue
        eng = str(engine or c.get("engine") or "SCAN").upper()
        try:
            score = float(c.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        key = (ticker, eng)
        prev = by_key.get(key)
        if prev and float(prev.get("score") or 0) > score and (now - float(prev.get("ts") or 0)) < 900:
            continue
        by_key[key] = {
            "ticker": ticker,
            "engine": eng,
            "broker": str(broker or c.get("broker") or ""),
            "score": score,
            "reason": str(c.get("reason") or c.get("action") or c.get("rec") or "")[:120],
            "asset_type": str(c.get("asset_type") or c.get("type") or ""),
            "is_crypto": bool(c.get("is_crypto"))
            or "crypto" in str(c.get("asset_type") or c.get("type") or "").lower(),
            "regime_caution": bool(c.get("regime_caution")),
            "ts": now,
        }

    out = sorted(by_key.values(), key=lambda x: float(x.get("score") or 0), reverse=True)
    out = out[: max(1, int(max_keep))]
    save_radar(out)
    return out


def top_radar(n: int = 8) -> list[dict]:
    now = _now()
    items = [x for x in load_radar() if (now - float(x.get("ts") or 0)) < _MAX_AGE_SEC]
    items.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    return items[: max(0, int(n))]


def latest_signal_alert(items: list[dict] | None = None) -> dict[str, Any] | None:
    """
    Compact alert payload for companion edge-trigger.
    id changes when top actionable name/score changes.
    """
    top = (items if items is not None else top_radar(1))
    if not top:
        return None
    hit = top[0]
    ticker = str(hit.get("ticker") or "")
    eng = str(hit.get("engine") or "")
    try:
        score = float(hit.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    if not ticker or score < 40:
        return None
    sid = f"{eng}:{ticker}:{int(score)}"
    return {
        "id": sid,
        "ticker": ticker,
        "engine": eng,
        "score": round(score, 1),
        "broker": str(hit.get("broker") or ""),
        "reason": str(hit.get("reason") or "")[:80],
        "ts": float(hit.get("ts") or _now()),
    }


def merge_breakout_universe(
    finviz: list[str] | None = None,
    rh_movers: list[str] | None = None,
    yahoo_gainers: list[str] | None = None,
    *,
    extended_micro: list[str] | None = None,
    max_total: int = 16,
) -> list[dict]:
    """
    Multi-source Breakouts list with source tags (deduped, first source wins).
  extended_micro: afford-filtered tickers for extended-hours micro books (1.40).
    """
    seen: set[str] = set()
    out: list[dict] = []

    def _add(raw: str, tag: str) -> None:
        if len(out) >= max_total:
            return
        sym = str(raw or "").upper().strip()
        if not sym or not sym.isalpha() or not (1 <= len(sym) <= 5):
            return
        if sym.startswith("AA") and len(sym) == 5:
            return
        if sym in seen:
            return
        seen.add(sym)
        out.append({"symbol": sym, "type": tag})

    for s in finviz or []:
        _add(s, "Finviz Breakout")
    for s in rh_movers or []:
        _add(s, "RH Top Mover")
    for s in yahoo_gainers or []:
        _add(s, "Yahoo Gainer")
    for s in extended_micro or []:
        _add(s, "Extended Micro")
    return out
