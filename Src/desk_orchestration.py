"""
Desk orchestration — focus broker, stuck capital, profit command center.

Sizing is never decided here. All ticket math stays in scoring.risk_sizing_breakdown
via gui._compute_trade_dollars / auto_cycle.capital_planner_snapshot.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

BROKER_ORDER = ("E*TRADE", "Robinhood", "Coinbase")

# Liquid names often whole-share affordable on a ~$100 micro ET book in extended hours.
EXTENDED_MICRO_BREAKOUTS: tuple[str, ...] = (
    "F", "SNAP", "SOFI", "PLTR", "RIVN", "LCID", "GRAB", "PLUG", "SOUN",
    "ACHR", "JOBY", "SNDL", "NIO", "BITF", "CLSK", "MARA", "RIOT", "HOOD",
    "AMD", "INTC", "BAC", "AAL", "DAL", "UAL", "T", "VZ", "WBD", "KVUE",
)


def focus_mode(settings: dict | None) -> str:
    """auto | off | manual"""
    mode = str((settings or {}).get("desk_focus_mode") or "auto").strip().lower()
    return mode if mode in ("auto", "off", "manual") else "auto"


def micro_broker_buy_parked(
    broker_name: str,
    deployable_bp: float,
    settings: dict | None,
) -> tuple[bool, str]:
    """
    Optional Coinbase buy park when cash cannot fund a min ticket.

    Default OFF — crypto is dollar-notional; cheap coins (SHIB etc.) can still
    deploy leftover BP via autosizing. When enabled, floor follows
    capital_min_deployable_buy (else min_trade_dollars). Legacy key
    capital_park_micro_floor is still accepted as an alias. Sells still run.
    """
    if not bool((settings or {}).get("capital_park_micro_crypto", False)):
        return False, ""
    if str(broker_name or "") != "Coinbase":
        return False, ""
    try:
        s = settings or {}
        # Canonical UI key first; legacy alias second
        override = s.get("capital_min_deployable_buy")
        if override is None:
            override = s.get("capital_park_micro_floor")
        if override is None:
            floor = float(s.get("min_trade_dollars") or 5.0)
        else:
            floor = float(override)
    except (TypeError, ValueError):
        floor = 5.0
    floor = max(1.0, floor)
    try:
        deploy = float(deployable_bp or 0.0)
    except (TypeError, ValueError):
        deploy = 0.0
    if deploy + 1e-9 < floor:
        return True, (
            f"Micro crypto park — deployable ${deploy:.0f} < ${floor:.0f} "
            f"(consolidate to primary; sells still run)"
        )
    return False, ""


def capital_efficiency_grade(
    by_broker: dict[str, dict],
    *,
    focus_broker: str | None = None,
    settings: dict | None = None,
    min_fragment: float = 8.0,
) -> dict[str, Any]:
    """fragmented | consolidating | single_stack."""
    preview = consolidated_deployable_preview(
        by_broker, focus_broker=focus_broker, settings=settings,
    )
    focus = preview.get("focus")
    fragmented = list(preview.get("fragmented") or [])
    try:
        extra = float(preview.get("extra") or 0.0)
        focus_deploy = float(preview.get("focus_deploy") or 0.0)
    except (TypeError, ValueError):
        extra, focus_deploy = 0.0, 0.0
    if not focus:
        grade = "fragmented"
    elif not fragmented or extra < min_fragment:
        grade = "single_stack"
    elif focus_deploy >= 40 and extra >= min_fragment:
        grade = "consolidating"
    else:
        grade = "fragmented"
    return {
        "grade": grade,
        "focus": focus,
        "focus_deploy": focus_deploy,
        "extra": extra,
        "combined": float(preview.get("combined") or 0.0),
        "fragmented": fragmented,
    }


def format_capital_efficiency_line(
    grade_info: dict[str, Any],
    *,
    money_fmt=None,
) -> str:
    def _m(x):
        if callable(money_fmt):
            return money_fmt(x)
        return f"${float(x or 0):,.0f}"

    g = str(grade_info.get("grade") or "")
    focus = grade_info.get("focus") or "—"
    if g == "single_stack":
        return f"Capital: single stack · {focus} {_m(grade_info.get('focus_deploy'))}"
    if g == "consolidating":
        return (
            f"Capital: consolidating · move {_m(grade_info.get('extra'))} → {focus} "
            f"for ~{_m(grade_info.get('combined'))}"
        )
    return (
        f"Capital: fragmented · primary {focus} {_m(grade_info.get('focus_deploy'))} "
        f"(+{_m(grade_info.get('extra'))} elsewhere)"
    )


def focus_scan_multiplier(
    broker_name: str,
    focus_broker: str | None,
    settings: dict | None,
    *,
    morning_boost: bool = False,
) -> float:
    """2× cadence (half interval) for focus broker; optional RTH morning 3×."""
    if not focus_broker or focus_mode(settings) == "off":
        return 1.0
    if str(broker_name or "") != str(focus_broker):
        return 1.0
    try:
        mult = float((settings or {}).get("focus_broker_scan_mult") or 2.0)
    except (TypeError, ValueError):
        mult = 2.0
    if morning_boost:
        try:
            morning = float((settings or {}).get("focus_morning_scan_mult") or 3.0)
        except (TypeError, ValueError):
            morning = 3.0
        mult = max(mult, morning)
    return max(1.0, min(4.0, mult))


def interval_scale_for_focus(
    base_sec: float,
    broker_name: str,
    focus_broker: str | None,
    settings: dict | None,
    *,
    morning_boost: bool = False,
) -> float:
    mult = focus_scan_multiplier(
        broker_name, focus_broker, settings, morning_boost=morning_boost,
    )
    if mult <= 1.0:
        return float(base_sec)
    return max(15.0, float(base_sec) / mult)


def is_rth_morning_window(now_et=None) -> bool:
    """True during 9:30–11:00 America/New_York on a weekday."""
    try:
        if now_et is None:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:
            return False
        sod = now_et.hour * 3600 + now_et.minute * 60 + now_et.second
        return (9 * 3600 + 30 * 60) <= sod < (11 * 3600)
    except Exception:
        return False


def resolve_focus_broker(
    by_broker: dict[str, dict],
    settings: dict | None,
) -> str | None:
    """
    Pick the broker that should receive buy-engine priority.
    auto: highest deployable_bp among can_place_new_buy; preferred primary then ET > RH > CB.
    manual: desk_focus_broker when set and can buy.
    """
    mode = focus_mode(settings)
    if mode == "off":
        return None
    if mode == "manual":
        manual = str((settings or {}).get("desk_focus_broker") or "").strip()
        if manual and (by_broker.get(manual) or {}).get("can_place_new_buy"):
            return manual
        return None

    preferred = str((settings or {}).get("desk_preferred_primary") or "E*TRADE").strip()
    candidates: list[tuple[float, str]] = []
    for name, ctx in (by_broker or {}).items():
        if not isinstance(ctx, dict):
            continue
        if not ctx.get("can_place_new_buy"):
            continue
        try:
            deploy = float(ctx.get("deployable_bp") or ctx.get("buying_power") or 0.0)
        except (TypeError, ValueError):
            deploy = 0.0
        parked, _ = micro_broker_buy_parked(name, deploy, settings)
        if parked:
            continue
        candidates.append((deploy, str(name)))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]

    def sort_key(item: tuple[float, str]) -> tuple:
        deploy, name = item
        pref_rank = 0 if name == preferred else 1
        try:
            rank = BROKER_ORDER.index(name)
        except ValueError:
            rank = 99
        return (pref_rank, -deploy, rank)

    candidates.sort(key=sort_key)
    return candidates[0][1]


def focus_parks_buys(broker_name: str, focus_broker: str | None, settings: dict | None) -> bool:
    """
    True when non-focus broker buy engines should rest.

    Default OFF: focus only speeds scans on the primary (interval multiplier).
    Exclusive parking starved E*TRADE/Coinbase whenever Robinhood was focus.
    Set desk_focus_park_others True to rest non-focus buys again.
    """
    if not bool((settings or {}).get("desk_focus_park_others", False)):
        return False
    if focus_mode(settings) == "off" or not focus_broker:
        return False
    return str(broker_name or "") != str(focus_broker)


def deployable_open_count(holdings: list, *, broker_name: str = "", classify_locked=None) -> int:
    """Open positions excluding OTC/dust/no-quote bags."""
    if not holdings:
        return 0
    if classify_locked is None:
        try:
            import auto_cycle as ac
            classify_locked = ac.classify_locked_holding
        except Exception:
            classify_locked = lambda h, **_: (False, "")
    n = 0
    for h in holdings:
        if not isinstance(h, dict):
            continue
        try:
            shares = float(h.get("shares") or h.get("qty") or 0.0)
        except (TypeError, ValueError):
            shares = 0.0
        if shares <= 0:
            continue
        locked, _ = classify_locked(h, broker_name=broker_name or str(h.get("broker") or ""))
        if locked:
            continue
        n += 1
    return n


def next_desk_action(
    by_broker: dict[str, dict],
    *,
    focus_broker: str | None = None,
    settings: dict | None = None,
) -> str:
    """One-line actionable next step for Home command center."""
    if not by_broker:
        return "Arm auto-trader and refresh balances."
    focus = focus_broker or resolve_focus_broker(by_broker, settings or {"desk_focus_mode": "auto"})
    parts: list[str] = []
    for name, ctx in by_broker.items():
        if not isinstance(ctx, dict):
            continue
        blockers = ctx.get("blockers") or []
        code = str((blockers[0] or {}).get("code") or "") if blockers else ""
        if ctx.get("can_place_new_buy"):
            parts.append(f"Scan {name} breakouts/crypto (autosized to BP)")
            continue
        if code == "fully_deployed":
            parts.append(f"Wait {name} exit or partial TTP to free cash")
        elif code == "dd_pause":
            parts.append(f"{name} DD pause — sells only until recovery")
        elif code == "low_bp":
            parts.append(f"{name} low BP — deposit or exit to fund")
        elif blockers:
            parts.append(f"{name}: {(blockers[0] or {}).get('message') or code}")
    if focus and focus_parks_buys("Robinhood", focus, settings or {}):
        # only mention focus when exclusive park is on and multi-broker
        buyable = [n for n, c in by_broker.items() if c.get("can_place_new_buy")]
        if len(buyable) == 1 and buyable[0] == focus:
            return f"Focus {focus} — " + (parts[0] if parts else "awaiting scan signals")
    if parts:
        return " · ".join(parts[:3])
    return "Portfolio cycles running — no blockers."


def format_profit_command_center(
    summary: dict | None,
    by_broker: dict[str, dict],
    *,
    focus_broker: str | None = None,
    settings: dict | None = None,
    money_fmt=None,
    engine_line: str = "",
) -> str:
    def _m(x):
        if callable(money_fmt):
            return money_fmt(x)
        try:
            return f"${float(x or 0):,.2f}"
        except (TypeError, ValueError):
            return "$0.00"

    s = summary or {}
    net = float(s.get("net_after_fees") or 0.0)
    drag = float(s.get("fee_drag_pct") or 0.0)
    nwr = s.get("net_win_rate")
    closed = int(s.get("net_wins") or 0) + int(s.get("net_losses") or 0)
    wr = f"{float(nwr) * 100:.0f}%" if nwr is not None and closed > 0 else "—"
    action = next_desk_action(by_broker, focus_broker=focus_broker, settings=settings)
    focus_txt = f" · Focus {focus_broker}" if focus_broker else ""
    line = (
        f"7d net {_m(net)} · WR {wr} · fee {drag:.1f}%"
        f"{focus_txt} — Next: {action}"
    )
    if engine_line:
        line += f" | {engine_line}"
    return line


def format_consolidation_playbook(
    by_broker: dict[str, dict],
    *,
    settings: dict | None = None,
) -> str:
    """Human checklist when capital is fragmented across brokers."""
    preferred = str((settings or {}).get("desk_preferred_primary") or "E*TRADE").strip()
    lines = ["Consolidation playbook (manual — brokers don't auto-transfer):"]
    deployable = []
    stuck = []
    idle_cash = []
    for name, ctx in (by_broker or {}).items():
        if not isinstance(ctx, dict):
            continue
        try:
            bp = float(ctx.get("buying_power") or 0.0)
            deploy = float(ctx.get("deployable_bp") or bp)
        except (TypeError, ValueError):
            bp, deploy = 0.0, 0.0
        blockers = ctx.get("blockers") or []
        codes = {
            str((b or {}).get("code") or "")
            for b in blockers
            if isinstance(b, dict)
        }
        if "fully_deployed" in codes:
            stuck.append(name)
        if deploy >= 8.0 and name != preferred:
            idle_cash.append((name, deploy))
        if ctx.get("can_place_new_buy") and deploy >= 5.0:
            deployable.append((name, deploy))
    focus = None
    for n, d in sorted(deployable, key=lambda x: -x[1]):
        if n == preferred:
            focus = n
            break
    if focus is None and deployable:
        focus = max(deployable, key=lambda x: x[1])[0]

    if idle_cash and focus:
        bits = ", ".join(f"{n} (~${d:.0f})" for n, d in idle_cash)
        lines.append(
            f"• ACH idle cash {bits} → {focus} to lift capital grade "
            f"(one stack beats three micro tickets)."
        )
    if len(deployable) == 1 and stuck:
        target, amt = deployable[0]
        lines.append(
            f"• Primary stack: {target} (~{amt:.0f} deployable). "
            f"Exit positions on {', '.join(stuck)} then transfer cash to {target} "
            f"for one autosized ticket (sizing still follows live BP)."
        )
    elif len(deployable) > 1:
        names = ", ".join(f"{n} (${d:.0f})" for n, d in deployable)
        lines.append(
            f"• Multiple buyers: {names} — focus mode prefers {preferred}; "
            f"disarm secondaries or merge cash to cut fee drag."
        )
    elif not idle_cash:
        lines.append("• No broker can fund a new min ticket — exit or deposit before consolidating.")
    lines.append("• OTC/dust (*Q) bags: sell in broker app; excluded from sizing/deploy counts.")
    lines.append(
        "• Day P&L is mark-to-market vs morning baseline (not realized sells). "
        "Use Reset Day P&L if a buy/lag glitch fakes a loss."
    )
    return "\n".join(lines)


def filter_extended_micro_symbols(
    symbols: list[str],
    *,
    max_share_price: float,
    price_lookup=None,
) -> list[str]:
    """Keep names under afford ceiling for extended micro-book scans."""
    if max_share_price <= 0:
        return list(symbols)
    out: list[str] = []
    for sym in symbols:
        s = str(sym or "").upper().strip()
        if not s:
            continue
        px = 0.0
        if callable(price_lookup):
            try:
                px = float(price_lookup(s) or 0.0)
            except Exception:
                px = 0.0
        if px > 0 and px > max_share_price * 1.12:
            continue
        out.append(s)
    return out


def zero_signal_coach_message(
    *,
    broker: str,
    engine: str,
    session_label: str,
    max_afford_share: float,
    cycles: int,
) -> str:
    return (
        f"[{broker}] {cycles}× zero {engine} BUY — session {session_label}; "
        f"afford ≤{max_afford_share:.0f}/share whole. "
        f"Check extended micro list, fee gate, or regime."
    )


def stuck_capital_tier(
    held_hours: float,
    *,
    warn_hours: float = 4.0,
    urgent_hours: float = 12.0,
) -> str | None:
    if held_hours >= urgent_hours:
        return "urgent"
    if held_hours >= warn_hours:
        return "warn"
    return None


def profit_guard_tripped(
    summary: dict | None,
    settings: dict | None,
    *,
    now_ts: float | None = None,
) -> tuple[bool, str]:
    """
    Park new buys when 7d net is negative and fee drag is high.
    Sells / PORTFOLIO still run. Acknowledgment resets until profit_guard_ack_hours.
    """
    if not bool((settings or {}).get("profit_guard_enabled", True)):
        return False, ""
    ts = float(now_ts if now_ts is not None else time.time())
    try:
        ack_until = float((settings or {}).get("profit_guard_ack_until") or 0.0)
    except (TypeError, ValueError):
        ack_until = 0.0
    if ack_until > ts:
        return False, ""
    s = summary or {}
    closed = int(s.get("net_wins") or 0) + int(s.get("net_losses") or 0)
    min_closed = int((settings or {}).get("profit_guard_min_closed") or 3)
    if closed < max(1, min_closed):
        return False, ""
    try:
        net = float(s.get("net_after_fees") or 0.0)
        drag = float(s.get("fee_drag_pct") or 0.0)
        drag_thresh = float((settings or {}).get("profit_guard_fee_drag_pct") or 2.5)
    except (TypeError, ValueError):
        return False, ""
    if net < -0.05 and drag + 1e-9 >= drag_thresh:
        return True, (
            f"7d net ${net:,.2f} · fee drag {drag:.1f}% "
            f"(≥ {drag_thresh:.1f}% with {closed} closes)"
        )
    return False, ""


def consolidated_deployable_preview(
    by_broker: dict[str, dict],
    *,
    focus_broker: str | None = None,
    settings: dict | None = None,
) -> dict[str, Any]:
    """Virtual one-stack deployable BP if cash on other brokers were merged to focus."""
    focus = focus_broker or resolve_focus_broker(by_broker, settings or {"desk_focus_mode": "auto"})
    if not focus:
        return {"focus": None, "focus_deploy": 0.0, "combined": 0.0, "fragmented": []}
    try:
        focus_deploy = float(
            (by_broker.get(focus) or {}).get("deployable_bp")
            or (by_broker.get(focus) or {}).get("buying_power")
            or 0.0
        )
    except (TypeError, ValueError):
        focus_deploy = 0.0
    fragmented: list[tuple[str, float]] = []
    extra = 0.0
    for name, ctx in (by_broker or {}).items():
        if name == focus or not isinstance(ctx, dict):
            continue
        try:
            deploy = float(ctx.get("deployable_bp") or ctx.get("buying_power") or 0.0)
        except (TypeError, ValueError):
            deploy = 0.0
        if deploy >= 5.0:
            fragmented.append((str(name), deploy))
            extra += deploy
    combined = focus_deploy + extra
    return {
        "focus": focus,
        "focus_deploy": focus_deploy,
        "combined": combined,
        "fragmented": fragmented,
        "extra": extra,
    }


def format_single_stack_banner(
    preview: dict[str, Any],
    *,
    money_fmt=None,
    min_extra: float = 8.0,
) -> str | None:
    """Home banner when consolidating would materially raise deployable stack."""
    focus = preview.get("focus")
    if not focus:
        return None
    fragmented = list(preview.get("fragmented") or [])
    if not fragmented:
        return None
    try:
        extra = float(preview.get("extra") or 0.0)
        focus_deploy = float(preview.get("focus_deploy") or 0.0)
        combined = float(preview.get("combined") or 0.0)
    except (TypeError, ValueError):
        return None
    if extra < min_extra:
        return None

    def _m(x):
        if callable(money_fmt):
            return money_fmt(x)
        return f"${float(x or 0):,.0f}"

    parts = ", ".join(f"{n} {_m(d)}" for n, d in fragmented[:3])
    return (
        f"Single-stack tip: move {parts} → {focus} for ~{_m(combined)} deployable "
        f"(now {_m(focus_deploy)} on {focus}). Sizing still follows live BP."
    )


def session_boundary_tasks(
    kind: str,
    broker_name: str,
    *,
    focus_broker: str | None,
    focus_mode_on: bool,
) -> tuple[str, ...]:
    """
    Task order at RTH boundaries. Open burst prioritizes BREAKOUT (PENNY) on focus broker.
    """
    if kind == "pre_close":
        return ("PORTFOLIO",)
    if kind == "pre_open":
        if focus_mode_on and focus_broker and broker_name == focus_broker:
            return ("PORTFOLIO", "PENNY")
        return ("PORTFOLIO",)
    if kind == "open":
        if focus_mode_on and focus_broker:
            if broker_name == focus_broker:
                return ("PORTFOLIO", "PENNY", "CORE")
            return ("PORTFOLIO",)
        return ("PORTFOLIO", "PENNY", "CORE")
    return ("PORTFOLIO", "CORE", "PENNY")


def focus_advisor_auto_clear(
    candidates: list[dict],
    broker_name: str,
    *,
    fee_clear_fn,
    known_cryptos: set[str] | None = None,
) -> bool:
    """
    True when every candidate clears the fee gate — required for focus fast-path.
    fee_clear_fn(broker, ticker, score, is_crypto, asset_type) -> (ok, why)
    """
    if not candidates:
        return False
    cryptos = known_cryptos or set()
    for c in candidates:
        if not isinstance(c, dict):
            return False
        ticker = str(c.get("ticker") or "").upper()
        if not ticker:
            return False
        if c.get("regime_caution"):
            return False
        asset_type = str(c.get("asset_type") or "")
        is_crypto = (
            "crypto" in asset_type.lower()
            or ticker in cryptos
            or str(broker_name) == "Coinbase"
        )
        try:
            score = float(c.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        ok, _why = fee_clear_fn(
            broker_name, ticker, score=score,
            is_crypto=is_crypto, asset_type=asset_type,
        )
        if not ok:
            return False
    return True
