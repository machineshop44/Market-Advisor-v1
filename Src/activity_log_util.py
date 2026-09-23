"""
Bounded activity-log helpers (disk rotate / tail / archive).

Kept out of gui.py so ops regressions can lock the cap without importing Qt.

Active file stays bounded for UI/perf; older lines are archived indefinitely
(never deleted by rotate). Clear Log still wipes the active file only.
"""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime

# UI buffer — Journal → Activity view
ACTIVITY_LOG_UI_MAX_LINES = 2000
# Active on-disk file (current session working set)
ACTIVITY_LOG_DISK_MAX_BYTES = 20 * 1024 * 1024  # ~20 MB before rotate
ACTIVITY_LOG_DISK_MAX_LINES = 200_000
ACTIVITY_LOG_DISK_KEEP_LINES = 80_000  # ~days of dense AUTO cycles
ACTIVITY_LOG_DISK_TAIL_LINES = 5000


def activity_log_archive_dir(path):
    """Sibling folder next to the active log file."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    return os.path.join(parent, "activity_log_archives")


def tail_activity_log_file(path, max_lines=ACTIVITY_LOG_DISK_TAIL_LINES):
    """Return the last max_lines from the activity log without loading the whole file."""
    try:
        if not os.path.isfile(path) or max_lines <= 0:
            return ""
        size = os.path.getsize(path)
        if size <= 0:
            return ""
        read_size = min(size, max_lines * 120 + 8192)
        with open(path, "rb") as f:
            f.seek(max(0, size - read_size))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if size > read_size and lines:
            lines = lines[1:]
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _archive_head_lines(path, head_lines):
    """
    Persist lines that are about to fall off the active file.
    Returns archive path or None. Never deletes prior archives.
    """
    if not head_lines:
        return None
    try:
        arch_dir = activity_log_archive_dir(path)
        os.makedirs(arch_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.splitext(os.path.basename(path))[0] or "activity_log"
        dest = os.path.join(arch_dir, f"{base}-{stamp}.txt")
        # Avoid clobber if two rotates in the same second
        if os.path.exists(dest):
            dest = os.path.join(arch_dir, f"{base}-{stamp}-{os.getpid()}.txt")
        with open(dest, "w", encoding="utf-8") as f:
            f.writelines(head_lines)
        return dest
    except Exception:
        return None


def rotate_activity_log_if_needed(
    path,
    *,
    force=False,
    max_bytes=ACTIVITY_LOG_DISK_MAX_BYTES,
    max_lines=ACTIVITY_LOG_DISK_MAX_LINES,
    keep_lines=ACTIVITY_LOG_DISK_KEEP_LINES,
    archive=True,
):
    """
    Rewrite path keeping only the last keep_lines when over size/line limits.
    Older lines are written to activity_log_archives/ (indefinite retention).
    Returns True if a rewrite happened.
    """
    try:
        if not os.path.isfile(path):
            return False
        size = os.path.getsize(path)
        if not force and size < max_bytes and size < max_lines * 40:
            return False
        keep = deque(maxlen=keep_lines)
        head = []
        total = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                normalized = line if line.endswith("\n") else line + "\n"
                total += 1
                if len(keep) == keep.maxlen:
                    # Line about to drop from the ring — archive candidate
                    dropped = keep[0]
                    head.append(dropped)
                keep.append(normalized)
        if not force and total <= max_lines and size < max_bytes:
            return False
        if total <= keep_lines:
            return False
        if archive and head:
            _archive_head_lines(path, head)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep)
        return True
    except Exception:
        return False


def explain_no_buys_after_rank(
    notes,
    execute_skips,
    *,
    buys_done,
    orig_n,
    ranked_n,
    broker_name,
):
    """
    Append a trail line when ranked candidates produced zero buys and notes
    lack an outcome bit. Returns the explanation string or None if not needed.
    """
    if buys_done != 0 or orig_n <= 0:
        return None
    outcome_bits = (
        "Regime blocked", "Skipping buys", "SCALE-IN skipped", "Max open",
        "Skipped [", "Deferring buy", "Buy cap", "Frac policy",
        "No buys executed", "Sandbox/no BP", "buy engines idle",
        "concentration", "trade-locked", "[ROTATE]",
    )
    has_outcome = any(
        any(bit in str(n) for bit in outcome_bits) for n in (notes or [])
    )
    if has_outcome:
        return None
    uniq = []
    for r in execute_skips or []:
        if r and r not in uniq:
            uniq.append(r)
    why = "; ".join(uniq[:3]) if uniq else "policy / size / empty after filter"
    line = (
        f"[{broker_name}] No buys executed after rank "
        f"({ranked_n}/{orig_n} candidate(s)) — {why}"
    )
    notes.append(line)
    return line


def sell_fail_ttl_for_status(status, *, default_ttl=1800) -> int:
    """Backoff length by fail class — session-stale should not park a sell for 30m."""
    low = str(status or "").lower()
    if "empty response" in low or "returned empty" in low:
        return max(int(default_ttl or 1800), 7200)  # ≥2h RH crypto empty
    # Ghost / sync lag positions: hammering every 30m burns log + API.
    if "insufficient_fund" in low or "insufficient balance" in low:
        return max(int(default_ttl or 1800), 6 * 3600)  # ≥6h
    # Overnight / no-quote / soft-dead — wait for a real session/API recovery.
    if any(
        k in low
        for k in (
            "overnight",
            "late session",
            "fractional equity sell",
            "blocks fractional",
            "no rh crypto quote",
            "soft-dead",
            "api gap",
        )
    ):
        return max(int(default_ttl or 1800), 2 * 3600)  # ≥2h
    # Premarket→RTH flag race: was 90s and still thrashed every portfolio pulse.
    if "hours mismatch" in low or "market hours mismatch" in low:
        return max(int(default_ttl or 1800), 30 * 60)  # ≥30m
    return int(default_ttl or 1800)


def sell_is_ghost_insufficient(status) -> bool:
    """True when broker says we cannot sell what the book thinks we hold."""
    low = str(status or "").lower()
    return "insufficient_fund" in low or "insufficient balance" in low


def advisor_miss_park_spec(why: str) -> tuple[float, str] | None:
    """
    Hard execute-miss classes that must not bounce pending→re-apply.
    Returns (cooldown_sec, reason_tag) or None to restore pending.
    """
    low = str(why or "").lower()
    if not low:
        return None
    if "consecutive-loss" in low:
        return (45.0 * 60.0, "consecutive_loss")
    if "empty response" in low or "returned empty" in low:
        return (2.0 * 3600.0, "empty_response")
    if "insufficient_fund" in low or "insufficient balance" in low or "insufficient fund" in low:
        return (2.0 * 3600.0, "insufficient_fund")
    if "hours mismatch" in low or "market hours mismatch" in low:
        # Align with buy_fail_ttl (≥30m) so Advisor doesn't re-propose into backoff.
        return (30.0 * 60.0, "hours_mismatch")
    if "stuck overnight" in low or "would be stuck overnight" in low or "session exit risk" in low:
        # RH fractional overnight — park until REGULAR / fractional_ok.
        return (3.0 * 3600.0, "overnight_frac")
    if "limit unfilled" in low or (
        "cancelled" in low and ("unfilled" in low or "queued" in low)
    ):
        # Place→cancel→re-approve thrash (RH EXT queued) — stop email spam.
        return (20.0 * 60.0, "limit_unfilled")
    if "invalid product_id" in low or "product_id" in low:
        # Advisor buy routed to wrong broker mid-cycle.
        return (30.0 * 60.0, "broker_route")
    if "left working" in low or "pending fill" in low:
        # Working order already reserved BP — do not re-fire.
        return (15.0 * 60.0, "left_working")
    if any(
        bit in low
        for bit in (
            "below rh crypto",
            "below cb min",
            "dust",
            "too small",
            "rejected small",
            "invalid crypto size",
            "ticket size too small",
        )
    ):
        return (60.0 * 60.0, "size_floor")
    if "buying power" in low or "insufficient sandbox" in low or "insufficient cash" in low:
        return (30.0 * 60.0, "no_bp")
    if "daily rotate cap" in low or "rotate cap" in low:
        return (30.0 * 60.0, "rotate_cap")
    return None


def sell_fail_should_skip(store, broker, ticker, *, now=None, ttl_sec=1800):
    """True when this ticker already failed loudly and reason unchanged within TTL."""
    import time
    store = store if isinstance(store, dict) else {}
    key = (str(broker), str(ticker).upper())
    entry = store.get(key)
    if not entry:
        return False
    ts_now = float(now if now is not None else time.time())
    age = ts_now - float(entry.get("ts") or 0)
    entry_ttl = float(entry.get("ttl_sec") or ttl_sec or 1800)
    if age >= entry_ttl:
        store.pop(key, None)
        return False
    return True


def record_sell_fail_backoff(store, broker, ticker, status, *, now=None, ttl_sec=1800):
    """
    First failure for (broker,ticker,reason) → record + return (False, note).
    Duplicate within TTL → return (True, None) meaning caller should skip logging.
    """
    import time
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = (str(broker), str(ticker).upper())
    reason = str(status or "Fail")[:180]
    use_ttl = sell_fail_ttl_for_status(reason, default_ttl=ttl_sec)
    prev = store.get(key)
    if prev and prev.get("reason") != reason:
        store.pop(key, None)
        prev = None
    if prev and prev.get("reason") == reason:
        return True, None
    ts_now = float(now if now is not None else time.time())
    store[key] = {"reason": reason, "ts": ts_now, "ttl_sec": int(use_ttl)}
    note = (
        f"[{broker}] Sell FAIL [{ticker}]: {reason} — backing off retries "
        f"(~{int(use_ttl) // 60}m TTL or until reason changes)"
    )
    return False, note


def buy_fail_ttl_for_status(status, *, default_ttl=900) -> int:
    low = str(status or "").lower()
    if "hours mismatch" in low or "market hours mismatch" in low:
        return max(int(default_ttl or 900), 30 * 60)
    if "empty response" in low or "returned empty" in low:
        return max(int(default_ttl or 900), 2 * 3600)
    return int(default_ttl or 900)


def buy_fail_should_skip(store, broker, ticker, *, now=None, ttl_sec=900):
    """True when this buy ticker already hit a transient API error within TTL."""
    import time
    store = store if isinstance(store, dict) else {}
    key = (str(broker), str(ticker).upper())
    entry = store.get(key)
    if not entry:
        return False
    ts_now = float(now if now is not None else time.time())
    age = ts_now - float(entry.get("ts") or 0)
    entry_ttl = float(entry.get("ttl_sec") or ttl_sec or 900)
    if age >= entry_ttl:
        store.pop(key, None)
        return False
    return True


def record_buy_fail_backoff(store, broker, ticker, status, *, now=None, ttl_sec=900):
    """Record a transient buy failure; return (already_recorded, note_or_none)."""
    import time
    if not isinstance(store, dict):
        raise TypeError("store must be a dict")
    key = (str(broker), str(ticker).upper())
    reason = str(status or "Fail")[:180]
    use_ttl = buy_fail_ttl_for_status(reason, default_ttl=ttl_sec)
    prev = store.get(key)
    if prev and prev.get("reason") != reason:
        store.pop(key, None)
        prev = None
    if prev and prev.get("reason") == reason:
        return True, None
    ts_now = float(now if now is not None else time.time())
    store[key] = {"reason": reason, "ts": ts_now, "ttl_sec": int(use_ttl)}
    note = (
        f"[{broker}] Buy FAIL [{ticker}]: {reason} — backing off retries "
        f"(~{int(use_ttl) // 60}m TTL or until reason changes)"
    )
    return False, note
