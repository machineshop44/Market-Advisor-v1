"""
Trader context — everything a human day trader checks before clicking Buy.

Single snapshot for: buy batch, Advisor AI, monitor API, MCP, Home UI, watchdog.
"""
from __future__ import annotations

import time
from typing import Any

from scoring import (
    crypto_regime_required,
    describe_posture_for_broker,
    entry_regime_ok,
    equity_regime_required,
    is_small_book,
    max_affordable_share_price,
    posture_knobs_for_broker,
    small_book_prefers_breakouts,
)


def _deployable_bp(buying_power: float, settings: dict | None) -> float:
    s = settings or {}
    try:
        util = float(s.get("target_bp_utilization_pct", 88.0))
        if util > 1.0:
            util = util / 100.0
    except (TypeError, ValueError):
        util = 0.88
    util = min(0.99, max(0.50, util))
    return max(0.0, float(buying_power or 0.0) * util)


def build_trader_context(
    broker_name: str,
    *,
    equity: float = 0.0,
    buying_power: float = 0.0,
    settings: dict | None = None,
    session_label: str = "",
    market_label: str = "",
    open_positions: int = 0,
    dd_paused: bool = False,
    dd_reason: str = "",
    dd_mins_left: int = 0,
    armed: bool = False,
    connected: bool = True,
    halted: bool = False,
    reauth_needed: bool = False,
    idle_reason: str = "",
    supports_crypto: bool = True,
    supports_equities: bool = True,
    advisor_gate: bool = False,
    regime_equity_ok: bool | None = None,
    regime_equity_reason: str = "",
    regime_crypto_ok: bool | None = None,
    regime_crypto_reason: str = "",
) -> dict[str, Any]:
    """Human-style desk snapshot for one broker."""
    s = settings or {}
    posture_info = describe_posture_for_broker(broker_name, s, equity=equity)
    posture = str(posture_info.get("effective") or "balanced")
    knobs = posture_knobs_for_broker(broker_name, s, equity=equity)
    max_open = int(knobs.get("max_open_positions") or 8)
    deploy = _deployable_bp(buying_power, s)
    min_ticket = float(s.get("min_trade_dollars", 5.0) or 5.0)
    max_share = max_affordable_share_price(buying_power, utilization=float(
        knobs.get("target_bp_utilization_pct") or s.get("target_bp_utilization_pct") or 88.0
    ))
    small = is_small_book(equity)
    breakouts_first = small_book_prefers_breakouts(equity, s)

    if regime_equity_ok is None and supports_equities:
        regime_equity_ok, regime_equity_reason = entry_regime_ok(
            is_crypto=False, posture=posture,
        )
    if regime_crypto_ok is None and supports_crypto:
        regime_crypto_ok, regime_crypto_reason = entry_regime_ok(
            is_crypto=True, posture=posture,
        )

    engines = {
        "crypto": {
            "enabled": supports_crypto,
            "recommended": supports_crypto and (small or broker_name == "Coinbase"),
        },
        "breakouts": {
            "enabled": supports_equities,
            "recommended": supports_equities and breakouts_first,
        },
        "core": {
            "enabled": supports_equities,
            "recommended": supports_equities and not breakouts_first,
            "parked": breakouts_first,
        },
    }

    blockers: list[dict[str, str]] = []
    if halted:
        blockers.append({"code": "halt", "message": "Panic halt — all brokers disarmed"})
    if not connected:
        blockers.append({"code": "offline", "message": f"{broker_name} disconnected"})
    if reauth_needed:
        blockers.append({"code": "reauth", "message": f"{broker_name} needs re-authentication"})
    if dd_paused:
        msg = dd_reason or "drawdown pause"
        if dd_mins_left > 0:
            msg += f" ({dd_mins_left}m left)"
        blockers.append({"code": "dd_pause", "message": msg})
    if idle_reason:
        blockers.append({"code": "idle", "message": idle_reason})
    if deploy + 1e-9 < min_ticket:
        if int(open_positions or 0) > 0:
            blockers.append({
                "code": "fully_deployed",
                "message": (
                    f"Fully deployed — ${deploy:.2f} left under min ticket "
                    f"${min_ticket:.0f} (waiting on exits or a deposit)"
                ),
            })
        else:
            blockers.append({
                "code": "low_bp",
                "message": f"Deployable ${deploy:.2f} below min ticket ${min_ticket:.2f}",
            })
    if supports_equities and regime_equity_ok is False and regime_equity_reason:
        blockers.append({
            "code": "regime_equity",
            "message": regime_equity_reason.replace("DO NOT BUY (", "").rstrip(")"),
        })
    if supports_crypto and regime_crypto_ok is False and regime_crypto_reason:
        blockers.append({
            "code": "regime_crypto",
            "message": regime_crypto_reason.replace("DO NOT BUY (", "").rstrip(")"),
        })
    if open_positions >= max_open > 0:
        blockers.append({
            "code": "max_positions",
            "message": f"At max open positions ({open_positions}/{max_open})",
        })

    if not armed:
        blockers.append({
            "code": "disarmed",
            "message": f"{broker_name} auto-trader disarmed",
        })

    regime_blocks_entry = bool(
        (supports_equities and regime_equity_ok is False and equity_regime_required(posture))
        or (supports_crypto and regime_crypto_ok is False and crypto_regime_required(posture))
    )
    hard_block = any(
        b["code"] in ("halt", "offline", "reauth", "dd_pause", "low_bp", "fully_deployed")
        for b in blockers
    )
    can_buy = (
        connected
        and not halted
        and not reauth_needed
        and not dd_paused
        and not hard_block
    )
    auto_ready = bool(can_buy and armed and not regime_blocks_entry)

    afford_hint = (
        f"≤{max_share:.0f}/share whole"
        if max_share >= 1
        else f"need ≥${min_ticket:.0f} deployable"
    )
    engine_hint = "Breakouts + Crypto" if breakouts_first else "Breakouts + Core + Crypto"
    if breakouts_first and supports_equities:
        engine_hint += " (CORE parked — small book)"

    summary_parts = [
        f"{broker_name}: ${buying_power:.0f} BP · deploy ~${deploy:.0f}",
        f"afford {afford_hint}",
        f"posture {posture_info.get('label') or posture}",
    ]
    if session_label:
        summary_parts.append(f"session {session_label}")
    if blockers:
        summary_parts.append("block: " + blockers[0]["message"][:80])
    else:
        summary_parts.append(f"engines: {engine_hint}")
    if advisor_gate:
        summary_parts.append("advisor approve ON")

    return {
        "broker": broker_name,
        "at": time.time(),
        "equity": round(float(equity or 0), 2),
        "buying_power": round(float(buying_power or 0), 2),
        "deployable_bp": round(deploy, 2),
        "min_ticket": round(min_ticket, 2),
        "max_affordable_share_price": round(max_share, 2),
        "small_book": small,
        "posture": posture,
        "posture_label": posture_info.get("label") or posture,
        "posture_manual": posture_info.get("manual") or posture,
        "posture_auto_scaled": bool(posture_info.get("auto_scaled")),
        "equity_tier": posture_info.get("equity_tier") or "",
        "session": session_label,
        "market": market_label,
        "open_positions": int(open_positions),
        "max_open_positions": max_open,
        "armed": bool(armed),
        "connected": bool(connected),
        "halted": bool(halted),
        "advisor_gate": bool(advisor_gate),
        "engines": engines,
        "regime": {
            "equity_ok": regime_equity_ok,
            "equity_reason": regime_equity_reason or "",
            "crypto_ok": regime_crypto_ok,
            "crypto_reason": regime_crypto_reason or "",
        },
        "dd_paused": bool(dd_paused),
        "dd_reason": dd_reason or "",
        "dd_mins_left": int(dd_mins_left or 0),
        "blockers": blockers,
        "can_place_new_buy": bool(can_buy),
        "regime_blocks_entry": bool(regime_blocks_entry),
        "auto_ready": auto_ready,
        "summary": " · ".join(summary_parts)[:500],
    }


def _regime_reason_short(reason: str) -> str:
    text = str(reason or "").replace("DO NOT BUY (", "").rstrip(")").strip()
    if "disagree" in text.lower():
        return "sources disagree"
    if "downtrend" in text.lower():
        return "1H downtrend"
    if "unavailable" in text.lower():
        return "data unavailable"
    return text[:48] if text else "blocked"


def format_regime_chip(ctx: dict | None) -> tuple[str, str, str]:
    """
    Desk-wide regime strip for Home: (label, tooltip, css_color).
    Growth/aggressive may skip SPY — show skipped state when gate not required.
    """
    ctx = ctx or {}
    regime = ctx.get("regime") or {}
    posture = str(ctx.get("posture") or "balanced")
    eq_req = equity_regime_required(posture)
    cr_req = crypto_regime_required(posture)
    eq_ok = regime.get("equity_ok")
    cr_ok = regime.get("crypto_ok")
    parts = []
    tips = []
    blocked = False
    if eq_req:
        if eq_ok is True:
            parts.append("SPY ok")
        elif eq_ok is False:
            blocked = True
            why = _regime_reason_short(regime.get("equity_reason") or "")
            parts.append(f"SPY {why}")
            tips.append(f"Equity regime: {why}")
        else:
            parts.append("SPY …")
    else:
        parts.append("SPY skipped")
        tips.append(f"Posture {posture}: SPY gate off for equities")
    if cr_req:
        if cr_ok is True:
            parts.append("BTC ok")
        elif cr_ok is False:
            blocked = True
            why = _regime_reason_short(regime.get("crypto_reason") or "")
            parts.append(f"BTC {why}")
            tips.append(f"Crypto regime: {why}")
        else:
            parts.append("BTC …")
    elif ctx.get("broker") == "Coinbase" or cr_ok is not None:
        parts.append("BTC turbulence only")
    label = "Regime: " + " · ".join(parts)
    tip = " · ".join(tips) if tips else label
    if blocked:
        tip += " · Advisor can propose past regime when you approve"
    color = "#C62828" if blocked else ("#2E7D32" if parts else "#555")
    return label, tip, color


def build_from_monitor_status(status: dict | None, broker_name: str) -> dict[str, Any]:
    """Rebuild trader context from /api/status snapshot (remote MCP)."""
    st = status or {}
    bal = (st.get("balances") or {}).get(broker_name) or {}
    bro = (st.get("brokers") or {}).get(broker_name) or {}
    heat = st.get("portfolio_heat") or {}
    combined = heat.get("combined") or {}
    holdings = (st.get("holdings_count") or {}).get(broker_name, 0)
    session = str(st.get("market") or "")
    return build_trader_context(
        broker_name,
        equity=float(bal.get("equity") or 0),
        buying_power=float(bal.get("cash") or 0),
        settings={},
        session_label=session,
        market_label=session,
        open_positions=int(holdings or 0),
        dd_paused=bool(bro.get("dd_pause") or combined.get("dd_paused")),
        dd_reason=str(bro.get("dd_reason") or combined.get("dd_reason") or ""),
        dd_mins_left=int(combined.get("dd_mins_left") or 0),
        armed=bool(bro.get("armed")),
        connected=bool(bro.get("connected", True)),
        halted=bool(st.get("halted")),
        reauth_needed=bool(bro.get("reauth_needed")),
        supports_crypto=bool(
            bro.get("supports_crypto")
            if "supports_crypto" in bro
            else broker_name in ("Coinbase", "Robinhood")
        ),
        supports_equities=bool(
            bro.get("supports_equities")
            if "supports_equities" in bro
            else broker_name != "Coinbase"
        ),
        advisor_gate=bool((st.get("advisor") or {}).get("count", 0) >= 0),
    )


def fully_deployed_idle_reason(
    *,
    buying_power: float,
    open_positions: int,
    min_ticket: float = 5.0,
    utilization: float = 0.88,
) -> str | None:
    """
    Cache-friendly fully-deployed message for buy-engine parking.
    Callers must pass cached BP / position counts — never live broker APIs.
    """
    try:
        bp = float(buying_power or 0.0)
    except (TypeError, ValueError):
        bp = 0.0
    try:
        util = float(utilization or 0.88)
        if util > 1.0:
            util = util / 100.0
    except (TypeError, ValueError):
        util = 0.88
    util = min(0.99, max(0.50, util))
    try:
        min_d = float(min_ticket or 5.0)
    except (TypeError, ValueError):
        min_d = 5.0
    deploy = max(0.0, bp * util)
    if deploy + 1e-9 >= min_d:
        return None
    if int(open_positions or 0) <= 0:
        return None
    return (
        f"Fully deployed — ${deploy:.2f} left under min ticket ${min_d:.0f} "
        f"(waiting on exits or a deposit)"
    )


def format_trader_digest(by_broker: dict[str, dict], *, day_pnl: float | None = None) -> str:
    lines = ["Trader desk context:"]
    if day_pnl is not None:
        try:
            d = float(day_pnl)
            lines.append(f"  Day P&L {d:+.2f}")
        except (TypeError, ValueError):
            pass
    for name, ctx in (by_broker or {}).items():
        if not isinstance(ctx, dict):
            continue
        if any(b.get("code") == "fully_deployed" for b in (ctx.get("blockers") or [])):
            flag = "DEPLOYED"
        elif ctx.get("regime_blocks_entry"):
            flag = "REGIME"
        elif ctx.get("auto_ready"):
            flag = "CAN BUY"
        elif ctx.get("can_place_new_buy"):
            flag = "PAUSED"
        else:
            flag = "BLOCKED"
        lines.append(f"  [{flag}] {ctx.get('summary') or name}")
    return "\n".join(lines)
