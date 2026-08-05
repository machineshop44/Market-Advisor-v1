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
    if day_loss_trip:
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
