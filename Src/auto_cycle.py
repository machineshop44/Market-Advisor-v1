"""Autotrader cycle helpers — rank trail, scan unpack, idle/coach notes.

Pure functions kept out of gui.py so ops/unit tests can lock trail wording
without importing Qt. Trading behavior stays in gui; this module only formats,
filters, and throttles bookkeeping.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional


def count_buy_signals(results) -> int:
    """Count BUY actions in a scan score result list (excludes DO NOT BUY)."""
    return sum(
        1
        for row in (results or [])
        if len(row) >= 3
        and "BUY" in str(row[2]).upper()
        and "DO NOT BUY" not in str(row[2]).upper()
    )


def unpack_scan_payload(payload) -> tuple[list, list, list, list]:
    """Normalize (opps, results[, buy_candidates[, dropped]]) from bg scan jobs."""
    if not payload:
        return [], [], [], []
    if isinstance(payload, (list, tuple)):
        if len(payload) >= 4:
            return payload[0] or [], payload[1] or [], payload[2] or [], payload[3] or []
        if len(payload) >= 3:
            return payload[0] or [], payload[1] or [], payload[2] or [], []
        if len(payload) == 2:
            return payload[0] or [], payload[1] or [], [], []
    return [], [], [], []


def filter_actionable_ranked(ranked, *, floor: float = -500.0) -> list:
    """Drop names that cannot improve the book (score <= floor after rank)."""
    return [c for c in (ranked or []) if float(c.get("score") or 0.0) > float(floor)]


def format_top_ranked(candidates, *, top_n: int = 3) -> str:
    """e.g. 'ETH(70*SI), BTC(65)' for activity-log rank trails."""
    parts = []
    for c in list(candidates or [])[: max(1, int(top_n))]:
        ticker = c.get("ticker") or "?"
        score = float(c.get("score") or 0.0)
        si = "*SI" if c.get("scale_in") else ""
        parts.append(f"{ticker}({score:.0f}{si})")
    return ", ".join(parts)


def format_ranked_for_book_note(
    broker_name: str,
    actionable: list,
    ranked: list,
    *,
    top_n: int = 3,
) -> str:
    """Ranked N/M buys for book — top: … (matches historical gui trail)."""
    top_src = actionable or ranked or []
    top = format_top_ranked(top_src, top_n=top_n)
    return (
        f"[{broker_name}] Ranked {len(actionable)}/{len(ranked)} buys for book — top: {top}"
    )


def format_ranked_buys_note(
    broker_name: str,
    buy_candidates: list,
    *,
    top_n: int = 3,
) -> str:
    """Simpler trail used after CRYPTO/BREAKOUT/CORE score when candidates exist."""
    top = format_top_ranked(buy_candidates, top_n=top_n)
    return f"[{broker_name}] Ranked {len(buy_candidates or [])} buys — top: {top}"


def empty_after_rank_filter_note(broker_name: str, ranked_n: int) -> str:
    """When rank produced candidates but none passed actionable filter."""
    return (
        f"[{broker_name}] No buys executed after rank "
        f"(0/{ranked_n} actionable — held/scale-in/cluster filtered)"
    )


def should_append_empty_after_rank_filter(notes, ranked) -> bool:
    """True when actionable emptied and notes lack a scale-in / skip outcome already."""
    if not ranked:
        return False
    return not any(
        ("SCALE-IN skipped" in str(n) or "Skipped [" in str(n)) for n in (notes or [])
    )


def throttle_scan_drops(
    store: dict,
    broker,
    engine,
    dropped,
    *,
    now: float,
    cooldown_sec: float = 780,
) -> tuple[list[str], int]:
    """
    Once-per-ticker drop lines. Returns (visible_lines, suppressed_count).
    Mutates store in place. Skips still apply — this only gates Activity Log noise.
    """
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    visible: list[str] = []
    suppressed = 0
    for line in dropped or []:
        text = str(line or "").strip()
        if not text:
            continue
        ticker = text.split("(", 1)[0].strip().upper() or text[:24].upper()
        key = (str(broker), str(engine), ticker)
        prev = float(store.get(key) or 0.0)
        if now - prev < float(cooldown_sec):
            suppressed += 1
            continue
        store[key] = now
        visible.append(text)
    return visible, suppressed


def coach_drop_bucket(line: str) -> str:
    """Classify a scan-drop line into a coach tip bucket."""
    d = str(line or "").lower()
    if "missing cost" in d:
        return "missing_cost"
    if "hard stop" in d:
        return "hard_stop"
    if "roi band" in d or "add roi" in d:
        return "roi_band"
    if "drawdown too deep" in d:
        return "drawdown"
    if "scale-in blocked" in d or "scale-in" in d:
        return "scale_in"
    if "cluster" in d or "already held" in d:
        return "held_cluster"
    return "other"


def dominant_coach_drop_bucket(dropped: Iterable) -> str:
    buckets = [coach_drop_bucket(line) for line in (dropped or [])]
    if not buckets:
        return "other"
    return Counter(buckets).most_common(1)[0][0]


def coach_tip_for_scan_drops(broker, engine, dropped) -> tuple[str, str]:
    """
    Match [COACH] wording to the dominant drop reason.
    Returns (throttle_key, tip_text).
    """
    dominant = dominant_coach_drop_bucket(dropped)
    tips = {
        "missing_cost": (
            f"{broker}/{engine}: scale-in blocked — cost basis unknown on held names. "
            f"Reconnect or wait until RH reports avg cost; TTP/ROI stay gated."
        ),
        "hard_stop": (
            f"{broker}/{engine}: held names past hard stop — scale-in correctly blocked. "
            f"Wait for recovery or a portfolio exit; not a cluster/swap issue."
        ),
        "roi_band": (
            f"{broker}/{engine}: held names outside the add ROI band — scale-in gated "
            f"until price re-enters the band (not a new-entry / swap issue)."
        ),
        "drawdown": (
            f"{broker}/{engine}: held names too deep in drawdown for scale-in. "
            f"Adds stay blocked until ROI recovers into the add window."
        ),
        "scale_in": (
            f"{broker}/{engine}: BUY signals are on held names that failed scale-in gates "
            f"(not fresh book slots). Check ROI band / support / cost basis."
        ),
        "held_cluster": (
            f"{broker}/{engine}: signals exist but none fit the book (held/cluster). "
            f"Opportunity-swap may help on Balanced/Aggressive when BP is tight."
        ),
        "other": (
            f"{broker}/{engine}: BUY signals exist but none were actionable for the book."
        ),
    }
    tip = tips.get(dominant) or tips["other"]
    key = f"{broker}:{engine}:no_actionable:{dominant}"
    return key, tip


def format_no_actionable_scan_note(
    broker: str,
    engine: str,
    raw_buys: int,
    *,
    visible: list[str] | None = None,
    suppressed: int = 0,
    fallback: str = "already held / cluster full",
) -> Optional[str]:
    """
    Activity line when BUY signals exist but 0 are actionable.
    Returns None when all drop lines are still muted (stay quiet).
    """
    visible = list(visible or [])
    if visible:
        detail = ", ".join(visible[:8])
        more = f" (+{len(visible) - 8} more)" if len(visible) > 8 else ""
        mute = f" ({suppressed} muted)" if suppressed else ""
        return (
            f"[{broker}] {engine}: {raw_buys} BUY signal(s) but 0 actionable for book "
            f"— no orders. Dropped: {detail}{more}{mute}"
        )
    if suppressed:
        return None
    return (
        f"[{broker}] {engine}: {raw_buys} BUY signal(s) but 0 actionable for book "
        f"— no orders. Dropped: {fallback}"
    )


def scale_in_skip_note(
    store: dict,
    broker_name: str,
    ticker: str,
    reason: str,
    *,
    now: float,
    throttle_sec: float = 780,
) -> Optional[str]:
    """
    Throttle identical SCALE-IN skip Activity notes.
    Mutates store; returns note to append or None if suppressed this cycle.
    Always journals separately (caller).
    """
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    reason = str(reason or "sizing blocked").strip()
    key = (str(broker_name or "").upper(), str(ticker or "").upper(), reason)
    prev = store.get(key)
    if prev is not None:
        elapsed = now - float(prev.get("t") or 0.0)
        if elapsed < float(throttle_sec):
            prev["n"] = int(prev.get("n") or 1) + 1
            return None
        n = int(prev.get("n") or 1)
        store[key] = {"t": now, "n": 1}
        suffix = f" (repeated {n}× over last {int(elapsed // 60) or 1}m)" if n > 1 else ""
        return f"[{broker_name}] SCALE-IN skipped [{ticker}]: {reason}{suffix}"
    store[key] = {"t": now, "n": 1}
    return f"[{broker_name}] SCALE-IN skipped [{ticker}]: {reason}"


def clear_scale_in_skip_throttle(store: dict, broker_name: str, ticker: str) -> None:
    """Clear throttle keys for a ticker after a successful scale-in."""
    if not isinstance(store, dict):
        return
    prefix = (str(broker_name or "").upper(), str(ticker or "").upper())
    for k in list(store.keys()):
        if isinstance(k, tuple) and len(k) >= 2 and k[:2] == prefix:
            store.pop(k, None)


# --- Buy / rotate / portfolio cycle helpers (next gui.py extract wave) ---

DEFAULT_CRYPTO_TICKERS = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI",
})


def throttled_buy_skip_note(
    store: dict,
    notes: list,
    broker_name: str,
    kind: str,
    message: str,
    *,
    now: float,
    cooldown_sec: float = 720,
) -> bool:
    """
    Append BP-too-low / rotate-capped notes once per broker+kind per cooldown.
    Mutates store and notes. Returns True when the note was appended.
    """
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = f"{broker_name}:{kind}"
    prev = float(store.get(key) or 0.0)
    if now - prev < float(cooldown_sec):
        return False
    store[key] = now
    notes.append(message)
    return True


def note_frac_buy_defer(
    store: dict,
    notes: list,
    broker_name: str,
    ticker: str,
    reason: str,
    session_label: str,
) -> bool:
    """Once per ticker/session: overnight/whole-share buy defer. Mutates store/notes."""
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = (str(broker_name), str(ticker).upper(), str(session_label or "?"))
    if store.get(key):
        return False
    store[key] = True
    notes.append(f"[{broker_name}] Deferring buy [{ticker}] — {reason}")
    return True


def note_deferred_sell(
    store: dict,
    notes: list,
    broker: str,
    ticker: str,
    reason: str,
    session_label: str,
) -> bool:
    """Log a deferred sell once per ticker/reason for this session label."""
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = (str(broker), str(ticker).upper(), str(reason)[:64])
    if store.get(key) == session_label:
        return False
    store[key] = session_label
    notes.append(f"[{broker}] Deferring [{ticker}] — {reason}")
    return True


def rh_equity_sell_defer_reason(
    ticker,
    shares_val,
    price,
    asset_type,
    session: dict,
    *,
    frac_ext_ineligible=None,
    known_cryptos: Optional[Iterable] = None,
) -> Optional[str]:
    """
    If this RH equity sell cannot succeed in the current session, return a short reason.
    Crypto always returns None (24/7). Pure policy — no Qt.
    """
    cryptos = set(known_cryptos) if known_cryptos is not None else set(DEFAULT_CRYPTO_TICKERS)
    is_crypto = (
        "crypto" in str(asset_type or "").lower()
        or str(ticker).upper() in cryptos
    )
    if is_crypto:
        return None
    if not (session or {}).get("equity_tradeable"):
        return "equity markets closed"
    try:
        shares = float(shares_val or 0)
    except (TypeError, ValueError):
        shares = 0.0
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        px = 0.0
    if 0 < shares < 1.0:
        if px > 0 and (shares * px) < 1.00:
            return "fractional notional under $1"
        if not (session or {}).get("fractional_ok"):
            return (
                "fractional equity sells blocked until ~7am ET / regular hours "
                "(after-hours fractionals end ~7:30pm ET)"
            )
        ineligible = frac_ext_ineligible or set()
        if (session or {}).get("label") != "REGULAR" and str(ticker).upper() in ineligible:
            return "ticker not eligible for extended-hours fractionals (waiting for regular open)"
    return None


def format_rotate_skip_note(broker_name: str, why: str) -> str:
    return f"[{broker_name}] [ROTATE] skipped — {why}"


def format_rotate_floor_clear_note(
    broker_name: str,
    candidate_ticker: str,
    *,
    bp: float,
    floor: float,
    label: str = "broker floor",
) -> str:
    """Andrew-visible trail when rotating to clear RH crypto / min ticket floor."""
    return (
        f"[{broker_name}] [ROTATE] freeing BP to clear {label} "
        f"(${float(bp):.2f} → ≥${float(floor):.2f}) for {candidate_ticker}…"
    )


def format_rotate_sell_note(
    broker_name: str,
    fund_ticker: str,
    candidate_ticker: str,
    *,
    roi: float = 0.0,
    fund_score: float = 0.0,
    candidate_score: float = 0.0,
    reason: str = "",
) -> str:
    return (
        f"[{broker_name}] [ROTATE] Sell {fund_ticker} "
        f"(roi {float(roi or 0) * 100:.2f}%, score {float(fund_score or 0):.0f}) "
        f"→ fund {candidate_ticker} "
        f"(score {float(candidate_score or 0):.0f}; {reason})"
    )


def format_rotate_sell_failed_note(broker_name: str, fund_ticker: str, status) -> str:
    return f"[{broker_name}] [ROTATE] Sell {fund_ticker} failed: {status}"


def format_rotate_freed_note(
    broker_name: str,
    fund_ticker: str,
    proceeds_txt: str,
    bp_txt: str,
) -> str:
    return (
        f"[{broker_name}] [ROTATE] Freed ~{proceeds_txt} from {fund_ticker}; "
        f"BP now ~{bp_txt}"
    )


def format_scale_in_ok_note(ticker: str, reason: str) -> str:
    return f"SCALE-IN considered [{ticker}]: OK — {reason}"


def holdings_fingerprint(holdings) -> str:
    """Stable holdings signature (broker/ticker/shares) for cycle change detection."""
    parts = []
    safe = [a for a in (holdings or []) if isinstance(a, dict)]
    for a in sorted(safe, key=lambda x: (str(x.get("broker", "")), str(x.get("ticker", "")))):
        parts.append(
            f"{a.get('broker', '')}:{a.get('ticker', '')}:{float(a.get('shares') or 0):.8f}"
        )
    return "|".join(parts)


def partition_portfolio_sells(
    sell_list,
    *,
    broker_name: str,
    session: dict,
    sell_fail_should_skip,
    rh_defer_reason_fn=None,
    note_deferred_fn=None,
) -> tuple[list, list, list]:
    """
    Split scored SELLs into actionable vs deferred.
    Callbacks keep broker/session policy injectable (no Qt).
    Returns (actionable, deferred_tickers, defer_notes).
    """
    actionable: list = []
    deferred: list = []
    notes_tmp: list = []
    for item in sell_list or []:
        row_b = str(item.get("broker") or broker_name)
        tick = item.get("ticker")
        if sell_fail_should_skip(row_b, tick):
            deferred.append(str(tick or "?").upper())
            continue
        if row_b == "Robinhood" and callable(rh_defer_reason_fn):
            defer = rh_defer_reason_fn(
                tick, item.get("shares"), item.get("price"),
                item.get("type"), session,
            )
            if defer:
                deferred.append(str(tick or "?").upper())
                if callable(note_deferred_fn):
                    note_deferred_fn(
                        "Robinhood", tick, defer,
                        (session or {}).get("label") or "UNKNOWN", notes_tmp,
                    )
                continue
        actionable.append(item)
    return actionable, deferred, notes_tmp


def format_portfolio_scored_note(
    broker: str,
    sell_n: int,
    *,
    actionable_n: int = 0,
    deferred: list | None = None,
    first_defer_this_session: bool = False,
) -> Optional[str]:
    """PORTFOLIO scored Activity trail. None = stay quiet (all still deferred)."""
    deferred = list(deferred or [])
    uniq = sorted(set(deferred))
    if uniq:
        if first_defer_this_session:
            return (
                f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s); "
                f"deferring {len(uniq)} until tradable: {', '.join(uniq)}"
            )
        if actionable_n > 0:
            return (
                f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s) "
                f"({actionable_n} actionable, {len(uniq)} still deferred)"
            )
        return None
    return f"[AUTO] [{broker}] PORTFOLIO scored — {sell_n} SELL signal(s)"


def format_cost_basis_display(cost, *, broker_name: str = "", unknown_label: str = "cost ?") -> str:
    """Portfolio Avg Cost cell: show cost ? when basis unknown (esp. Coinbase)."""
    try:
        c = float(cost or 0.0)
    except (TypeError, ValueError):
        c = 0.0
    if c > 0:
        return f"${c:,.2f}"
    if str(broker_name) == "Coinbase" or c <= 0:
        return unknown_label
    return f"${c:,.2f}"


def count_unknown_cost_holdings(assets, *, broker_name: str | None = None) -> int:
    """Count holdings with no usable avg cost (for Home CB honesty chip)."""
    n = 0
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        if broker_name and str(a.get("broker") or "") != broker_name:
            continue
        try:
            cost = float(a.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        if cost <= 0:
            n += 1
    return n


def etrade_home_env_chip(
    *,
    environment: str,
    live_trading: bool,
    buying_power: float,
    min_trade_dollars: float = 5.0,
) -> tuple[str, str, str]:
    """
    Home E*TRADE env chip: (label, tooltip, color_hex).
    Surfaces Stops N/A · TTP and live $0 BP honesty (not sandbox-only).
    """
    env = str(environment or "sandbox").lower()
    try:
        bp_f = float(buying_power or 0.0)
    except (TypeError, ValueError):
        bp_f = 0.0
    try:
        min_d = float(min_trade_dollars or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    low_bp = bp_f < max(0.01, min_d)

    if env == "sandbox":
        chip = "Sandbox / no BP" if low_bp else "Sandbox · stops N/A"
        tip = (
            "Sandbox environment — paper/sandbox path until live credentials and funded BP. "
            "Protective stops N/A on E*TRADE (software TTP only). "
            "Repair stops skips E*TRADE by design."
            + (" Sandbox often returns $0 BP — not a live funded account; buy engines parked." if low_bp else "")
        )
        return chip, tip, "#F9A825"

    if live_trading:
        chip = "Live · orders ON · stops N/A"
        tip = (
            "Live environment with live order placement enabled. "
            "Protective stops N/A on E*TRADE — software TTP only. Repair skips E*TRADE."
        )
        col = "#2E7D32"
    else:
        chip = "Live · orders OFF · stops N/A"
        tip = (
            "Live environment but live trading kill-switch is OFF (read-only). "
            "Enable in Settings after validation. Protective stops N/A (TTP only)."
        )
        col = "#EF6C00"

    if low_bp:
        chip = chip.replace(" · stops N/A", " · $0 BP · stops N/A")
        if "stops N/A" not in chip:
            chip = f"{chip} · $0 BP"
        tip += (
            " Buying power is ~$0 — buy engines parked; verify funding / account "
            "selection before arming (no fake live fills)."
        )
        col = "#F9A825"
    return chip, tip, col


def etrade_bp_label(bp: float, *, environment: str, min_trade_dollars: float = 5.0) -> tuple[str, str]:
    """Buying Power label + tooltip for Home ET row."""
    env = str(environment or "sandbox").lower()
    try:
        bp_f = float(bp or 0.0)
    except (TypeError, ValueError):
        bp_f = 0.0
    try:
        min_d = float(min_trade_dollars or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    low = bp_f < max(0.01, min_d)
    money = f"${bp_f:,.2f}"
    if env == "sandbox" and low:
        return (
            "Buying Power: $0 (sandbox stub)",
            "E*TRADE sandbox often returns $0 buying power even when connected. "
            "This is not a live funded account — do not arm expecting real BP.",
        )
    if env == "live" and low:
        return (
            f"Buying Power: {money}",
            "Live E*TRADE reports ~$0 buying power — verify funding, account picker, "
            "and that the selected account can trade. Stops remain N/A (TTP only).",
        )
    return (f"Buying Power: {money}", "")
