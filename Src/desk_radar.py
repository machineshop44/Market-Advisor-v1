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


def _board_symbol(raw) -> str | None:
    """US ticker the breakout board will accept. Same shape rules as the merger."""
    sym = str(raw or "").upper().strip()
    if not sym or not sym.isalpha() or not (1 <= len(sym) <= 5):
        return None
    if sym.startswith("AA") and len(sym) == 5:
        return None
    return sym


def parse_yahoo_screener_quotes(payload) -> list[dict]:
    """Yahoo predefined-screener JSON → [{symbol, price}]. Equities only."""
    out: list[dict] = []
    seen: set[str] = set()
    try:
        screens = ((payload or {}).get("finance") or {}).get("result") or []
    except Exception:
        return out
    if isinstance(screens, dict):
        screens = [screens]
    for screen in screens or []:
        if not isinstance(screen, dict):
            continue
        for quote in screen.get("quotes") or []:
            if not isinstance(quote, dict):
                continue
            kind = str(quote.get("quoteType") or "EQUITY").upper()
            if kind not in ("EQUITY", "ETF"):
                continue
            sym = _board_symbol(quote.get("symbol"))
            if not sym or sym in seen:
                continue
            try:
                px = float(
                    quote.get("regularMarketPrice")
                    or quote.get("regularMarketPreviousClose")
                    or 0
                )
            except (TypeError, ValueError):
                px = 0.0
            seen.add(sym)
            out.append({"symbol": sym, "price": px})
    return out


def _tape_tag(source: str) -> str:
    text = str(source or "").lower()
    if "news" in text:
        return "News Mover"
    if "finviz" in text:
        return "Finviz Mover"
    if "extended" in text:
        return "Extended Mover"
    if "rh" in text or "robin" in text:
        return "RH Top Mover"
    return "Tape Mover"


def assemble_market_board(
    rows: list | None = None,
    *,
    news: list[str] | None = None,
    extended_micro: list[str] | None = None,
    max_share: float = 0.0,
    max_total: int = 30,
    max_above: int = 8,
    max_news: int = 8,
    ceiling_mult: float = 1.15,
) -> list[dict]:
    """
    Live-tape board. Any US name a scanner returned can appear — the list is
    whoever is active, up, or down, not a stored watchlist.

    Names one whole share fits fill the board. A short slice is kept for names
    above that price so the tape is still visible. Buys are decided later.
    """
    try:
        ceiling = float(max_share or 0.0) * float(ceiling_mult or 1.15)
    except (TypeError, ValueError):
        ceiling = 0.0
    if ceiling < 0:
        ceiling = 0.0
    try:
        cap = max(1, int(max_total))
    except (TypeError, ValueError):
        cap = 30
    try:
        above_cap = max(0, int(max_above))
    except (TypeError, ValueError):
        above_cap = 8
    if ceiling <= 0:
        above_cap = cap
    try:
        news_cap = max(0, int(max_news))
    except (TypeError, ValueError):
        news_cap = 8

    price_of: dict[str, float] = {}

    def _remember(sym: str, price) -> None:
        try:
            px = float(price or 0.0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0 and price_of.get(sym, 0.0) <= 0:
            price_of[sym] = px

    def _above(sym: str) -> bool:
        if ceiling <= 0:
            return False
        px = price_of.get(sym) or 0.0
        return px > ceiling

    sequenced: list[tuple[str, str]] = []
    seen_rows: set[str] = set()
    for row in rows or []:
        if isinstance(row, str):
            sym = _board_symbol(row)
            price = 0.0
            source = ""
        elif isinstance(row, dict):
            sym = _board_symbol(row.get("symbol"))
            price = row.get("price")
            source = str(row.get("source") or "")
        else:
            continue
        if not sym or sym in seen_rows:
            continue
        _remember(sym, price)
        seen_rows.add(sym)
        sequenced.append((sym, _tape_tag(source)))

    news_rows: list[tuple[str, str]] = []
    seen_news: set[str] = set()
    for raw in news or []:
        sym = _board_symbol(raw)
        if not sym or sym in seen_news:
            continue
        seen_news.add(sym)
        news_rows.append((sym, "News Mover"))
        if len(news_rows) >= news_cap:
            break

    news_set = {sym for sym, _tag in news_rows}
    main: list[tuple[str, str]] = []
    above: list[tuple[str, str]] = []
    for sym, tag in sequenced:
        if sym in news_set:
            continue
        if _above(sym):
            above.append((sym, tag))
        else:
            main.append((sym, tag))

    out: list[dict] = []
    seen: set[str] = set()
    above_n = 0

    def _push(sym: str, tag: str) -> None:
        nonlocal above_n
        if sym in seen or len(out) >= cap:
            return
        if _above(sym):
            if above_n >= above_cap:
                return
            above_n += 1
        seen.add(sym)
        out.append({"symbol": sym, "type": tag})

    for sym, tag in news_rows:
        _push(sym, tag)
    for sym, tag in above:
        _push(sym, tag)
    for sym, tag in main:
        _push(sym, tag)
    for raw in extended_micro or []:
        sym = _board_symbol(raw)
        if sym:
            _push(sym, "Extended Mover")
    return out


def merge_breakout_universe(
    finviz: list[str] | None = None,
    rh_movers: list[str] | None = None,
    yahoo_gainers: list[str] | None = None,
    *,
    extended_micro: list[str] | None = None,
    news: list[str] | None = None,
    max_news: int = 5,
    max_total: int = 16,
) -> list[dict]:
    """
    Multi-source Breakouts list with source tags (deduped, first source wins).
  extended_micro: afford-filtered tickers for extended-hours micro books (1.40).
  news: News Radar names (1.42.59) — capped and placed after Finviz so RH/Yahoo
  movers cannot crowd them out of max_total.
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
    n_before = len(out)
    for s in news or []:
        if len(out) - n_before >= max(0, int(max_news)):
            break
        _add(s, "News Radar")
    for s in rh_movers or []:
        _add(s, "RH Top Mover")
    for s in yahoo_gainers or []:
        _add(s, "Yahoo Gainer")
    for s in extended_micro or []:
        _add(s, "Extended Micro")
    return out
