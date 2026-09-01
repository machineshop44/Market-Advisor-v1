"""
Desk orchestration — focus broker, stuck capital, profit command center.

Sizing is never decided here. All ticket math stays in scoring.risk_sizing_breakdown
via gui._compute_trade_dollars / auto_cycle.capital_planner_snapshot.
"""
from __future__ import annotations

import time
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


def focus_scan_multiplier(broker_name: str, focus_broker: str | None, settings: dict | None) -> float:
    """2× cadence (half interval) for focus broker when mode is on."""
    if not focus_broker or focus_mode(settings) == "off":
        return 1.0
    if str(broker_name or "") != str(focus_broker):
        return 1.0
    try:
        mult = float((settings or {}).get("focus_broker_scan_mult") or 2.0)
    except (TypeError, ValueError):
        mult = 2.0
    return max(1.0, min(4.0, mult))


def interval_scale_for_focus(base_sec: float, broker_name: str, focus_broker: str | None, settings: dict | None) -> float:
    mult = focus_scan_multiplier(broker_name, focus_broker, settings)
    if mult <= 1.0:
        return float(base_sec)
    return max(15.0, float(base_sec) / mult)


def resolve_focus_broker(
    by_broker: dict[str, dict],
    settings: dict | None,
) -> str | None:
    """
    Pick the broker that should receive buy-engine priority.
    auto: highest deployable_bp among can_place_new_buy; tie-break ET > RH > CB.
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
        candidates.append((deploy, str(name)))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]

    def sort_key(item: tuple[float, str]) -> tuple:
        deploy, name = item
        try:
            rank = BROKER_ORDER.index(name)
        except ValueError:
            rank = 99
        return (-deploy, rank)

    candidates.sort(key=sort_key)
    return candidates[0][1]


def focus_parks_buys(broker_name: str, focus_broker: str | None, settings: dict | None) -> bool:
    """True when non-focus broker buy engines should rest."""
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


def next_desk_action(by_broker: dict[str, dict], *, focus_broker: str | None = None) -> str:
    """One-line actionable next step for Home command center."""
    if not by_broker:
        return "Arm auto-trader and refresh balances."
    focus = focus_broker or resolve_focus_broker(by_broker, {"desk_focus_mode": "auto"})
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
    if focus and focus_parks_buys("Robinhood", focus, {"desk_focus_mode": "auto"}):
        # only mention focus when multi-broker
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
    action = next_desk_action(by_broker, focus_broker=focus_broker)
    focus_txt = f" · Focus {focus_broker}" if focus_broker else ""
    line = (
        f"7d net {_m(net)} · WR {wr} · fee {drag:.1f}%"
        f"{focus_txt} — Next: {action}"
    )
    if engine_line:
        line += f" | {engine_line}"
    return line


def format_consolidation_playbook(by_broker: dict[str, dict]) -> str:
    """Human checklist when capital is fragmented across brokers."""
    lines = ["Consolidation playbook (manual — brokers don't auto-transfer):"]
    deployable = []
    stuck = []
    for name, ctx in (by_broker or {}).items():
        if not isinstance(ctx, dict):
            continue
        try:
            bp = float(ctx.get("buying_power") or 0.0)
            deploy = float(ctx.get("deployable_bp") or bp)
        except (TypeError, ValueError):
            bp, deploy = 0.0, 0.0
        if ctx.get("can_place_new_buy") and deploy >= 5.0:
            deployable.append((name, deploy))
        elif (ctx.get("blockers") or []) and str((ctx["blockers"][0] or {}).get("code")) == "fully_deployed":
            stuck.append(name)
    if len(deployable) == 1 and stuck:
        target, amt = deployable[0]
        lines.append(
            f"• Primary stack: {target} (~{amt:.0f} deployable). "
            f"Exit positions on {', '.join(stuck)} then transfer cash to {target} "
            f"for one autosized ticket (sizing still follows live BP)."
        )
    elif len(deployable) > 1:
        names = ", ".join(f"{n} (${d:.0f})" for n, d in deployable)
        lines.append(f"• Multiple buyers: {names} — focus mode picks one; consider merging to reduce fee drag.")
    else:
        lines.append("• No broker can fund a new min ticket — exit or deposit before consolidating.")
    lines.append("• OTC/dust (*Q) bags: sell in broker app; excluded from sizing/deploy counts.")
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


def is_otc_permanent_skip(ticker: str, *, broker_name: str = "") -> bool:
    """OTC/delisted *Q bags — skip repeated portfolio scoring noise."""
    t = str(ticker or "").upper().replace("-USD", "").strip()
    if not t:
        return False
    if t.endswith("Q") and len(t) >= 4:
        return True
    return False


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
