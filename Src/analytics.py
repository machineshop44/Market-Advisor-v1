"""Trade analytics — turnover, estimated fees, paired P&L, posture journal compare."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

try:
    from scoring import (
        estimate_fee_dollars,
        estimate_round_trip_fee_pct,
        fee_profile_key,
        resolve_journal_fee_key,
        normalize_risk_posture,
        get_risk_posture_profile,
    )
except ImportError:
    estimate_fee_dollars = None  # type: ignore
    estimate_round_trip_fee_pct = None  # type: ignore
    fee_profile_key = None  # type: ignore
    resolve_journal_fee_key = None  # type: ignore
    normalize_risk_posture = lambda x: "balanced"  # noqa: E731
    get_risk_posture_profile = lambda x=None: {}  # noqa: E731


def _parse_ts(row: dict) -> datetime | None:
    ts = str(row.get("timestamp") or "")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", ""))
    except Exception:
        return None


def _is_fill(row: dict) -> bool:
    if row.get("confirmed") is False:
        return False
    status = str(row.get("status") or "")
    if "Fail" in status or "Skipped" in status or "Pending" in status:
        return False
    if row.get("confirmed") is True:
        return True
    return "Filled" in status or "[PAPER]" in status


def _notional(row: dict) -> float:
    try:
        d = float(row.get("dollars") or 0.0)
        if d > 0:
            return abs(d)
    except (TypeError, ValueError):
        pass
    try:
        px = float(row.get("price") or 0.0)
        qty = float(row.get("qty") or 0.0)
        return abs(px * qty)
    except (TypeError, ValueError):
        return 0.0


def read_journal_since(path: str, days: int | None = 7, limit: int = 5000) -> list[dict]:
    """Load journal rows from the last N days (newest last).

    ``days=None`` (or < 0) = all time — no date cutoff; still capped by ``limit``.
    """
    import json
    import os

    if not os.path.exists(path):
        return []
    all_time = days is None or int(days) < 0
    cutoff = None if all_time else datetime.now() - timedelta(days=max(0, int(days)))
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        for ln in lines[-max(limit, 1):]:
            try:
                row = json.loads(ln)
            except Exception:
                continue
            ts = _parse_ts(row)
            if ts is None:
                continue
            if cutoff is not None and ts < cutoff:
                continue
            rows.append(row)
    except Exception:
        return []
    return rows


def summarize_fills(rows: list[dict], *, broker: str | None = None) -> dict[str, Any]:
    """
    Aggregate turnover, est. fees, rotate counts, paired realized P&L,
    win rate, and avg hold proxy (when buy/sell timestamps exist).
    """
    buys = sells = rotates = 0
    buy_notional = sell_notional = fee_est = 0.0
    by_broker: dict[str, dict] = {}

    def _bkt(name: str) -> dict:
        if name not in by_broker:
            by_broker[name] = {
                "buys": 0,
                "sells": 0,
                "rotates": 0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "fee_est": 0.0,
                "realized_pnl": 0.0,
                "wins": 0,
                "losses": 0,
                "net_wins": 0,
                "net_losses": 0,
                "hold_minutes_sum": 0.0,
                "hold_samples": 0,
            }
        return by_broker[name]

    # Open lots for FIFO-ish pairing: (broker, ticker) -> list of {qty, price, dollars, ts}
    lots: dict[tuple[str, str], list[dict]] = {}
    realized = 0.0
    wins = losses = net_wins = net_losses = 0
    hold_sum = 0.0
    hold_n = 0

    for row in rows:
        if not _is_fill(row):
            continue
        b = str(row.get("broker") or "Unknown")
        if broker and b != broker:
            continue
        side = str(row.get("side") or "").upper()
        ticker = str(row.get("ticker") or "").upper()
        notion = _notional(row)
        reason = str(row.get("reason") or "")
        asset_type = str(row.get("asset_type") or "")
        fee_key = row.get("fee_profile") or ""
        resolved_fee_key = fee_key
        if resolve_journal_fee_key is not None:
            try:
                resolved_fee_key = resolve_journal_fee_key(fee_key, b, ticker, asset_type)
            except Exception:
                resolved_fee_key = fee_key
        row_ts = _parse_ts(row)
        # Prefer broker invoice fields; else stored fee_est; else profile estimate
        broker_fe = _row_broker_fee_dollars(row)
        try:
            fe = float(row.get("fee_est")) if row.get("fee_est") is not None else None
        except (TypeError, ValueError):
            fe = None
        if broker_fe is not None:
            fe = float(broker_fe)
        elif fe is None and estimate_fee_dollars is not None:
            fe = estimate_fee_dollars(
                notion, resolved_fee_key or b, ticker, asset_type, round_trip=False
            )
        fe = float(fe or 0.0)

        bucket = _bkt(b)
        fee_est += fe
        bucket["fee_est"] += fe

        if "ROTATE" in reason.upper():
            rotates += 1
            bucket["rotates"] += 1

        if side == "BUY":
            buys += 1
            buy_notional += notion
            bucket["buys"] += 1
            bucket["buy_notional"] += notion
            key = (b, ticker)
            lots.setdefault(key, []).append({
                "qty": abs(float(row.get("qty") or 0) or (notion / float(row.get("price") or 1))),
                "price": float(row.get("price") or 0),
                "dollars": notion,
                "fee": fe,
                "ts": row_ts,
            })
        elif side == "SELL":
            sells += 1
            sell_notional += notion
            bucket["sells"] += 1
            bucket["sell_notional"] += notion
            key = (b, ticker)
            sell_qty = abs(float(row.get("qty") or 0) or 0.0)
            sell_px = float(row.get("price") or 0.0)
            remaining = sell_qty if sell_qty > 0 else 0.0
            if remaining <= 0 and sell_px > 0 and notion > 0:
                remaining = notion / sell_px
            queue = lots.get(key) or []
            pnl = 0.0
            buy_fees = 0.0
            while remaining > 1e-12 and queue:
                lot = queue[0]
                take = min(remaining, float(lot.get("qty") or 0))
                lot_qty = float(lot.get("qty") or 0)
                cost_px = float(lot.get("price") or 0)
                pnl += (sell_px - cost_px) * take
                lot_fee = float(lot.get("fee") or 0.0)
                if lot_qty > 1e-12 and lot_fee > 0:
                    buy_fees += lot_fee * (take / lot_qty)
                buy_ts = lot.get("ts")
                if buy_ts is not None and row_ts is not None:
                    hold_m = max(0.0, (row_ts - buy_ts).total_seconds() / 60.0)
                    hold_sum += hold_m
                    hold_n += 1
                    bucket["hold_minutes_sum"] += hold_m
                    bucket["hold_samples"] += 1
                lot["qty"] = lot_qty - take
                remaining -= take
                if float(lot.get("qty") or 0) <= 1e-12:
                    queue.pop(0)
            net_pnl = pnl - fe - buy_fees
            realized += pnl
            bucket["realized_pnl"] += pnl
            if pnl > 1e-9:
                wins += 1
                bucket["wins"] += 1
            elif pnl < -1e-9:
                losses += 1
                bucket["losses"] += 1
            if net_pnl > 1e-9:
                net_wins += 1
                bucket["net_wins"] += 1
            elif net_pnl < -1e-9:
                net_losses += 1
                bucket["net_losses"] += 1

    turnover = buy_notional + sell_notional
    closed = wins + losses
    win_rate = (wins / closed) if closed > 0 else None
    net_closed = net_wins + net_losses
    net_win_rate = (net_wins / net_closed) if net_closed > 0 else None
    avg_hold_min = (hold_sum / hold_n) if hold_n > 0 else None
    for bkt in by_broker.values():
        c = int(bkt.get("wins") or 0) + int(bkt.get("losses") or 0)
        bkt["win_rate"] = (bkt["wins"] / c) if c > 0 else None
        nc = int(bkt.get("net_wins") or 0) + int(bkt.get("net_losses") or 0)
        bkt["net_win_rate"] = (bkt["net_wins"] / nc) if nc > 0 else None
        hs = int(bkt.get("hold_samples") or 0)
        bkt["avg_hold_min"] = (bkt["hold_minutes_sum"] / hs) if hs > 0 else None
        # Fee drag = est fees / turnover for this broker
        bt = float(bkt.get("buy_notional") or 0) + float(bkt.get("sell_notional") or 0)
        bkt["fee_drag_pct"] = (float(bkt.get("fee_est") or 0) / bt * 100.0) if bt > 0 else 0.0
        bkt["net_after_fees"] = float(bkt.get("realized_pnl") or 0) - float(bkt.get("fee_est") or 0)

    return {
        "buys": buys,
        "sells": sells,
        "rotates": rotates,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "turnover": turnover,
        "fee_est": fee_est,
        "fee_drag_pct": (fee_est / turnover * 100.0) if turnover > 0 else 0.0,
        "realized_pnl": realized,
        "net_after_fees": realized - fee_est,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_wins": net_wins,
        "net_losses": net_losses,
        "net_win_rate": net_win_rate,
        "avg_hold_min": avg_hold_min,
        "by_broker": by_broker,
    }


def _row_broker_fee_dollars(row: dict) -> float | None:
    """Prefer explicit broker invoice fields when present on a journal row."""
    for key in ("fee_paid", "commission", "broker_fee", "fees"):
        if row.get(key) is None:
            continue
        try:
            val = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if val >= 0:
            return val
    return None


def extract_fee_dollars_from_order(payload: Any) -> float | None:
    """
    Best-effort parse of broker-reported fees from order / fill payloads.

    RH stock: ``fees``
    CB Advanced: ``total_fees`` / ``commission`` / ``order_configuration`` crumbs
    E*TRADE: ``estimatedCommission`` / ``estimatedFees`` / ``commission``
    Returns None when no usable field (caller keeps Est. fee_est).
    """
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        try:
            v = float(payload)
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None

    keys = (
        "fee_paid",
        "fees",
        "fee",
        "total_fees",
        "totalFees",
        "commission",
        "Commission",
        "estimatedCommission",
        "estimatedFees",
        "broker_fee",
        "brokerage_fee",
    )
    for key in keys:
        if key not in payload or payload.get(key) is None:
            continue
        try:
            val = float(payload.get(key))
        except (TypeError, ValueError):
            # Nested money objects e.g. {"value": "0.12"}
            nested = payload.get(key)
            if isinstance(nested, dict):
                for nk in ("value", "amount", "Value"):
                    if nested.get(nk) is None:
                        continue
                    try:
                        val = float(nested.get(nk))
                        break
                    except (TypeError, ValueError):
                        val = None
                else:
                    continue
            else:
                continue
        if val is not None and val >= 0:
            return float(val)

    # Coinbase nested order blob
    for nest_key in ("order", "Order", "OrderDetail", "orderDetail"):
        nested = payload.get(nest_key)
        if isinstance(nested, dict):
            found = extract_fee_dollars_from_order(nested)
            if found is not None:
                return found
    return None


def summarize_fee_confidence(rows: list[dict]) -> dict[str, Any]:
    """
    Classify fee honesty for Reports.

    Levels:
      High  — all fills carry broker fee fields (fee_paid / commission / …)
      Med   — mix of broker fields and profile estimates
      Low   — profile / stored fee_est only (current default path)

    Does not invent broker invoices — only reads what journal rows provide.
    """
    fills = [r for r in (rows or []) if isinstance(r, dict) and _is_fill(r)]
    broker_n = estimate_n = 0
    for row in fills:
        if _row_broker_fee_dollars(row) is not None:
            broker_n += 1
        else:
            estimate_n += 1
    total = broker_n + estimate_n
    if total == 0:
        level, label = "none", "No fills"
        tip = "No filled trades in this window — fee confidence N/A."
    elif estimate_n == 0:
        level, label = "high", "High · broker fees"
        tip = "Fees from broker-reported fields on fills (not profile estimates)."
    elif broker_n == 0:
        level, label = "low", "Low · Est. (profile)"
        tip = (
            "Fees are profile estimates — not broker invoices. "
            "Net≈ = realized P&L − est. fees."
        )
    else:
        level, label = "med", "Med · mixed"
        tip = (
            f"{broker_n} fill(s) with broker fee fields; "
            f"{estimate_n} still profile estimates."
        )
    return {
        "level": level,
        "label": label,
        "tip": tip,
        "broker_fee_n": broker_n,
        "estimate_n": estimate_n,
        "fills": total,
        "chip": f"Fee confidence: {label}",
    }


def fee_drag_coach(
    summary: dict[str, Any] | None,
    *,
    small_turnover: float = 5000.0,
    high_drag_pct: float = 0.45,
) -> str | None:
    """
    Small-account fee-drag coach for Reports.
    Returns a short tip when est. drag is elevated vs turnover, else None.
    """
    s = summary or {}
    try:
        turnover = float(s.get("turnover") or 0.0)
        drag = float(s.get("fee_drag_pct") or 0.0)
    except (TypeError, ValueError):
        return None
    if turnover <= 0:
        return None
    if turnover < float(small_turnover) and drag >= float(high_drag_pct):
        return (
            f"Small-account tip: est. fee drag {drag:.2f}% of turnover "
            f"({turnover:,.0f} window) — prefer fewer rotates / larger size when edge is thin."
        )
    return None


def format_reports_hero(
    summary: dict[str, Any],
    *,
    money_fmt=None,
    window_label: str = "",
) -> str:
    """
    Hero = net after fees first (money honesty), then fee drag + trade count.
    money_fmt(x) -> currency string; defaults to plain $ formatting.
    """
    def _m(x):
        if callable(money_fmt):
            return money_fmt(x)
        try:
            return f"${float(x or 0):,.2f}"
        except (TypeError, ValueError):
            return "$0.00"

    s = summary or {}
    wr = s.get("win_rate")
    nwr = s.get("net_win_rate")
    if wr is not None and nwr is not None:
        wr_txt = f"{wr * 100:.0f}% gross / {nwr * 100:.0f}% net"
    elif wr is not None:
        wr_txt = f"{wr * 100:.0f}%"
    else:
        wr_txt = "—"
    hold = s.get("avg_hold_min")
    hold_txt = f"{hold:.0f}m" if hold is not None else "—"
    win_lbl = f" · {window_label}" if window_label else ""
    buys = int(s.get("buys") or 0)
    sells = int(s.get("sells") or 0)
    rotates = int(s.get("rotates") or 0)
    trades = buys + sells
    net = s.get("net_after_fees")
    if net is None:
        try:
            net = float(s.get("realized_pnl") or 0) - float(s.get("fee_est") or 0)
        except (TypeError, ValueError):
            net = 0.0
    hero = (
        f"Net≈ {_m(net)}{win_lbl} · "
        f"Realized {_m(s.get('realized_pnl'))} − fees {_m(s.get('fee_est'))} "
        f"({float(s.get('fee_drag_pct') or 0):.2f}% drag) · "
        f"{trades} trades"
    )
    secondary = (
        f"Buys {buys} · Sells {sells} · Rotates {rotates} · "
        f"Win rate {wr_txt} · Avg hold {hold_txt} · "
        f"Turnover {_m(s.get('turnover'))}"
    )
    return f"{hero}\n{secondary}"


def compare_posture_fees(rows: list[dict], postures: list[str] | None = None) -> dict[str, Any]:
    """
    Lite journal replay: re-estimate fees under each posture's fee_buffer mindset.
    Does NOT re-simulate signals — only executed-fill economics with profile RT fees.
    """
    postures = postures or ["safer", "balanced", "aggressive"]
    base = summarize_fills(rows)
    out: dict[str, Any] = {"baseline": base, "postures": {}}
    if estimate_round_trip_fee_pct is None:
        return out

    for posture in postures:
        p = normalize_risk_posture(posture)
        # Scale fee estimate slightly by posture patience (aggressive holds longer → fewer RTs
        # is not modeled; we only show fee_$ if every fill paid profile RT/2).
        fee_total = 0.0
        for row in rows:
            if not _is_fill(row):
                continue
            notion = _notional(row)
            b = str(row.get("broker") or "")
            t = str(row.get("ticker") or "")
            asset_type = str(row.get("asset_type") or "")
            fee_total += estimate_fee_dollars(
                notion, row.get("fee_profile") or b, t, asset_type, round_trip=False
            )
        prof = get_risk_posture_profile(p)
        # Same fee math on executed fills; posture only changes capacity/rotate context
        out["postures"][p] = {
            "label": prof.get("label", p),
            "fee_est": fee_total,
            "realized_pnl": base.get("realized_pnl", 0.0),
            "net_after_fees": float(base.get("realized_pnl") or 0) - fee_total,
            "max_open": prof.get("max_open_positions"),
            "day_dd_pause_pct": prof.get("day_dd_pause_pct"),
            "note": (
                "Same fill fees for every posture — capacity/DD rails differ. "
                "Does not replay skipped signals."
            ),
        }
    return out


# Decision-journal actions that count as skips (Reports / buy-rate denominator).
_DECISION_SKIP_ACTIONS = frozenset({"SKIP", "ROTATE_SKIP", "SCALE_IN_SKIP", "IDLE_SKIP"})


def summarize_decisions(rows: list[dict]) -> dict[str, Any]:
    """Aggregate decision_journal rows: skips, buys, top reasons, buy rate."""
    from collections import Counter

    buys = skips = fails = rotates_skip = scale_in_skips = idle_skips = 0
    reasons: Counter = Counter()
    regime_blocked = 0
    by_broker: dict[str, dict] = {}
    by_action: Counter = Counter()

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "").upper()
        reason = str(row.get("reason") or "unknown")
        broker = str(row.get("broker") or "Unknown")
        by_action[action or "?"] += 1
        if broker not in by_broker:
            by_broker[broker] = {"buys": 0, "skips": 0, "fails": 0, "total": 0}
        by_broker[broker]["total"] += 1

        if action == "BUY":
            buys += 1
            by_broker[broker]["buys"] += 1
        elif action in ("BUY_FAIL", "FAIL"):
            fails += 1
            by_broker[broker]["fails"] += 1
            reasons[reason.split(":")[0][:40]] += 1
        elif action in _DECISION_SKIP_ACTIONS:
            skips += 1
            by_broker[broker]["skips"] += 1
            key = reason.split(":")[0][:48] if reason else "unknown"
            reasons[key] += 1
            if action == "ROTATE_SKIP":
                rotates_skip += 1
            elif action == "SCALE_IN_SKIP":
                scale_in_skips += 1
            elif action == "IDLE_SKIP":
                idle_skips += 1
        else:
            reasons[f"other:{action or '?'}"] += 1

        if row.get("regime_ok") is False:
            regime_blocked += 1

    attempts = buys + skips + fails
    buy_rate = (buys / attempts) if attempts else None
    top_reasons = [{"reason": r, "count": c} for r, c in reasons.most_common(8)]
    return {
        "total": len(rows or []),
        "buys": buys,
        "skips": skips,
        "fails": fails,
        "rotate_skips": rotates_skip,
        "scale_in_skips": scale_in_skips,
        "idle_skips": idle_skips,
        "buy_rate": buy_rate,
        "regime_blocked": regime_blocked,
        "top_reasons": top_reasons,
        "by_broker": by_broker,
        "by_action": dict(by_action),
    }


def lite_posture_decision_replay(rows: list[dict], postures: list[str] | None = None) -> dict[str, Any]:
    """
    Lite replay of SKIP decisions under alternate postures.

    Uses logged open_count / max_open / reason — not a full market simulation.
    - max_open skips: would clear if posture.max_open > logged open_count (or 0 = unlimited)
    - concentration / low_bp: same across postures (count as still blocked)
    - Safer: no opportunity-swap → rotate-related recovery not assumed
    """
    postures = postures or ["safer", "balanced", "aggressive"]
    base = summarize_decisions(rows)
    out: dict[str, Any] = {
        "baseline": base,
        "postures": {},
        "note": (
            "Decision-log replay only — does not resimulate prices or signals. "
            "max_open skips may clear under roomier postures when open_count was logged."
        ),
    }
    skip_rows = [
        r for r in (rows or [])
        if isinstance(r, dict) and str(r.get("action") or "").upper() in _DECISION_SKIP_ACTIONS
    ]

    for posture in postures:
        p = normalize_risk_posture(posture)
        prof = get_risk_posture_profile(p)
        try:
            max_open = int(prof.get("max_open_positions") or 0)
        except (TypeError, ValueError):
            max_open = 8
        swap_on = bool(prof.get("allow_scale_in") is not None)  # placeholder
        # opportunity swap from separate params
        try:
            from scoring import opportunity_swap_enabled
            swap_on = opportunity_swap_enabled(p)
        except Exception:
            swap_on = p != "safer"

        would_clear = 0
        still_blocked = 0
        for row in skip_rows:
            reason = str(row.get("reason") or "").lower()
            cleared = False
            if reason.startswith("max_open"):
                try:
                    open_count = int(row.get("open_count"))
                except (TypeError, ValueError):
                    open_count = None
                if open_count is not None:
                    if max_open <= 0 or open_count < max_open:
                        cleared = True
                elif max_open <= 0:
                    cleared = True
            elif "rotate" in reason or reason.startswith("low_bp") or "cluster" in reason:
                # Roomier books / swap may help low_bp+cluster only if swap on — conservative: no clear
                if swap_on and ("low_bp" in reason or "cluster" in reason or "max_open" in reason):
                    # Without state we can't know — don't invent clears for rotate
                    cleared = False
            if cleared:
                would_clear += 1
            else:
                still_blocked += 1

        out["postures"][p] = {
            "label": prof.get("label", p),
            "max_open": max_open,
            "swap_enabled": swap_on,
            "skips_seen": len(skip_rows),
            "would_clear_max_open": would_clear,
            "still_blocked": still_blocked,
            "buys_logged": base.get("buys", 0),
        }
    return out


def _fill_rows_sorted(rows: list[dict]) -> list[dict]:
    fills = [r for r in (rows or []) if isinstance(r, dict) and _is_fill(r)]
    fills.sort(key=lambda r: (_parse_ts(r) or datetime.min))
    return fills


def walk_forward_fee_replay(
    fill_rows: list[dict],
    decision_rows: list[dict] | None = None,
    *,
    n_folds: int = 3,
) -> dict[str, Any]:
    """
    Chronological fee-aware walk-forward on journal fills (not a bar backtest).

    Splits confirmed fills into time-ordered folds. Fold k (k>=1) is out-of-sample
    vs folds 0..k-1 as in-sample. Reports fee-aware net P&L / win rate per fold.

    Assumptions (surfaced in ``assumptions``):
    - Prices come from the trade journal (fills), not independent OHLCV bars.
    - Fees use stored ``fee_est`` or broker fee-profile estimates.
    - Decision skips do not invent P&L — only capacity / buy-rate context.
    - Unpaired open lots contribute 0 realized until a matching sell is logged.
    """
    fills = _fill_rows_sorted(fill_rows)
    n_folds = max(2, min(8, int(n_folds or 3)))
    assumptions = [
        "Journal fill prices only — not an independent bar / QuantConnect backtest.",
        "Fees from fee_est or profile estimate; net = realized P&L − est. fees.",
        "Open lots without a matching sell contribute $0 realized in-window.",
        "Decision skips are capacity context only — no synthetic fills invented.",
    ]
    empty = {
        "folds": [],
        "overall": summarize_fills(fills),
        "decisions": summarize_decisions(decision_rows or []),
        "n_fills": len(fills),
        "n_folds": n_folds,
        "assumptions": assumptions,
        "note": "Need at least 2 fills spanning folds for walk-forward.",
    }
    if len(fills) < 2:
        return empty

    # Equal-count folds (time-ordered); last fold absorbs remainder
    fold_size = max(1, len(fills) // n_folds)
    folds_rows: list[list[dict]] = []
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else len(fills)
        if start >= len(fills):
            break
        chunk = fills[start:end]
        if chunk:
            folds_rows.append(chunk)
    if len(folds_rows) < 2:
        return empty

    fold_summaries = []
    for i, chunk in enumerate(folds_rows):
        s = summarize_fills(chunk)
        ts0 = _parse_ts(chunk[0])
        ts1 = _parse_ts(chunk[-1])
        fold_summaries.append({
            "fold": i,
            "n_fills": len(chunk),
            "from": ts0.isoformat(timespec="seconds") if ts0 else None,
            "to": ts1.isoformat(timespec="seconds") if ts1 else None,
            "realized_pnl": s.get("realized_pnl", 0.0),
            "fee_est": s.get("fee_est", 0.0),
            "net_after_fees": s.get("net_after_fees", 0.0),
            "win_rate": s.get("win_rate"),
            "turnover": s.get("turnover", 0.0),
            "buys": s.get("buys", 0),
            "sells": s.get("sells", 0),
        })

    walk = []
    for k in range(1, len(folds_rows)):
        is_rows: list[dict] = []
        for j in range(k):
            is_rows.extend(folds_rows[j])
        oos_rows = folds_rows[k]
        is_s = summarize_fills(is_rows)
        oos_s = summarize_fills(oos_rows)
        walk.append({
            "step": k,
            "in_sample_folds": list(range(k)),
            "oos_fold": k,
            "in_sample": {
                "n_fills": len(is_rows),
                "net_after_fees": is_s.get("net_after_fees", 0.0),
                "win_rate": is_s.get("win_rate"),
                "fee_drag_pct": is_s.get("fee_drag_pct", 0.0),
            },
            "out_of_sample": {
                "n_fills": len(oos_rows),
                "net_after_fees": oos_s.get("net_after_fees", 0.0),
                "win_rate": oos_s.get("win_rate"),
                "fee_drag_pct": oos_s.get("fee_drag_pct", 0.0),
                "realized_pnl": oos_s.get("realized_pnl", 0.0),
                "fee_est": oos_s.get("fee_est", 0.0),
            },
        })

    oos_nets = [w["out_of_sample"]["net_after_fees"] for w in walk]
    return {
        "folds": fold_summaries,
        "walk_forward": walk,
        "oos_net_sum": sum(oos_nets),
        "oos_steps": len(walk),
        "overall": summarize_fills(fills),
        "decisions": summarize_decisions(decision_rows or []),
        "n_fills": len(fills),
        "n_folds": len(folds_rows),
        "assumptions": assumptions,
        "note": (
            f"{len(folds_rows)} time folds · {len(walk)} OOS steps · "
            "fee-aware net from journal fills only."
        ),
    }


def summarize_fill_quality(rows: list[dict]) -> dict[str, Any]:
    """
    Aggregate expected-vs-fill slippage from journal rows that carry
    ``slippage_bps`` (or reconstruct from quote_price / fill_price).
    """
    samples = 0
    slip_sum = 0.0
    adverse = 0
    favorable = 0
    by_side: dict[str, dict] = {
        "BUY": {"n": 0, "slip_sum": 0.0, "adverse": 0},
        "SELL": {"n": 0, "slip_sum": 0.0, "adverse": 0},
    }
    missing = 0

    for row in rows or []:
        if not isinstance(row, dict) or not _is_fill(row):
            continue
        side = str(row.get("side") or "").upper()
        bps = row.get("slippage_bps")
        if bps is None:
            try:
                quote = float(row.get("quote_price") or row.get("expected_price") or 0)
                fill = float(row.get("fill_price") or row.get("price") or 0)
            except (TypeError, ValueError):
                quote = fill = 0.0
            if quote > 0 and fill > 0:
                if side == "BUY":
                    bps = (fill - quote) / quote * 10000.0
                elif side == "SELL":
                    bps = (quote - fill) / quote * 10000.0
                else:
                    bps = None
            else:
                bps = None
        if bps is None:
            missing += 1
            continue
        try:
            bps_f = float(bps)
        except (TypeError, ValueError):
            missing += 1
            continue
        samples += 1
        slip_sum += bps_f
        if bps_f > 0.5:
            adverse += 1
        elif bps_f < -0.5:
            favorable += 1
        bucket = by_side.get(side)
        if bucket is not None:
            bucket["n"] += 1
            bucket["slip_sum"] += bps_f
            if bps_f > 0.5:
                bucket["adverse"] += 1

    avg = (slip_sum / samples) if samples else None
    adverse_rate = (adverse / samples) if samples else None
    for b in by_side.values():
        n = int(b["n"])
        b["avg_slippage_bps"] = (b["slip_sum"] / n) if n else None
        b["adverse_rate"] = (b["adverse"] / n) if n else None
        del b["slip_sum"]
    return {
        "samples": samples,
        "missing_slippage": missing,
        "avg_slippage_bps": avg,
        "adverse_count": adverse,
        "favorable_count": favorable,
        "adverse_rate": adverse_rate,
        "by_side": by_side,
        "note": (
            "Slippage bps vs quote at order time (BUY: fill−quote; SELL: quote−fill). "
            "Positive = adverse. Rows without quote/fill metadata are counted in missing."
        ),
    }


def compare_paper_live(rows: list[dict]) -> dict[str, Any]:
    """
    Shadow compare paper vs live fills in the same window (session quality strip).
    Uses ``paper`` bool on journal rows when present.
    """
    paper_rows = []
    live_rows = []
    for row in rows or []:
        if not isinstance(row, dict) or not _is_fill(row):
            continue
        if row.get("paper") is True or "[PAPER]" in str(row.get("status") or ""):
            paper_rows.append(row)
        else:
            live_rows.append(row)

    paper_s = summarize_fills(paper_rows)
    live_s = summarize_fills(live_rows)
    paper_fq = summarize_fill_quality(paper_rows)
    live_fq = summarize_fill_quality(live_rows)

    def _pack(label: str, s: dict, fq: dict, n: int) -> dict:
        return {
            "label": label,
            "fills": n,
            "realized_pnl": s.get("realized_pnl", 0.0),
            "net_after_fees": s.get("net_after_fees", 0.0),
            "win_rate": s.get("win_rate"),
            "fee_drag_pct": s.get("fee_drag_pct", 0.0),
            "avg_slippage_bps": fq.get("avg_slippage_bps"),
            "adverse_rate": fq.get("adverse_rate"),
        }

    both = len(paper_rows) > 0 and len(live_rows) > 0
    note = (
        "Paper vs live from journal ``paper`` flag / [PAPER] status in the same window."
        if both
        else "Need fills in both paper and live modes in this window for a shadow compare."
    )
    delta_net = None
    if both:
        delta_net = float(live_s.get("net_after_fees") or 0) - float(paper_s.get("net_after_fees") or 0)
    return {
        "both_modes": both,
        "paper": _pack("Paper", paper_s, paper_fq, len(paper_rows)),
        "live": _pack("Live", live_s, live_fq, len(live_rows)),
        "delta_live_minus_paper_net": delta_net,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Bar / OHLCV walk-forward (independent of journal fill prices)
# ---------------------------------------------------------------------------

DEFAULT_BAR_STOP_PCT = 0.035  # ~3.5% hard-stop proxy when broker stop unknown
DEFAULT_BAR_FEE_BPS = 5.0     # round-trip estimate when fee_est missing (stocks)


def _yahoo_symbol(ticker: str, *, is_crypto: bool = False) -> str:
    clean = str(ticker or "").upper().replace("-USD", "").strip()
    if not clean:
        return ""
    if is_crypto or clean in {"BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "AVAX", "LINK", "DOT", "MATIC"}:
        return f"{clean}-USD"
    return clean


def fetch_ohlcv_bars(
    ticker: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    period: str = "60d",
    interval: str = "1d",
    is_crypto: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch OHLCV bars via yfinance. Returns list of
    {ts, open, high, low, close, volume} sorted ascending.
    Failures return [] (caller surfaces in assumptions).
    """
    sym = _yahoo_symbol(ticker, is_crypto=is_crypto)
    if not sym:
        return []
    try:
        import yfinance as yf
    except Exception:
        return []
    try:
        t = yf.Ticker(sym)
        kwargs: dict[str, Any] = {"interval": interval, "auto_adjust": True}
        if start is not None:
            kwargs["start"] = start.strftime("%Y-%m-%d")
            if end is not None:
                kwargs["end"] = (end + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            kwargs["period"] = period
        df = t.history(**kwargs)
        if df is None or len(df) == 0:
            return []
        out: list[dict[str, Any]] = []
        for idx, row in df.iterrows():
            try:
                ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else datetime.fromisoformat(str(idx)[:19])
                if getattr(ts, "tzinfo", None) is not None:
                    ts = ts.replace(tzinfo=None)
            except Exception:
                continue
            try:
                o = float(row.get("Open") or 0)
                h = float(row.get("High") or 0)
                lo = float(row.get("Low") or 0)
                c = float(row.get("Close") or 0)
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            if not is_crypto:
                try:
                    from market_calendar import is_equity_session_day
                    d = ts.date() if hasattr(ts, "date") else None
                    if d is not None and not is_equity_session_day(d):
                        continue
                except Exception:
                    pass
            out.append({
                "ts": ts,
                "open": o if o > 0 else c,
                "high": h if h > 0 else c,
                "low": lo if lo > 0 else c,
                "close": c,
                "volume": float(row.get("Volume") or 0),
            })
        out.sort(key=lambda b: b["ts"])
        return out
    except Exception:
        return []


def simulate_bar_round_trip(
    bars: list[dict],
    entry_ts: datetime,
    qty: float,
    *,
    exit_ts: datetime | None = None,
    stop_pct: float = DEFAULT_BAR_STOP_PCT,
    fee_dollars: float = 0.0,
) -> dict[str, Any] | None:
    """
    Core bar-replay math for one long: enter at first bar open at/after entry_ts;
    exit on stop (bar low ≤ entry×(1−stop)), or at first bar at/after exit_ts close,
    or at last bar close.
    """
    if not bars or qty <= 0 or entry_ts is None:
        return None
    entry_i = None
    for i, b in enumerate(bars):
        if b["ts"] >= entry_ts:
            entry_i = i
            break
    if entry_i is None:
        return None
    entry = bars[entry_i]
    entry_px = float(entry.get("open") or entry.get("close") or 0)
    if entry_px <= 0:
        return None
    stop_px = entry_px * (1.0 - max(0.001, float(stop_pct or DEFAULT_BAR_STOP_PCT)))
    exit_i = None
    exit_reason = "eod"
    exit_px = float(bars[-1].get("close") or 0)
    for j in range(entry_i + 1, len(bars)):
        b = bars[j]
        lo = float(b.get("low") or 0)
        if lo > 0 and lo <= stop_px:
            exit_i = j
            exit_px = stop_px
            exit_reason = "stop"
            break
        if exit_ts is not None and b["ts"] >= exit_ts:
            exit_i = j
            exit_px = float(b.get("close") or exit_px)
            exit_reason = "signal"
            break
    if exit_i is None:
        exit_i = len(bars) - 1
        exit_px = float(bars[exit_i].get("close") or exit_px)
        exit_reason = "eod"
    if exit_px <= 0:
        return None
    pnl = (exit_px - entry_px) * float(qty)
    fee = float(fee_dollars or 0)
    hold_bars = max(0, exit_i - entry_i)
    return {
        "entry_ts": entry["ts"],
        "exit_ts": bars[exit_i]["ts"],
        "entry_px": entry_px,
        "exit_px": exit_px,
        "qty": float(qty),
        "realized_pnl": pnl,
        "fee_est": fee,
        "net_after_fees": pnl - fee,
        "exit_reason": exit_reason,
        "hold_bars": hold_bars,
        "win": pnl > 1e-9,
    }


def _estimate_rt_fee(row: dict, notional: float) -> float:
    broker_fe = _row_broker_fee_dollars(row)
    if broker_fe is not None:
        # Broker field is typically one-way; double for RT proxy when pairing
        return float(broker_fe) * 2.0 if broker_fe > 0 else abs(notional) * DEFAULT_BAR_FEE_BPS / 10000.0
    try:
        fe = float(row.get("fee_est")) if row.get("fee_est") is not None else None
    except (TypeError, ValueError):
        fe = None
    if fe is not None and fe >= 0:
        # Stored fee_est is typically one-way; double for RT proxy
        return float(fe) * 2.0 if fe > 0 else notional * DEFAULT_BAR_FEE_BPS / 10000.0
    if estimate_fee_dollars is not None:
        try:
            fk = row.get("fee_profile") or row.get("broker") or ""
            if resolve_journal_fee_key is not None:
                fk = resolve_journal_fee_key(
                    fk,
                    row.get("broker") or "",
                    row.get("ticker"),
                    row.get("asset_type") or "",
                )
            return float(estimate_fee_dollars(
                notional,
                fk,
                row.get("ticker"),
                row.get("asset_type") or "",
                round_trip=True,
            ) or 0)
        except Exception:
            pass
    return abs(notional) * DEFAULT_BAR_FEE_BPS / 10000.0


def build_bar_candidates_from_journal(fill_rows: list[dict]) -> list[dict[str, Any]]:
    """Pair journal BUY fills with matching SELLs → candidates for bar replay."""
    fills = _fill_rows_sorted(fill_rows)
    open_lots: dict[tuple[str, str], list[dict]] = {}
    candidates: list[dict] = []
    for row in fills:
        side = str(row.get("side") or "").upper()
        broker = str(row.get("broker") or "Unknown")
        ticker = str(row.get("ticker") or "").upper().replace("-USD", "")
        if not ticker:
            continue
        ts = _parse_ts(row)
        if ts is None:
            continue
        try:
            qty = abs(float(row.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0.0
        notion = _notional(row)
        if qty <= 0 and notion > 0:
            try:
                px = float(row.get("price") or 0)
                if px > 0:
                    qty = notion / px
            except (TypeError, ValueError):
                pass
        key = (broker, ticker)
        is_crypto = (
            "crypto" in str(row.get("asset_type") or "").lower()
            or ticker in {"BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "AVAX", "LINK", "DOT", "MATIC"}
        )
        if side == "BUY" and qty > 0:
            open_lots.setdefault(key, []).append({
                "ts": ts, "qty": qty, "notional": notion, "row": row, "is_crypto": is_crypto,
            })
        elif side == "SELL" and qty > 0:
            queue = open_lots.get(key) or []
            remaining = qty
            while remaining > 1e-12 and queue:
                lot = queue[0]
                take = min(remaining, float(lot["qty"]))
                fee = _estimate_rt_fee(lot["row"], float(lot.get("notional") or 0) * (take / max(lot["qty"], 1e-12)))
                fee_src = "broker" if _row_broker_fee_dollars(lot["row"]) is not None else "estimate"
                stop_pct = DEFAULT_BAR_STOP_PCT
                try:
                    from scoring import get_stop_distance_pct
                    stop_pct = abs(float(get_stop_distance_pct(
                        lot["row"].get("broker") or broker,
                        ticker=ticker,
                        asset_type=lot["row"].get("asset_type") or "",
                    ) or DEFAULT_BAR_STOP_PCT))
                except Exception:
                    stop_pct = DEFAULT_BAR_STOP_PCT
                candidates.append({
                    "broker": broker,
                    "ticker": ticker,
                    "entry_ts": lot["ts"],
                    "exit_ts": ts,
                    "qty": take,
                    "fee_est": fee,
                    "fee_source": fee_src,
                    "stop_pct": stop_pct,
                    "is_crypto": bool(lot.get("is_crypto")),
                    "source": "journal_pair",
                })
                lot["qty"] = float(lot["qty"]) - take
                remaining -= take
                if float(lot["qty"]) <= 1e-12:
                    queue.pop(0)
    # Unpaired buys — exit at last bar (eod)
    for (broker, ticker), queue in open_lots.items():
        for lot in queue:
            if float(lot.get("qty") or 0) <= 1e-12:
                continue
            fee = _estimate_rt_fee(lot["row"], float(lot.get("notional") or 0))
            fee_src = "broker" if _row_broker_fee_dollars(lot["row"]) is not None else "estimate"
            stop_pct = DEFAULT_BAR_STOP_PCT
            try:
                from scoring import get_stop_distance_pct
                stop_pct = abs(float(get_stop_distance_pct(
                    lot["row"].get("broker") or broker,
                    ticker=ticker,
                    asset_type=lot["row"].get("asset_type") or "",
                ) or DEFAULT_BAR_STOP_PCT))
            except Exception:
                stop_pct = DEFAULT_BAR_STOP_PCT
            candidates.append({
                "broker": broker,
                "ticker": ticker,
                "entry_ts": lot["ts"],
                "exit_ts": None,
                "qty": float(lot["qty"]),
                "fee_est": fee,
                "fee_source": fee_src,
                "stop_pct": stop_pct,
                "is_crypto": bool(lot.get("is_crypto")),
                "source": "open_lot",
            })
    candidates.sort(key=lambda c: c.get("entry_ts") or datetime.min)
    return candidates


def bar_walk_forward_replay(
    fill_rows: list[dict],
    *,
    n_folds: int = 3,
    stop_pct: float = DEFAULT_BAR_STOP_PCT,
    bar_fetcher=None,
    period: str = "60d",
    interval: str = "1d",
    decision_rows: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Walk-forward using historical OHLCV bars (not journal fill prices).

    Multi-symbol OHLCV from journal tickers; fee-aware (broker fee_paid preferred);
    optional posture capacity compare from decision skips.
    """
    assumptions = [
        "Entry = next bar open at/after journal BUY timestamp (not fill price).",
        f"Exit = hard-stop (−{stop_pct*100:.1f}% of entry) or journal SELL bar close or last bar.",
        "Daily (or interval) Yahoo/yfinance bars — gaps, splits, and halts are not modeled.",
        "Fees = broker fee_paid×2 when present, else fee_est×2 / profile RT (fee_source stamped).",
        "Multi-symbol: each ticker fetched independently; no portfolio heat / cash coupling.",
        "Equity session days only when market_calendar filters bars (weekends/holidays skipped).",
        "Not QuantConnect: no partial fills, no shorting, no borrow.",
    ]
    n_folds = max(2, min(8, int(n_folds or 3)))
    fetcher = bar_fetcher or fetch_ohlcv_bars
    candidates = build_bar_candidates_from_journal(fill_rows)
    empty = {
        "mode": "bar_ohlcv",
        "trades": [],
        "folds": [],
        "walk_forward": [],
        "n_candidates": len(candidates),
        "n_trades": 0,
        "n_folds": n_folds,
        "oos_steps": 0,
        "oos_net_sum": None,
        "assumptions": assumptions,
        "note": "Need journal BUY fills + downloadable bars for bar walk-forward.",
        "missing_bars": [],
        "symbols": [],
        "by_symbol": {},
        "posture_compare": None,
    }
    if len(candidates) < 1:
        return empty

    bar_cache: dict[str, list] = {}
    missing: list[str] = []
    trades: list[dict] = []
    broker_fee_n = 0
    for cand in candidates:
        ticker = cand["ticker"]
        if ticker not in bar_cache:
            try:
                bar_cache[ticker] = list(fetcher(
                    ticker,
                    period=period,
                    interval=interval,
                    is_crypto=bool(cand.get("is_crypto")),
                ) or [])
            except Exception:
                bar_cache[ticker] = []
            if not bar_cache[ticker]:
                missing.append(ticker)
        sim = simulate_bar_round_trip(
            bar_cache[ticker],
            cand["entry_ts"],
            cand["qty"],
            exit_ts=cand.get("exit_ts"),
            stop_pct=float(cand.get("stop_pct") or stop_pct),
            fee_dollars=float(cand.get("fee_est") or 0),
        )
        if sim is None:
            continue
        sim["ticker"] = ticker
        sim["broker"] = cand.get("broker")
        sim["source"] = cand.get("source")
        sim["fee_source"] = cand.get("fee_source") or "estimate"
        if cand.get("fee_source") == "broker":
            broker_fee_n += 1
        trades.append(sim)

    symbols = sorted({str(t.get("ticker") or "") for t in trades if t.get("ticker")})
    by_symbol: dict[str, dict] = {}
    for sym in symbols:
        chunk = [t for t in trades if t.get("ticker") == sym]
        realized = sum(float(t.get("realized_pnl") or 0) for t in chunk)
        fees = sum(float(t.get("fee_est") or 0) for t in chunk)
        wins = sum(1 for t in chunk if t.get("win"))
        by_symbol[sym] = {
            "n_trades": len(chunk),
            "realized_pnl": realized,
            "fee_est": fees,
            "net_after_fees": realized - fees,
            "wins": wins,
            "losses": len(chunk) - wins,
        }

    posture_compare = None
    if decision_rows is not None:
        try:
            posture_compare = lite_posture_decision_replay(decision_rows)
        except Exception:
            posture_compare = None

    empty["missing_bars"] = sorted(set(missing))
    empty["n_trades"] = len(trades)
    empty["symbols"] = symbols
    empty["by_symbol"] = by_symbol
    empty["posture_compare"] = posture_compare
    empty["broker_fee_trades"] = broker_fee_n
    if len(trades) < 2:
        empty["trades"] = trades
        empty["note"] = (
            f"{len(trades)} bar trade(s) · {len(symbols)} symbol(s) from {len(candidates)} candidates"
            + (f" · missing bars: {', '.join(empty['missing_bars'][:6])}" if missing else "")
            + " — need ≥2 for folds."
        )
        if trades:
            empty["overall"] = {
                "realized_pnl": sum(t["realized_pnl"] for t in trades),
                "fee_est": sum(t["fee_est"] for t in trades),
                "net_after_fees": sum(t["net_after_fees"] for t in trades),
                "wins": sum(1 for t in trades if t.get("win")),
                "losses": sum(1 for t in trades if not t.get("win")),
            }
        return empty

    fold_size = max(1, len(trades) // n_folds)
    folds_rows: list[list[dict]] = []
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else len(trades)
        if start >= len(trades):
            break
        chunk = trades[start:end]
        if chunk:
            folds_rows.append(chunk)
    if len(folds_rows) < 2:
        empty["trades"] = trades
        empty["note"] = "Not enough trades to span 2 folds."
        return empty

    def _sum_fold(chunk: list[dict]) -> dict:
        realized = sum(float(t.get("realized_pnl") or 0) for t in chunk)
        fees = sum(float(t.get("fee_est") or 0) for t in chunk)
        wins = sum(1 for t in chunk if t.get("win"))
        losses = len(chunk) - wins
        return {
            "n_trades": len(chunk),
            "realized_pnl": realized,
            "fee_est": fees,
            "net_after_fees": realized - fees,
            "win_rate": (wins / len(chunk)) if chunk else None,
            "wins": wins,
            "losses": losses,
        }

    fold_summaries = []
    for i, chunk in enumerate(folds_rows):
        s = _sum_fold(chunk)
        fold_summaries.append({
            "fold": i,
            "from": chunk[0]["entry_ts"].isoformat(timespec="seconds") if chunk else None,
            "to": chunk[-1]["exit_ts"].isoformat(timespec="seconds") if chunk else None,
            **s,
        })

    walk = []
    for k in range(1, len(folds_rows)):
        is_rows: list[dict] = []
        for j in range(k):
            is_rows.extend(folds_rows[j])
        oos_rows = folds_rows[k]
        walk.append({
            "step": k,
            "in_sample_folds": list(range(k)),
            "oos_fold": k,
            "in_sample": _sum_fold(is_rows),
            "out_of_sample": _sum_fold(oos_rows),
        })
    oos_nets = [w["out_of_sample"]["net_after_fees"] for w in walk]
    overall = _sum_fold(trades)
    sym_note = f"{len(symbols)} symbols" if symbols else "0 symbols"
    return {
        "mode": "bar_ohlcv",
        "trades": trades,
        "folds": fold_summaries,
        "walk_forward": walk,
        "oos_net_sum": sum(oos_nets),
        "oos_steps": len(walk),
        "overall": overall,
        "n_candidates": len(candidates),
        "n_trades": len(trades),
        "n_folds": len(folds_rows),
        "assumptions": assumptions,
        "missing_bars": sorted(set(missing)),
        "symbols": symbols,
        "by_symbol": by_symbol,
        "posture_compare": posture_compare,
        "broker_fee_trades": broker_fee_n,
        "note": (
            f"{len(folds_rows)} time folds · {len(walk)} OOS steps · "
            f"{len(trades)} bar trades · {sym_note} · "
            "fee-aware OHLCV (not journal fill prices)."
        ),
    }


def evaluate_shadow_guardrail(
    fill_rows: list[dict],
    *,
    adverse_rate_threshold: float = 0.55,
    delta_net_threshold: float = -25.0,
    min_samples: int = 4,
) -> dict[str, Any]:
    """
    Light paper↔live / fill-quality guardrail for live sizing.

    Triggers when recent adverse fill rate is high, or when both paper+live
    exist and live net lags paper by more than ``delta_net_threshold``.
    """
    fq = summarize_fill_quality(fill_rows)
    shadow = compare_paper_live(fill_rows)
    adverse_rate = fq.get("adverse_rate")
    samples = int(fq.get("samples") or 0)
    reasons: list[str] = []
    tighten = False
    size_mult = 1.0
    offset_bump = 0.0

    if samples >= min_samples and adverse_rate is not None and adverse_rate >= adverse_rate_threshold:
        tighten = True
        reasons.append(f"adverse fill rate {adverse_rate*100:.0f}% ≥ {adverse_rate_threshold*100:.0f}%")
        size_mult = min(size_mult, 0.85)
        offset_bump = max(offset_bump, 0.05)

    delta = shadow.get("delta_live_minus_paper_net")
    if shadow.get("both_modes") and delta is not None and float(delta) <= float(delta_net_threshold):
        tighten = True
        reasons.append(
            f"live net lags paper by {float(delta):.2f} (threshold {delta_net_threshold})"
        )
        size_mult = min(size_mult, 0.80)
        offset_bump = max(offset_bump, 0.08)

    status = "tighten" if tighten else ("ok" if samples >= min_samples or shadow.get("both_modes") else "insufficient")
    tip = ""
    if tighten:
        tip = (
            "Shadow guardrail: " + "; ".join(reasons)
            + f" → size×{size_mult:.2f}, offset +{offset_bump:.2f}% (temporary)."
        )
    elif status == "ok":
        tip = "Shadow guardrail: paper/live fill quality within thresholds."
    else:
        tip = "Shadow guardrail: need more fills with slippage meta (or both paper+live) to judge."

    return {
        "status": status,
        "tighten": tighten,
        "size_mult": size_mult,
        "offset_bump_pct": offset_bump,
        "reasons": reasons,
        "tip": tip,
        "fill_quality": fq,
        "shadow": shadow,
        "adverse_rate_threshold": adverse_rate_threshold,
        "delta_net_threshold": delta_net_threshold,
    }


def apply_fractional_share_policy(
    trade_dollars: float,
    price: float,
    *,
    prefer_whole_shares: bool = True,
    allow_fractional_ttp_only: bool = True,
    min_dollars: float = 5.0,
) -> dict[str, Any]:
    """
    First-class fractional equity policy for RH (broker stops need whole shares).

    When prefer_whole_shares and afford ≥1 share: round down to whole shares.
    When cannot afford 1 share: keep fractional only if allow_fractional_ttp_only
    (TTP-only / stop N/A), else skip.
    """
    out = {
        "trade_dollars": 0.0,
        "qty": 0.0,
        "whole_shares": False,
        "policy": "skip",
        "note": "",
    }
    dollars = float(trade_dollars or 0)
    px = float(price or 0)
    if dollars <= 0 or px <= 0:
        out["note"] = "invalid dollars/price"
        return out
    raw_qty = dollars / px
    if prefer_whole_shares and raw_qty >= 1.0:
        whole = int(raw_qty)
        out["qty"] = float(whole)
        out["trade_dollars"] = round(whole * px, 2)
        out["whole_shares"] = True
        out["policy"] = "whole_shares"
        out["note"] = f"prefer_whole_shares → {whole} sh (stops eligible)"
        return out
    if raw_qty >= 1.0 and not prefer_whole_shares:
        out["qty"] = raw_qty
        out["trade_dollars"] = round(dollars, 2)
        out["whole_shares"] = abs(raw_qty - round(raw_qty)) < 1e-9
        out["policy"] = "fractional_ok"
        out["note"] = "prefer_whole_shares off — fractional allowed"
        return out
    # Sub-1 share
    if allow_fractional_ttp_only and dollars + 1e-9 >= float(min_dollars or 0):
        out["qty"] = raw_qty
        out["trade_dollars"] = round(dollars, 2)
        out["whole_shares"] = False
        out["policy"] = "fractional_ttp_only"
        out["note"] = "sub-1 share — broker stop N/A, TTP only"
        return out
    out["policy"] = "skip"
    out["note"] = (
        "sub-1 share blocked (prefer whole shares; enable TTP-only fractionals in Settings to allow)"
        if prefer_whole_shares
        else "below min / fractional blocked"
    )
    return out
