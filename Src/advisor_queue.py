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
DEFAULT_TTL_SEC = 45 * 60  # 45 minutes
_lock = threading.Lock()


def _load() -> dict[str, Any]:
    if not os.path.exists(QUEUE_FILE):
        return {"proposals": []}
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"proposals": []}
        if not isinstance(data.get("proposals"), list):
            data["proposals"] = []
        return data
    except Exception:
        return {"proposals": []}


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


def expire_stale(now: float | None = None) -> int:
    """Drop expired pending proposals. Returns count removed."""
    now = float(now or time.time())
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
                continue
            kept.append(p)
        if removed:
            data["proposals"] = kept[-50:]
            _save(data)
        return removed


def list_pending(limit: int = 12) -> list[dict]:
    expire_stale()
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
) -> dict | None:
    """Create or refresh a pending proposal for broker+ticker."""
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
                str(p.get("status") or "") == "pending"
                and str(p.get("broker") or "") == broker_s
                and str(p.get("ticker") or "").upper() == tick
            ):
                p["price"] = float(price or 0)
                p["dollars"] = float(dollars or 0)
                p["score"] = float(score or 0)
                p["engine"] = str(engine or "")
                p["reason"] = str(reason or "entry")
                p["updated_at"] = now
                p["expires_at"] = now + float(ttl_sec or DEFAULT_TTL_SEC)
                _save(data)
                return dict(p)
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
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + float(ttl_sec or DEFAULT_TTL_SEC),
        }
        proposals.append(prop)
        data["proposals"] = proposals[-80:]
        _save(data)
        return prop


def _set_status(proposal_id: str, status: str, *, from_statuses=None) -> dict | None:
    pid = str(proposal_id or "").strip()
    if not pid:
        return None
    allowed = set(from_statuses or ("pending",))
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
                return dict(p)
    return None


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
    with _lock:
        data = _load()
        n = 0
        for p in data.get("proposals") or []:
            if isinstance(p, dict) and str(p.get("status") or "") in ("pending", "executing"):
                p["status"] = "rejected"
                p["resolved_at"] = time.time()
                n += 1
        if n:
            _save(data)
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
            }
            for p in pending
        ],
    }
