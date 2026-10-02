"""
Overnight-hold research for late-day E*TRADE entries.

Judges from daily history whether a name is a sane close→next-open hold:
gap behavior (drift + tail vs our stop), trend, strength into the close, and
earnings timing. Pure function — the GUI fetches bars and passes them in.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _sma(vals: Sequence[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    seg = vals[-n:]
    return sum(seg) / float(n)


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def assess_overnight_hold(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    price: Optional[float] = None,
    stop_pct: float = 0.04,
    earnings_next: Optional[bool] = None,
    min_days: int = 40,
    gap_window: int = 60,
) -> dict:
    """
    Last bar = today (partial during RTH). Returns
    {ok, score, reasons: [...], facts: {...}, summary: str}.
    """
    o = [float(x) for x in opens]
    h = [float(x) for x in highs]
    lo = [float(x) for x in lows]
    c = [float(x) for x in closes]
    n = min(len(o), len(h), len(lo), len(c))
    o, h, lo, c = o[-n:], h[-n:], lo[-n:], c[-n:]
    fails: list[str] = []
    goods: list[str] = []
    facts: dict = {}

    if earnings_next is True:
        fails.append("earnings before next open")
    elif earnings_next is None:
        fails.append("earnings date unknown")

    if n < max(25, int(min_days)):
        fails.append(f"only {n} daily bars (need {min_days})")
        return _result(False, 0, fails, goods, facts)

    px = float(price) if price and float(price) > 0 else c[-1]
    start = max(1, n - int(gap_window))
    gaps = [o[i] / c[i - 1] - 1.0 for i in range(start, n) if c[i - 1] > 0]
    sg = sorted(gaps)
    mean_gap = sum(gaps) / len(gaps) if gaps else 0.0
    pos_frac = sum(1 for g in gaps if g > 0) / len(gaps) if gaps else 0.0
    p10 = _pct(sg, 0.10)
    worst = sg[0] if sg else 0.0
    facts.update({
        "gap_mean_pct": round(mean_gap * 100, 3),
        "gap_up_frac": round(pos_frac, 2),
        "gap_p10_pct": round(p10 * 100, 2),
        "gap_worst_pct": round(worst * 100, 2),
        "stop_pct": round(stop_pct * 100, 2),
    })

    tail_cap = -abs(stop_pct) * 0.75
    if p10 < tail_cap:
        fails.append(
            f"1-in-10 gap-downs {p10*100:.1f}% exceed ~{abs(tail_cap)*100:.1f}% (stop {stop_pct*100:.1f}%)"
        )
    else:
        goods.append(f"gap-down tail {p10*100:.1f}% inside stop")
    if mean_gap < 0 and pos_frac < 0.5:
        fails.append(
            f"negative overnight drift ({mean_gap*100:+.2f}% avg, {pos_frac*100:.0f}% gap up)"
        )
    elif mean_gap >= 0:
        goods.append(f"overnight drift {mean_gap*100:+.2f}% avg, {pos_frac*100:.0f}% gap up")

    sma20 = _sma(c, 20)
    sma50 = _sma(c, 50)
    facts["sma20"] = round(sma20, 4) if sma20 else None
    facts["sma50"] = round(sma50, 4) if sma50 else None
    if sma20 and px < sma20:
        fails.append(f"below 20-day trend (${px:.2f} < ${sma20:.2f})")
    elif sma20:
        goods.append("above 20-day trend")
    if sma20 and sma50 and sma20 >= sma50:
        goods.append("20d over 50d")

    rng = h[-1] - lo[-1]
    loc = (px - lo[-1]) / rng if rng > 0 else 0.5
    facts["close_location"] = round(loc, 2)
    if loc < 0.5:
        fails.append(f"fading into the close (in lower {loc*100:.0f}% of day range)")
    else:
        goods.append(f"strong into close (top {100 - loc*100:.0f}% of range)")

    score = 50
    score += 15 if mean_gap > 0 else 0
    score += int(max(0.0, min(15.0, (pos_frac - 0.5) * 60)))
    score += 10 if (sma20 and sma50 and sma20 >= sma50) else 0
    score += int(max(0.0, min(10.0, (loc - 0.5) * 20)))
    score -= 15 * len(fails)
    score = max(0, min(100, score))
    return _result(not fails, score, fails, goods, facts)


def _result(ok: bool, score: int, fails: list, goods: list, facts: dict) -> dict:
    reasons = fails if not ok else goods
    summary = ("Overnight OK: " if ok else "Not an overnight hold: ") + "; ".join(reasons[:3])
    return {
        "ok": bool(ok),
        "score": int(score),
        "reasons": list(reasons),
        "fails": list(fails),
        "goods": list(goods),
        "facts": dict(facts),
        "summary": summary,
    }
