"""
Crash / fault logging — survives hard exits better than activity_log alone.

Writes to Src/crash_log.txt (same folder as activity_log.txt / settings.json).
Captures:
  - Uncaught Python exceptions (main + threads)
  - Fatal native faults via faulthandler (segfault / access violation)
  - Explicit record_crash() calls from guarded UI paths
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
CRASH_LOG_FILE = os.path.join(_SRC_DIR, "crash_log.txt")
_FAULT_FILE = None  # keep handle open for faulthandler
_installed = False
_MAX_BYTES = 2_000_000  # ~2 MB then rotate


def crash_log_path() -> str:
    return CRASH_LOG_FILE


def _rotate_if_huge() -> None:
    try:
        if not os.path.isfile(CRASH_LOG_FILE):
            return
        if os.path.getsize(CRASH_LOG_FILE) < _MAX_BYTES:
            return
        bak = CRASH_LOG_FILE + ".prev"
        if os.path.isfile(bak):
            os.remove(bak)
        os.replace(CRASH_LOG_FILE, bak)
    except Exception:
        pass


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _version_line() -> str:
    try:
        from version import __version__, VERSION_NOTE

        return f"Market Advisor {__version__} — {VERSION_NOTE}"
    except Exception:
        return "Market Advisor (version unknown)"


def write_crash(title: str, body: str = "", *, also_stderr: bool = True) -> None:
    """Append a crash/fault block to crash_log.txt (best-effort, flushed)."""
    _rotate_if_huge()
    block = (
        f"\n{'=' * 72}\n"
        f"[{_stamp()}] {title}\n"
        f"{_version_line()}\n"
        f"{'-' * 72}\n"
        f"{body.rstrip()}\n"
        f"{'=' * 72}\n"
    )
    try:
        with open(CRASH_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(block)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        pass
    if also_stderr:
        try:
            if sys.stderr is not None:
                sys.stderr.write(block)
                sys.stderr.flush()
        except Exception:
            pass


def record_exception(exc_type, exc, tb, *, where: str = "uncaught") -> None:
    parts = [f"where={where}", "".join(traceback.format_exception(exc_type, exc, tb))]
    write_crash(f"EXCEPTION ({where})", "\n".join(parts))


def record_crash(message: str, *, detail: str = "", where: str = "app") -> None:
    write_crash(f"CRASH ({where})", f"{message}\n{detail}".strip())


def read_tail(*, max_chars: int = 12000, max_lines: int = 120) -> dict:
    """
    Tail of crash_log.txt for remote /api/agent/crash_log.
    Returns {ok, path, text, truncated, exists, size}.
    """
    path = CRASH_LOG_FILE
    out = {
        "ok": True,
        "path": path,
        "text": "",
        "truncated": False,
        "exists": False,
        "size": 0,
    }
    try:
        if not os.path.isfile(path):
            out["text"] = "(no crash_log.txt yet — no hard faults recorded)"
            return out
        size = os.path.getsize(path)
        out["exists"] = True
        out["size"] = int(size)
        max_chars = max(500, min(int(max_chars or 12000), 80_000))
        max_lines = max(10, min(int(max_lines or 120), 400))
        # Read last chunk by bytes for large files
        with open(path, "rb") as f:
            if size > max_chars + 200:
                f.seek(max(0, size - (max_chars + 200)))
                raw = f.read()
                out["truncated"] = True
            else:
                raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        if out["truncated"] and "\n" in text:
            text = text.split("\n", 1)[-1]
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
            out["truncated"] = True
        out["text"] = "\n".join(lines)
        return out
    except Exception as e:
        out["ok"] = False
        out["text"] = f"(crash_log read error: {e})"
        return out


def _excepthook(exc_type, exc, tb):
    record_exception(exc_type, exc, tb, where="sys.excepthook")
    # Chain to default so frozen builds still show dialogs when available
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass


def _threading_excepthook(args):
    # threading.ExceptHookArgs: exc_type, exc_value, exc_traceback, thread
    try:
        name = getattr(getattr(args, "thread", None), "name", "?")
        record_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            where=f"thread:{name}",
        )
    except Exception:
        write_crash("THREAD EXCEPTION", str(args))


def install_crash_logging() -> str:
    """Call as early as possible in main() — before QApplication preferred."""
    global _installed, _FAULT_FILE
    if _installed:
        return CRASH_LOG_FILE
    _installed = True
    _rotate_if_huge()

    try:
        write_crash(
            "BOOT",
            f"pid={os.getpid()} cwd={os.getcwd()}\n"
            f"executable={sys.executable}\n"
            f"faulthandler=on crash_log={CRASH_LOG_FILE}",
            also_stderr=False,
        )
    except Exception:
        pass

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _threading_excepthook

    try:
        _FAULT_FILE = open(CRASH_LOG_FILE, "a", encoding="utf-8")
        _FAULT_FILE.write(
            f"\n[{_stamp()}] faulthandler enabled (native faults dump below)\n"
        )
        _FAULT_FILE.flush()
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except Exception:
        try:
            faulthandler.enable(all_threads=True)
        except Exception:
            pass

    return CRASH_LOG_FILE


def last_crash_snippet(max_chars: int = 1200) -> str:
    """Tail of crash log for Activity / Discord boot tip."""
    try:
        if not os.path.isfile(CRASH_LOG_FILE):
            return ""
        with open(CRASH_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        if "EXCEPTION" not in data and "Fatal Python error" not in data and "CRASH" not in data:
            return ""
        return data[-max_chars:].strip()
    except Exception:
        return ""
