"""Regression: buy handoff must pass advisor_gate positionally to run_thread."""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_run_thread_rejects_advisor_gate_kwarg():
    """Documents the crash class — unexpected kwargs must not reach run_thread."""
    import gui as gui_mod

    sig = inspect.signature(gui_mod.MarketAdvisorGUI.run_thread)
    assert "advisor_gate" not in sig.parameters
    assert "unlock_queue_on_error" in sig.parameters


def test_bg_buy_batch_safe_accepts_positional_advisor_gate():
    import gui as gui_mod

    sig = inspect.signature(gui_mod.MarketAdvisorGUI._bg_buy_batch_safe)
    params = list(sig.parameters)
    # self, candidates, rank, advisor_gate
    assert "advisor_gate" in params
    assert params.index("advisor_gate") > params.index("candidates")
