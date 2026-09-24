"""
Desk Advisor proposal queue — ask-before-apply for auto-trader buys.

Proposals are created when advisor_ask_before_apply is ON; user approves on
desktop Home or companion POST /api/advisor/approve before live orders fire.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(_SRC_DIR, "advisor_queue.json")
DECISIONS_FILE = os.path.join(_SRC_DIR, "advisor_decisions.jsonl")
DEFAULT_TTL_SEC = 45 * 60  # 45 minutes
WAIT_TTL_SEC = 12 * 60  # AI "wait" should not rot in the queue
# After reject / AI skip, do not re-propose the same broker+ticker (SOUN thrash).
REJECT_REPROPOSE_COOLDOWN_SEC = 20 * 60
_DECISIONS_MAX_LINES = 500
_lock = threading.Lock()
_decisions_lock = threading.Lock()


def _load() -> dict[str, Any]:
    if not os.path.exists(QUEUE_FILE):
        return {"proposals": [], "repropose_cooldown": {}}
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"proposals": [], "repropose_cooldown": {}}
        if not isinstance(data.get("proposals"), list):
            data["proposals"] = []
        if not isinstance(data.get("repropose_cooldown"), dict):
            data["repropose_cooldown"] = {}
        return data
    except Exception:
        return {"proposals": [], "repropose_cooldown": {}}


def _save(data: dict[str, Any]) -> None:
    try:
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, QUEUE_FILE)
    except Exception as e:
        try:
            print(f"Advisor queue save error: {e}")
        except Exception:
            pass


def _cooldown_key(broker: str, ticker: str) -> str:
    return f"{str(broker or '').strip()}|{str(ticker or '').replace('-USD', '').upper().strip()}"


def _prune_cooldowns(data: dict[str, Any], *, now: float | None = None) -> None:
    now = float(now if now is not None else time.time())
    store = data.get("repropose_cooldown")
    if not isinstance(store, dict):
        data["repropose_cooldown"] = {}
        return
    dead = [k for k, v in store.items() if not isinstance(v, dict) or float(v.get("until") or 0) <= now]
    for k in dead:
        store.pop(k, None)


def set_repropose_cooldown(
    broker: str,
    ticker: str,
    *,
    seconds: float | None = None,
    reason: str = "",
    verdict: str = "",
    now: float | None = None,
) -> float:
    """
    Block re-proposing broker+ticker until now+seconds.
    Returns until-timestamp (0 if skipped).
    """
    broker_s = str(broker or "").strip()
    tick = str(ticker or "").replace("-USD", "").upper().strip()
    if not broker_s or not tick:
        return 0.0
    try:
        sec = float(seconds if seconds is not None else REJECT_REPROPOSE_COOLDOWN_SEC)
    except (TypeError, ValueError):
        sec = float(REJECT_REPROPOSE_COOLDOWN_SEC)
    if sec <= 0:
        return 0.0
    ts_now = float(now if now is not None else time.time())
    until = ts_now + sec
    with _lock:
        data = _load()
        _prune_cooldowns(data, now=ts_now)
        store = data.setdefault("repropose_cooldown", {})
        store[_cooldown_key(broker_s, tick)] = {
            "until": until,
            "reason": str(reason or "")[:120],
            "verdict": str(verdict or "").strip().lower()[:40],
            "set_at": ts_now,
        }
        _save(data)
    return until


def clear_repropose_cooldown(broker: str, ticker: str) -> None:
    broker_s = str(broker or "").strip()
    tick = str(ticker or "").replace("-USD", "").upper().strip()
    if not broker_s or not tick:
        return
    with _lock:
        data = _load()
        store = data.get("repropose_cooldown")
        if isinstance(store, dict):
            store.pop(_cooldown_key(broker_s, tick), None)
            _save(data)


def repropose_cooldown_remaining(
    broker: str,
    ticker: str,
    *,
    now: float | None = None,
) -> float:
    """Seconds left before broker+ticker may be proposed again (0 = clear)."""
    broker_s = str(broker or "").strip()
    tick = str(ticker or "").replace("-USD", "").upper().strip()
    if not broker_s or not tick:
        return 0.0
    ts_now = float(now if now is not None else time.time())
    with _lock:
        data = _load()
        _prune_cooldowns(data, now=ts_now)
        store = data.get("repropose_cooldown") or {}
        entry = store.get(_cooldown_key(broker_s, tick))
        if not isinstance(entry, dict):
            return 0.0
        until = float(entry.get("until") or 0)
        rem = until - ts_now
        if rem <= 0:
            store.pop(_cooldown_key(broker_s, tick), None)
            _save(data)
            return 0.0
        return rem


def repropose_blocked(
    broker: str,
    ticker: str,
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """True when a recent reject/skip should block a fresh propose."""
    rem = repropose_cooldown_remaining(broker, ticker, now=now)
    if rem <= 0:
        return False, ""
    mins = max(1, int((rem + 59) // 60))
    return True, f"re-ask cooldown ~{mins}m after prior reject/skip"


def expire_stale(now: float | None = None) -> int:
    """
    Drop expired pending proposals. Returns count removed.
    Records Journal decisions (expire / wait_expire) so the queue does not silently rot.
    """
    now = float(now or time.time())
    expired_rows: list[dict] = []
    with _lock:
        data = _load()
        kept = []
        removed = 0
        for p in data.get("proposals") or []:
            if not isinstance(p, dict):
                continue
            status = str(p.get("status") or "pending")
            if status != "pending":
                kept.append(p)
                continue
            exp = float(p.get("expires_at") or 0)
            if exp > 0 and now > exp:
                removed += 1
                expired_rows.append(dict(p))
                continue
            kept.append(p)
        if removed:
            data["proposals"] = kept[-50:]
            _save(data)
    for p in expired_rows:
        try:
            verdict = str(p.get("ai_verdict") or "").strip().lower()
            record_decision(
                proposal_id=str(p.get("id") or ""),
                broker=str(p.get("broker") or ""),
                ticker=str(p.get("ticker") or ""),
                verdict=verdict or "expire",
                action="wait_expire" if verdict == "wait" else "expire",
                source=str(p.get("ai_source") or ""),
                brief=str(p.get("ai_brief") or "")[:200],
                detail="TTL expired — cleared from pending queue",
                dollars=float(p.get("dollars") or 0),
                score=float(p.get("score") or 0),
                engine=str(p.get("engine") or ""),
                status="expired",
            )
        except Exception:
            pass
    return removed


def expire_wait_verdicts(now: float | None = None, *, ttl_sec: float | None = None) -> int:
    """
    Auto-clear pending proposals whose AI verdict is wait past WAIT_TTL_SEC
    (or custom ttl). Returns count rejected. Prefer shorter than default TTL.
    """
    now = float(now or time.time())
    limit = float(ttl_sec if ttl_sec is not None else WAIT_TTL_SEC)
    to_reject: list[str] = []
    with _lock:
        data = _load()
        for p in data.get("proposals") or []:
            if not isinstance(p, dict):
                continue
            if str(p.get("status") or "") != "pending":
                continue
            if str(p.get("ai_verdict") or "").strip().lower() != "wait":
                continue
            stamped = float(p.get("ai_at") or p.get("updated_at") or p.get("created_at") or 0)
            if stamped <= 0:
                continue
            if now - stamped >= limit:
                to_reject.append(str(p.get("id") or ""))
    n = 0
    for pid in to_reject:
        if not pid:
            continue
        rejected = reject(pid)
        if rejected:
            n += 1
            try:
                record_decision(
                    proposal_id=pid,
                    broker=str(rejected.get("broker") or ""),
                    ticker=str(rejected.get("ticker") or ""),
                    verdict="wait",
                    action="wait_auto_skip",
                    source=str(rejected.get("ai_source") or ""),
                    brief=str(rejected.get("ai_brief") or "")[:200],
                    detail=f"Wait TTL {int(limit)}s — auto-skipped",
                    dollars=float(rejected.get("dollars") or 0),
                    score=float(rejected.get("score") or 0),
                    engine=str(rejected.get("engine") or ""),
                    status="rejected",
                )
            except Exception:
                pass
    return n


def list_pending(limit: int = 12) -> list[dict]:
    expire_stale()
    expire_wait_verdicts()
    out = []
    with _lock:
        rows = list(_load().get("proposals") or [])
    for p in reversed(rows):
        if not isinstance(p, dict):
            continue
        if str(p.get("status") or "") != "pending":
            continue
        out.append(dict(p))
        if len(out) >= limit:
            break
    return list(reversed(out))


def patch_ai(proposal_id: str, ai: dict) -> dict | None:
    """Attach AI brief/verdict to a pending proposal."""
    pid = str(proposal_id or "").strip()
    if not pid or not isinstance(ai, dict):
        return None
    with _lock:
        data = _load()
        for p in data.get("proposals") or []:
            if isinstance(p, dict) and str(p.get("id") or "") == pid:
                if str(p.get("status") or "") not in ("pending", "executing"):
                    return None
                p["ai_verdict"] = str(ai.get("verdict") or "")
                p["ai_brief"] = str(ai.get("brief") or "")[:500]
                p["ai_detail"] = str(ai.get("detail") or "")[:500]
                p["ai_source"] = str(ai.get("source") or "")
                p["ai_error"] = str(ai.get("error") or "")[:200]
                p["ai_at"] = float(time.time())
                try:
                    from desk_advisor_ai import clamp_retry_after_min
                    p["ai_retry_after_min"] = int(
                        clamp_retry_after_min(
                            ai.get("retry_after_min"),
                            verdict=str(p.get("ai_verdict") or ""),
                        )
                    )
                except Exception:
                    p["ai_retry_after_min"] = 0
                # Wait verdicts should not rot in the queue for beginners
                if str(p.get("ai_verdict") or "").lower() == "wait":
                    wait_mins = float(p.get("ai_retry_after_min") or 0) or (WAIT_TTL_SEC / 60.0)
                    p["expires_at"] = time.time() + max(60.0, wait_mins * 60.0)
                _save(data)
                return dict(p)
    return None


def get(proposal_id: str) -> dict | None:
    pid = str(proposal_id or "").strip()
    if not pid:
        return None
    with _lock:
        rows = list(_load().get("proposals") or [])
    for p in rows:
        if isinstance(p, dict) and str(p.get("id") or "") == pid:
            return dict(p)
    return None


def propose(
    *,
    broker: str,
    ticker: str,
    asset_type: str = "",
    price: float = 0.0,
    dollars: float = 0.0,
    score: float = 0.0,
    engine: str = "",
    reason: str = "entry",
    ttl_sec: int = DEFAULT_TTL_SEC,
    regime_caution: bool = False,
    honor_cooldown: bool = True,
) -> dict | None:
    """Create or refresh a pending proposal for broker+ticker.

    honor_cooldown: when True (default), a recent reject/skip blocks a *new*
    proposal. An already-pending row is still refreshed (price/size).
    """
    broker_s = str(broker or "").strip()
    tick = str(ticker or "").replace("-USD", "").upper().strip()
    if not broker_s or not tick:
        return None
    now = time.time()
    expire_stale(now)
    with _lock:
        data = _load()
        proposals = data.setdefault("proposals", [])
        for p in proposals:
            if not isinstance(p, dict):
                continue
            if (
                str(p.get("broker") or "") == broker_s
                and str(p.get("ticker") or "").upper() == tick
            ):
                st = str(p.get("status") or "")
                # In-flight apply: do not spawn a duplicate that re-briefs / re-applies.
                if st == "executing":
                    return None
                if st == "pending":
                    p["price"] = float(price or 0)
                    p["dollars"] = float(dollars or 0)
                    p["score"] = float(score or 0)
                    p["engine"] = str(engine or "")
                    p["reason"] = str(reason or "entry")
                    p["regime_caution"] = bool(regime_caution)
                    p["updated_at"] = now
                    # Refresh TTL on each re-propose so live pending rows don't rot
                    # mid-brief; expire_stale still drops truly abandoned names.
                    p["expires_at"] = now + float(ttl_sec or DEFAULT_TTL_SEC)
                    out = dict(p)
                    out["_refreshed"] = True
                    _save(data)
                    return out
        if honor_cooldown:
            _prune_cooldowns(data, now=now)
            store = data.get("repropose_cooldown") or {}
            entry = store.get(_cooldown_key(broker_s, tick))
            if isinstance(entry, dict) and float(entry.get("until") or 0) > now:
                return None
        prop = {
            "id": uuid.uuid4().hex[:12],
            "broker": broker_s,
            "ticker": tick,
            "asset_type": str(asset_type or ""),
            "price": float(price or 0),
            "dollars": float(dollars or 0),
            "score": float(score or 0),
            "engine": str(engine or ""),
            "reason": str(reason or "entry"),
            "regime_caution": bool(regime_caution),
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + float(ttl_sec or DEFAULT_TTL_SEC),
            "ai_verdict": "",
            "ai_brief": "",
            "ai_detail": "",
            "ai_source": "",
            "ai_at": 0.0,
        }
        proposals.append(prop)
        data["proposals"] = proposals[-80:]
        _save(data)
        out = dict(prop)
        out["_refreshed"] = False
        return out


def _set_status(proposal_id: str, status: str, *, from_statuses=None) -> dict | None:
    pid = str(proposal_id or "").strip()
    if not pid:
        return None
    allowed = set(from_statuses or ("pending",))
    touched = None
    with _lock:
        data = _load()
        for p in data.get("proposals") or []:
            if isinstance(p, dict) and str(p.get("id") or "") == pid:
                if str(p.get("status") or "") not in allowed:
                    return None
                p["status"] = status
                if status in ("approved", "rejected"):
                    p["resolved_at"] = time.time()
                else:
                    p["updated_at"] = time.time()
                _save(data)
                touched = dict(p)
                break
    if not touched:
        return None
    # Cool down re-asks after reject; clear on approve so a later setup can fire.
    try:
        if status == "rejected":
            set_repropose_cooldown(
                touched.get("broker") or "",
                touched.get("ticker") or "",
                reason="rejected",
                verdict=str(touched.get("ai_verdict") or "reject"),
            )
        elif status == "approved":
            clear_repropose_cooldown(
                touched.get("broker") or "",
                touched.get("ticker") or "",
            )
    except Exception:
        pass
    return touched


def approve(proposal_id: str) -> dict | None:
    return _set_status(proposal_id, "approved")


def reject(proposal_id: str) -> dict | None:
    return _set_status(proposal_id, "rejected", from_statuses=("pending", "executing"))


def claim(proposal_id: str) -> dict | None:
    """Mark pending as executing so a second approve cannot double-fire."""
    return _set_status(proposal_id, "executing", from_statuses=("pending",))


def complete(proposal_id: str, ok: bool) -> dict | None:
    """Finish an executing proposal: approved on fill, pending again on miss."""
    if ok:
        return _set_status(proposal_id, "approved", from_statuses=("executing", "pending"))
    return _set_status(proposal_id, "pending", from_statuses=("executing",))


def reject_all() -> int:
    touched: list[tuple[str, str]] = []
    with _lock:
        data = _load()
        n = 0
        for p in data.get("proposals") or []:
            if isinstance(p, dict) and str(p.get("status") or "") in ("pending", "executing"):
                p["status"] = "rejected"
                p["resolved_at"] = time.time()
                touched.append((str(p.get("broker") or ""), str(p.get("ticker") or "")))
                n += 1
        if n:
            _save(data)
    for broker, tick in touched:
        try:
            set_repropose_cooldown(broker, tick, reason="reject_all", verdict="reject")
        except Exception:
            pass
    return n


def monitor_payload(limit: int = 5) -> dict:
    pending = list_pending(limit=limit)
    return {
        "count": len(pending),
        "pending": [
            {
                "id": p.get("id"),
                "broker": p.get("broker"),
                "ticker": p.get("ticker"),
                "dollars": p.get("dollars"),
                "score": p.get("score"),
                "engine": p.get("engine"),
                "price": p.get("price"),
                "ai_verdict": p.get("ai_verdict") or "",
                "ai_brief": p.get("ai_brief") or "",
                "ai_detail": p.get("ai_detail") or "",
                "ai_source": p.get("ai_source") or "",
                "ai_pending": not bool(p.get("ai_brief")),
            }
            for p in pending
        ],
    }


def record_decision(
    *,
    proposal_id: str = "",
    broker: str = "",
    ticker: str = "",
    verdict: str = "",
    action: str = "",
    source: str = "",
    brief: str = "",
    detail: str = "",
    dollars: float = 0.0,
    score: float = 0.0,
    engine: str = "",
    status: str = "",
) -> dict:
    """
    Append a durable Advisor decision row for Journal lookup.
    action examples: propose, approve, reject, auto_apply, auto_reject, hold_rails, expire
    """
    row = {
        "at": time.time(),
        "id": str(proposal_id or "").strip() or uuid.uuid4().hex[:12],
        "broker": str(broker or "").strip(),
        "ticker": str(ticker or "").replace("-USD", "").upper().strip(),
        "verdict": str(verdict or "").strip().lower(),
        "action": str(action or "").strip().lower(),
        "source": str(source or "").strip(),
        "brief": str(brief or "").strip()[:500],
        "detail": str(detail or "").strip()[:500],
        "dollars": float(dollars or 0),
        "score": float(score or 0),
        "engine": str(engine or "").strip(),
        "status": str(status or "").strip(),
    }
    with _decisions_lock:
        try:
            with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
                f.flush()
        except Exception:
            pass
        _trim_decisions_file()
    return row


def _trim_decisions_file() -> None:
    try:
        if not os.path.isfile(DECISIONS_FILE):
            return
        with open(DECISIONS_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _DECISIONS_MAX_LINES:
            return
        keep = lines[-_DECISIONS_MAX_LINES:]
        tmp = DECISIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, DECISIONS_FILE)
    except Exception:
        pass


def list_decisions(limit: int = 120) -> list[dict]:
    """Newest-first Advisor decision history for Journal."""
    lim = max(1, min(int(limit or 120), 500))
    rows: list[dict] = []
    with _decisions_lock:
        try:
            if os.path.isfile(DECISIONS_FILE):
                with open(DECISIONS_FILE, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(obj, dict):
                            rows.append(obj)
        except Exception:
            rows = []
    # Also fold in recent queue proposals that may lack a decisions line yet
    with _lock:
        try:
            for p in list(_load().get("proposals") or []):
                if not isinstance(p, dict):
                    continue
                if not (p.get("ai_verdict") or p.get("status") in ("approved", "rejected", "executing")):
                    continue
                rows.append({
                    "at": float(p.get("ai_at") or p.get("resolved_at") or p.get("updated_at") or p.get("created_at") or 0),
                    "id": p.get("id") or "",
                    "broker": p.get("broker") or "",
                    "ticker": p.get("ticker") or "",
                    "verdict": p.get("ai_verdict") or "",
                    "action": str(p.get("status") or "pending"),
                    "source": p.get("ai_source") or "",
                    "brief": p.get("ai_brief") or "",
                    "detail": p.get("ai_detail") or "",
                    "dollars": p.get("dollars") or 0,
                    "score": p.get("score") or 0,
                    "engine": p.get("engine") or "",
                    "status": p.get("status") or "",
                    "_from_queue": True,
                })
        except Exception:
            pass
    # Dedupe by id+action+verdict keeping newest
    seen = set()
    out = []
    for r in sorted(rows, key=lambda x: float(x.get("at") or 0), reverse=True):
        key = (
            str(r.get("id") or ""),
            str(r.get("action") or ""),
            str(r.get("verdict") or ""),
            round(float(r.get("at") or 0), 0),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= lim:
            break
    return out
