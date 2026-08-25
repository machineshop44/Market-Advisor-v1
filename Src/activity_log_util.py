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
    if age >= float(ttl_sec or 1800):
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
    prev = store.get(key)
    if prev and prev.get("reason") != reason:
        store.pop(key, None)
        prev = None
    if prev and prev.get("reason") == reason:
        return True, None
    ts_now = float(now if now is not None else time.time())
    store[key] = {"reason": reason, "ts": ts_now}
    note = (
        f"[{broker}] Sell FAIL [{ticker}]: {reason} — backing off retries "
        f"(~{int(ttl_sec or 1800) // 60}m TTL or until reason changes)"
    )
    return False, note
