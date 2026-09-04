"""
Guard against flaky broker equity reads that look like total account wipes.

Classic failure modes:
  1) Robinhood returns $0 portfolio_equity (session glitch) twice
     → Day P&L ≈ −baseline → false MAX DAILY LOSS halt.
  2) Moderate under-read (e.g. $88.81 → $78.23) slips under the near-zero /
     large-collapse guards but still trips a tight daily loss limit (−$8).
"""
from __future__ import annotations

# Near-zero equity when we recently had real money is never trusted as a wipe.
NEAR_ZERO_EQUITY = 0.50
MIN_PRIOR_EQUITY = 5.0

# Non-zero but sudden drops (e.g. $100 → $30) need several agreeing reads.
LARGE_COLLAPSE_CONFIRM_READS = 4
LARGE_COLLAPSE_FRAC = 0.35
LARGE_COLLAPSE_MIN_DROP = 15.0

# Single-read drops that would newly trip max daily loss (small-account case).
# Aug 4 2026: RH $88.81 (+$1.15) → ~$78.23 (−$9.43) with −$8 limit — no $0 wipe.
DAY_LOSS_TRIP_CONFIRM_READS = 3

# Sep 1 2026: RH baseline $100.49 after BTC buy → equity under-read $81.85 (−$18.64)
# looked like MAX DAILY LOSS. Drop ≈ recent buy notional must never confirm as loss.
BUY_LAG_FEE_CUSHION = 2.50
BUY_LAG_FRAC_LO = 0.70
BUY_LAG_FRAC_HI = 1.35


def is_near_zero_equity(value) -> bool:
    try:
        return float(value or 0.0) <= NEAR_ZERO_EQUITY
    except (TypeError, ValueError):
        return True


def reference_equity(old_p, baseline, last_trusted=None) -> float:
    """Best non-glitch reference for collapse checks."""
    vals = []
    for v in (last_trusted, old_p, baseline):
        try:
            fv = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            fv = 0.0
        if fv > 0:
            vals.append(fv)
    return max(vals) if vals else 0.0


def _safe_float(value, default=0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def equity_drop_matches_recent_buy(
    new_p,
    ref_p,
    recent_buy_notional: float,
    *,
    fee_cushion: float = BUY_LAG_FEE_CUSHION,
) -> bool:
    """
    True when equity drop vs ref looks like cash→position lag after a buy
    (not a realized mark-to-market wipe).
    """
    buy = _safe_float(recent_buy_notional, 0.0)
    if buy < 3.0:
        return False
    ref = _safe_float(ref_p, 0.0)
    new = _safe_float(new_p, 0.0)
    if ref < MIN_PRIOR_EQUITY or new <= 0:
        return False
    drop = ref - new
    if drop < 3.0:
        return False
    lo = max(3.0, buy * BUY_LAG_FRAC_LO - fee_cushion)
    hi = buy * BUY_LAG_FRAC_HI + fee_cushion
    return lo <= drop <= hi


def day_pnl_looks_like_cash_to_holdings_baseline(
    equity: float,
    buying_power: float,
    holdings_value: float,
    day_pnl: float,
    *,
    min_loss: float = 5.0,
    book_slop: float = 2.5,
    match_slop: float = 2.5,
) -> bool:
    """
    True when Day P&L ≈ −holdings mark and equity ≈ BP + holdings.

    Classic false Day P&L after a buy: baseline locked on a cash-heavy print,
    then equity settles at cash+coin — looks like a −$N loss with no sell.
    """
    eq = _safe_float(equity, 0.0)
    bp = _safe_float(buying_power, 0.0)
    hv = max(0.0, _safe_float(holdings_value, 0.0))
    pl = _safe_float(day_pnl, 0.0)
    if pl > -min_loss or hv < min_loss or eq < MIN_PRIOR_EQUITY:
        return False
    if abs(eq - (bp + hv)) > max(book_slop, eq * 0.05):
        return False
    loss = abs(pl)
    return abs(loss - hv) <= max(match_slop, hv * 0.25)


def buying_power_looks_unreliable(
    equity: float,
    buying_power: float,
    *,
    holdings_value: float = 0.0,
    prior_bp: float = 0.0,
    min_implied_cash: float = 5.0,
) -> bool:
    """
    True when equity implies deployable cash but BP printed ~$0.

    Common RH glitch: stock buying_power=0 while cash still sits in the account
    (or prior poll had real BP and this read wiped it).
    """
    eq = _safe_float(equity, 0.0)
    bp = _safe_float(buying_power, 0.0)
    hv = max(0.0, _safe_float(holdings_value, 0.0))
    prior = _safe_float(prior_bp, 0.0)
    if eq < MIN_PRIOR_EQUITY:
        return False
    if bp >= min_implied_cash:
        return False
    implied_cash = max(0.0, eq - hv)
    if implied_cash >= min_implied_cash and bp < 1.0:
        return True
    if prior >= min_implied_cash and bp < 1.0 and implied_cash >= min_implied_cash * 0.5:
        return True
    return False


def repair_buying_power(
    equity: float,
    buying_power: float,
    *,
    holdings_value: float = 0.0,
    prior_bp: float = 0.0,
) -> float:
    """Replace a ghost $0 BP with prior BP or equity−holdings implied cash."""
    bp = _safe_float(buying_power, 0.0)
    if not buying_power_looks_unreliable(
        equity, bp, holdings_value=holdings_value, prior_bp=prior_bp
    ):
        return bp
    prior = _safe_float(prior_bp, 0.0)
    # Prefer last good BP — implied cash can inflate when holdings snapshot is empty/stale
    if prior >= 5.0:
        return max(bp, prior)
    eq = _safe_float(equity, 0.0)
    hv = max(0.0, _safe_float(holdings_value, 0.0))
    implied = max(0.0, eq - hv)
    return max(bp, implied)


def holdings_equity_gap(
    equity: float,
    buying_power: float,
    holdings_value: float,
    *,
    min_gap: float = 5.0,
    min_frac: float = 0.05,
) -> tuple[bool, float]:
    """
    True when equity − BP implies deployed capital that holdings mark does not cover.
    Returns (mismatch, ghost_dollars).
    """
    eq = _safe_float(equity, 0.0)
    bp = _safe_float(buying_power, 0.0)
    hv = max(0.0, _safe_float(holdings_value, 0.0))
    if eq < MIN_PRIOR_EQUITY:
        return False, 0.0
    implied = max(0.0, eq - bp)
    gap = implied - hv
    thresh = max(min_gap, eq * min_frac)
    if gap >= thresh:
        return True, gap
    return False, 0.0


def day_loss_trip_is_suspicious(new_p, old_p, baseline, last_trusted=None, loss_limit=0.0) -> bool:
    """
    True when one equity print would newly cross the max daily loss limit vs a
    healthier recent print — classic false halt on a transient under-read.

    Uses last trusted / prior painted equity as the recent reference (not the
    day baseline). Comparing against baseline would treat every first crossing
    of the limit as a sudden −limit$ drop.
    """
    limit = _safe_float(loss_limit, 0.0)
    if limit <= 0:
        return False
    new_p = _safe_float(new_p, 0.0)
    baseline_f = _safe_float(baseline, 0.0)
    if baseline_f <= 0:
        return False
    if is_near_zero_equity(new_p):
        # Near-zero path has its own never-accept rules.
        return False

    # Prefer last trusted, then prior painted — never day baseline as "recent".
    ref = 0.0
    for v in (last_trusted, old_p):
        fv = _safe_float(v, 0.0)
        if fv > NEAR_ZERO_EQUITY:
            ref = fv
            break
    if ref < MIN_PRIOR_EQUITY:
        return False

    new_pl = new_p - baseline_f
    ref_pl = ref - baseline_f
    if new_pl > -limit:
        return False
    if ref_pl <= -limit:
        # Already past the limit on the last good book — allow halt without delay.
        return False

    drop = ref - new_p
    # Require a meaningful single-read drop (not a $0.05 tick across the line).
    min_drop = max(limit * 0.5, 4.0)
    return drop >= min_drop


def balance_reading_is_suspicious(new_p, old_p, baseline, last_trusted=None, loss_limit=0.0) -> bool:
    """
    True when a new equity print looks like a failed API read, not a real wipe.
    """
    try:
        new_p = float(new_p or 0.0)
    except (TypeError, ValueError):
        return True
    try:
        old_p = float(old_p or 0.0)
    except (TypeError, ValueError):
        old_p = 0.0
    try:
        baseline = float(baseline or 0.0) if baseline else 0.0
    except (TypeError, ValueError):
        baseline = 0.0
    ref = reference_equity(old_p, baseline, last_trusted)

    if is_near_zero_equity(new_p) and ref >= MIN_PRIOR_EQUITY:
        return True
    if is_near_zero_equity(new_p) and old_p >= MIN_PRIOR_EQUITY:
        return True
    if is_near_zero_equity(new_p) and baseline >= MIN_PRIOR_EQUITY:
        return True

    if old_p >= 20.0 and new_p < old_p * LARGE_COLLAPSE_FRAC and (old_p - new_p) >= LARGE_COLLAPSE_MIN_DROP:
        return True
    if (
        last_trusted is not None
        and float(last_trusted) >= 20.0
        and new_p < float(last_trusted) * LARGE_COLLAPSE_FRAC
        and (float(last_trusted) - new_p) >= LARGE_COLLAPSE_MIN_DROP
    ):
        return True

    if day_loss_trip_is_suspicious(new_p, old_p, baseline, last_trusted, loss_limit):
        return True

    return False


def is_near_zero_wipe(new_p, old_p, baseline, last_trusted=None) -> bool:
    """Total/near-total wipe vs a substantial reference equity."""
    try:
        new_p = float(new_p or 0.0)
    except (TypeError, ValueError):
        new_p = 0.0
    ref = reference_equity(old_p, baseline, last_trusted)
    return is_near_zero_equity(new_p) and ref >= MIN_PRIOR_EQUITY


def decide_suspicious_equity(
    new_p,
    old_p,
    baseline,
    *,
    last_trusted=None,
    holdings_count=0,
    bad_streak=0,
    collapse_confirm_reads=LARGE_COLLAPSE_CONFIRM_READS,
    loss_limit=0.0,
    recent_buy_notional=0.0,
):
    """
    Decide whether to accept a suspicious equity reading.

    Returns dict:
      action: 'keep' | 'accept'
      trusted: bool  (False ⇒ never trip day-loss / profit limits on this read)
      reason: short log fragment
      streak: updated bad-streak counter
    """
    try:
        holdings_count = int(holdings_count or 0)
    except (TypeError, ValueError):
        holdings_count = 0
    try:
        streak = int(bad_streak or 0) + 1
    except (TypeError, ValueError):
        streak = 1

    try:
        new_p = float(new_p or 0.0)
        old_p = float(old_p or 0.0)
    except (TypeError, ValueError):
        new_p, old_p = 0.0, float(old_p or 0.0) if old_p else 0.0

    ref = reference_equity(old_p, baseline, last_trusted)
    fmt_ref = ref
    buy_n = _safe_float(recent_buy_notional, 0.0)

    # Post-buy equity lag: cash left the book before position marks — never accept as $-loss.
    if equity_drop_matches_recent_buy(new_p, fmt_ref, buy_n):
        return {
            "action": "keep",
            "trusted": False,
            "reason": (
                f"Day P&L equity lag after BUY — not treating cash deploy as $-loss "
                f"(${fmt_ref:.2f} → ${new_p:.2f}; recent buy ~${buy_n:.2f})"
            ),
            "streak": 0,
        }

    day_loss_trip = day_loss_trip_is_suspicious(
        new_p, old_p, baseline, last_trusted, loss_limit
    )
    wipe_like = is_near_zero_wipe(new_p, old_p, baseline, last_trusted) or (
        not day_loss_trip
        and balance_reading_is_suspicious(new_p, old_p, baseline, last_trusted, loss_limit=0.0)
    )

    # Holdings still on book ⇒ near-zero / catastrophic wipe is an API lie.
    # Do NOT block moderate day-loss-trip confirms (real mark-to-market while holding).
    if holdings_count > 0 and wipe_like:
        return {
            "action": "keep",
            "trusted": False,
            "reason": (
                f"Ignoring suspicious equity ${new_p:.2f} "
                f"(ref ${fmt_ref:.2f}; {holdings_count} holding(s) still present)"
            ),
            "streak": streak,
        }

    # Near-zero vs substantial book: never accept as P&L truth (session/API glitch).
    # Real liquidations leave cash ≈ equity; $0 total with a prior baseline is not trusted.
    if is_near_zero_wipe(new_p, old_p, baseline, last_trusted):
        return {
            "action": "keep",
            "trusted": False,
            "reason": (
                f"Unreliable near-zero equity ${new_p:.2f} "
                f"(was ${old_p:.2f}; baseline ${float(baseline or 0):.2f}) — "
                f"keeping last good; not treating as realized wipe"
            ),
            "streak": streak,
        }

    # Moderate drop that newly trips max daily loss: confirm before halt.
    # Never confirm when drop still matches a recent buy (even after N reads).
    if day_loss_trip:
        if equity_drop_matches_recent_buy(new_p, fmt_ref, buy_n):
            return {
                "action": "keep",
                "trusted": False,
                "reason": (
                    f"Day P&L equity lag after BUY — not treating cash deploy as $-loss "
                    f"(${fmt_ref:.2f} → ${new_p:.2f}; recent buy ~${buy_n:.2f})"
                ),
                "streak": 0,
            }
        needed = max(2, int(DAY_LOSS_TRIP_CONFIRM_READS))
        if streak < needed:
            try:
                bl = float(baseline or 0.0)
            except (TypeError, ValueError):
                bl = 0.0
            implied_pl = new_p - bl if bl > 0 else 0.0
            return {
                "action": "keep",
                "trusted": False,
                "reason": (
                    f"Ignoring day-loss trip equity ${new_p:.2f} "
                    f"(ref ${fmt_ref:.2f}; implied P&L ${implied_pl:+.2f}) — "
                    f"need confirming read {streak}/{needed}"
                ),
                "streak": streak,
            }
        return {
            "action": "accept",
            "trusted": True,
            "reason": (
                f"Day-loss trip equity confirmed on read {streak}/{needed} "
                f"(${fmt_ref:.2f} → ${new_p:.2f}); accepting"
            ),
            "streak": 0,
        }

    # Large but non-zero collapse: require several agreeing reads.
    needed = max(2, int(collapse_confirm_reads or LARGE_COLLAPSE_CONFIRM_READS))
    if streak < needed:
        return {
            "action": "keep",
            "trusted": False,
            "reason": (
                f"Ignoring suspicious equity ${new_p:.2f} "
                f"(was ${old_p:.2f}; baseline ${float(baseline or 0):.2f}) — "
                f"need confirming read {streak}/{needed}"
            ),
            "streak": streak,
        }

    return {
        "action": "accept",
        "trusted": True,
        "reason": (
            f"Equity collapse confirmed on read {streak}/{needed} "
            f"(${old_p:.2f} → ${new_p:.2f}); accepting"
        ),
        "streak": 0,
    }
