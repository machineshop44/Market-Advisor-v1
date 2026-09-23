"""
Consecutive losing exits → temporary buy pause (peer day-trader risk rail).

Separate from scoring._loss_streak (hard-stop hysteresis). This tracks closed
exit outcomes and pauses *new buys* after a streak of losses.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

_STATE_NAME = "consecutive_loss_streak.json"
_streak: dict[str, dict] = {}  # broker -> {count, paused_until, last_ts}
_loaded = False


def _state_path() -> str:
    try:
        from scoring import STATE_DIR

        base = STATE_DIR
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    return os.path.join(str(base), _STATE_NAME)


def load(force: bool = False) -> None:
    global _streak, _loaded
    if _loaded and not force:
        return
    path = _state_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                _streak = {
                    str(k): v for k, v in raw.items() if isinstance(v, dict)
                }
            else:
                _streak = {}
        else:
            _streak = {}
    except Exception:
        _streak = {}
    _loaded = True


def save() -> None:
    load()
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_streak, f, indent=2)
    except Exception:
        pass


def record_exit_result(broker: str, *, was_loss: bool, now: Optional[float] = None) -> None:
    """Call after a confirmed sell. Losses increment streak; wins reset."""
    load()
    ts = float(now if now is not None else time.time())
    b = str(broker)
    entry = _streak.setdefault(b, {"count": 0, "paused_until": 0.0, "last_ts": 0.0})
    if was_loss:
        entry["count"] = int(entry.get("count") or 0) + 1
    else:
        entry["count"] = 0
    entry["last_ts"] = ts
    save()


def maybe_trip_pause(
    broker: str,
    *,
    max_losses: int = 3,
    pause_minutes: int = 45,
    now: Optional[float] = None,
) -> tuple[bool, str]:
    """If streak hit max_losses, arm a buy pause. Returns (tripped_now, message)."""
    load()
    ts = float(now if now is not None else time.time())
    b = str(broker)
    entry = _streak.setdefault(b, {"count": 0, "paused_until": 0.0, "last_ts": 0.0})
    count = int(entry.get("count") or 0)
    cap = max(1, int(max_losses or 3))
    if count < cap:
        return False, ""
    until = ts + max(5, int(pause_minutes or 45)) * 60
    entry["paused_until"] = until
    entry["count"] = 0  # reset after trip so we don't re-trip forever
    save()
    return True, (
        f"Consecutive loss guard — {cap} losing exits; "
        f"pausing new buys ~{int(pause_minutes)}m"
    )


def buys_paused(broker: str, *, now: Optional[float] = None) -> tuple[bool, str]:
    load()
    ts = float(now if now is not None else time.time())
    entry = _streak.get(str(broker)) or {}
    until = float(entry.get("paused_until") or 0)
    if until <= ts:
        return False, ""
    mins = max(1, int((until - ts) / 60.0))
    return True, f"consecutive-loss pause ({mins}m left)"


def pause_remaining_sec(broker: str, *, now: Optional[float] = None) -> float:
    """Seconds left on a buy pause (0 if not paused)."""
    load()
    ts = float(now if now is not None else time.time())
    until = float((_streak.get(str(broker)) or {}).get("paused_until") or 0)
    return max(0.0, until - ts)


def streak_count(broker: str) -> int:
    load()
    return int((_streak.get(str(broker)) or {}).get("count") or 0)


def clear(broker: Optional[str] = None) -> None:
    load()
    if broker is None:
        _streak.clear()
    else:
        _streak.pop(str(broker), None)
    save()
