"""Persistent trade journal — append-only JSONL + recent-trade helpers."""
import os
import json
from datetime import datetime

JOURNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_journal.jsonl")

# (mtime, size, days_key, limit) -> rows
_since_cache: dict[tuple, list] = {}
_SINCE_CACHE_MAX = 8


def _read_jsonl_tail_lines(path, limit):
    """Last N non-empty lines without loading the whole file (recent-only reads)."""
    limit = max(int(limit or 1), 1)
    if not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return []
        take = min(size, max(16384, limit * 640))
        with open(path, "rb") as f:
            f.seek(max(0, size - take))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if size > take and lines:
            lines = lines[1:]
        return lines[-limit:]
    except Exception:
        return []


def _read_jsonl_tail_bytes(path, max_bytes):
    """Tail of file as non-empty text lines (may drop first partial line)."""
    if not os.path.exists(path):
        return [], 0
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return [], 0
        take = min(size, max(int(max_bytes or 0), 4096))
        with open(path, "rb") as f:
            f.seek(max(0, size - take))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if size > take and lines:
            lines = lines[1:]
        return lines, size
    except Exception:
        return [], 0


def _parse_ts(row):
    ts = str((row or {}).get("timestamp") or "")
    try:
        return datetime.fromisoformat(ts.replace("Z", ""))
    except Exception:
        return None


def log_trade(entry):
    """
    Append one trade event.
    Expected keys: timestamp, broker, side, ticker, price, qty/dollars, status,
    reason, fee_profile, paper (bool), order_id (optional), confirmed (optional)
    """
    row = dict(entry)
    row.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        _since_cache.clear()
    except Exception as e:
        print(f"Journal write error: {e}")
    return row


def read_recent(limit=20):
    """Return the most recent trade events (newest last)."""
    rows = []
    for ln in _read_jsonl_tail_lines(JOURNAL_FILE, limit):
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def read_since_days(days=7, limit=5000):
    """Return fills from the last N days (newest last).

    ``days=None`` (or < 0) = all time — no date cutoff; still capped by ``limit``.

    Prefer growing file-tail windows so Home / profit-guard / Reports do not
    scan multi-MB journals on the UI thread every refresh.
    """
    if not os.path.exists(JOURNAL_FILE):
        return []
    from datetime import timedelta

    all_time = days is None or int(days) < 0
    cutoff = None if all_time else datetime.now() - timedelta(days=max(0, int(days)))
    lim = max(int(limit or 1), 1)
    days_key = -1 if all_time else int(days)

    try:
        st = os.stat(JOURNAL_FILE)
        cache_key = (float(st.st_mtime), int(st.st_size), days_key, lim)
        hit = _since_cache.get(cache_key)
        if hit is not None:
            return list(hit)
    except Exception:
        cache_key = None

    try:
        file_size = os.path.getsize(JOURNAL_FILE)
    except Exception:
        file_size = 0

    # Grow tail until oldest kept row is before cutoff, or whole file scanned.
    # ~400 bytes/line average → start near 2× limit, expand ×2.
    window = min(file_size, max(65536, lim * 500))
    rows: list = []
    while True:
        lines, _ = _read_jsonl_tail_bytes(JOURNAL_FILE, window)
        parsed = []
        for ln in lines:
            try:
                row = json.loads(ln)
            except Exception:
                continue
            dt = _parse_ts(row)
            if dt is None:
                continue
            if cutoff is None or dt >= cutoff:
                parsed.append(row)
        rows = parsed[-lim:]
        covered_all = window >= file_size
        if covered_all or cutoff is None:
            break
        # If the oldest line in this window is still within the window, we may
        # have missed older in-range rows — expand. If oldest is before cutoff,
        # the tail already covers the date range.
        oldest = None
        for ln in lines[:40]:
            try:
                dt = _parse_ts(json.loads(ln))
            except Exception:
                continue
            if dt is not None:
                oldest = dt
                break
        if oldest is not None and oldest < cutoff:
            break
        if window >= file_size:
            break
        window = min(file_size, window * 2)
        if window >= file_size:
            # Final full-file path via line list for correctness on huge "all" gaps
            try:
                with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
                    all_lines = [ln.strip() for ln in f if ln.strip()]
                parsed = []
                for ln in all_lines[-lim:]:
                    try:
                        row = json.loads(ln)
                    except Exception:
                        continue
                    dt = _parse_ts(row)
                    if dt is None:
                        continue
                    if cutoff is None or dt >= cutoff:
                        parsed.append(row)
                rows = parsed[-lim:]
            except Exception:
                pass
            break

    if cache_key is not None:
        if len(_since_cache) >= _SINCE_CACHE_MAX:
            try:
                _since_cache.pop(next(iter(_since_cache)))
            except Exception:
                _since_cache.clear()
        _since_cache[cache_key] = list(rows)
    return rows


def export_fills_csv(path, days=7, limit=8000):
    """
    Write fee-aware fill rows to CSV for tax/ops export.
    Returns number of rows written (excluding header).
    """
    import csv
    rows = read_since_days(days=days, limit=limit)
    fields = [
        "timestamp", "broker", "side", "ticker", "asset_type", "price", "qty",
        "dollars", "status", "confirmed", "paper", "fee_est", "fee_paid",
        "commission", "slippage_bps", "fee_profile", "order_id", "reason",
    ]
    written = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            status = str(row.get("status") or "")
            # Prefer confirmed fills; still include buys/sells that look like fills
            side = str(row.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue
            if any(x in status for x in ("Fail", "Skipped", "Reject")):
                continue
            out = {k: row.get(k, "") for k in fields}
            w.writerow(out)
            written += 1
    return written


DECISION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decision_journal.jsonl")


def log_decision(entry):
    """
    Append one autotrader decision (buy attempt, skip, rotate reject, etc.).
    Keys: timestamp, broker, ticker, action (BUY/SKIP/ROTATE_SKIP/SCALE_IN_SKIP/…), score,
    reason, regime_ok, posture, engine (optional).
    """
    row = dict(entry)
    row.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    row.setdefault("kind", "decision")
    try:
        with open(DECISION_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        print(f"Decision journal write error: {e}")
    return row


def read_recent_decisions(limit=50):
    rows = []
    for ln in _read_jsonl_tail_lines(DECISION_FILE, limit):
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def read_decisions_since_days(days=7, limit=8000):
    """Return decision rows from the last N days (newest last).

    ``days=None`` (or < 0) = all time — no date cutoff; still capped by ``limit``.
    Uses a growing file-tail window (same idea as read_since_days).
    """
    if not os.path.exists(DECISION_FILE):
        return []
    from datetime import timedelta
    all_time = days is None or int(days) < 0
    cutoff = None if all_time else datetime.now() - timedelta(days=max(0, int(days)))
    lim = max(int(limit or 1), 1)
    try:
        file_size = os.path.getsize(DECISION_FILE)
    except Exception:
        return []
    window = min(file_size, max(65536, lim * 400))
    rows: list = []
    while True:
        lines, _ = _read_jsonl_tail_bytes(DECISION_FILE, window)
        parsed = []
        for ln in lines:
            try:
                row = json.loads(ln)
            except Exception:
                continue
            dt = _parse_ts(row)
            if dt is None:
                continue
            if cutoff is None or dt >= cutoff:
                parsed.append(row)
        rows = parsed[-lim:]
        if window >= file_size or cutoff is None:
            break
        oldest = None
        for ln in lines[:40]:
            try:
                dt = _parse_ts(json.loads(ln))
            except Exception:
                continue
            if dt is not None:
                oldest = dt
                break
        if oldest is not None and oldest < cutoff:
            break
        nxt = min(file_size, window * 2)
        if nxt == window:
            break
        window = nxt
    return rows
