"""Crash log helpers."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import crash_log as cl


def test_write_and_read_crash(tmp_path, monkeypatch):
    path = tmp_path / "crash_log.txt"
    monkeypatch.setattr(cl, "CRASH_LOG_FILE", str(path))
    cl.write_crash("TEST", "hello crash", also_stderr=False)
    text = path.read_text(encoding="utf-8")
    assert "TEST" in text
    assert "hello crash" in text


def test_record_exception(tmp_path, monkeypatch):
    path = tmp_path / "crash_log.txt"
    monkeypatch.setattr(cl, "CRASH_LOG_FILE", str(path))
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        cl.record_exception(*sys.exc_info(), where="unit")
    text = path.read_text(encoding="utf-8")
    assert "EXCEPTION" in text
    assert "boom" in text
