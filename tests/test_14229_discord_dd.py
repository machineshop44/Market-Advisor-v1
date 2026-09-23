"""1.42.29 — Discord advisor spam + brief sanitize + DD re-check before buys."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Src"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import desk_advisor_ai as dai


def test_sanitize_schema_echo_brief():
    out = dai._sanitize_ai_brief(
        "<=2 sentences plain English",
        detail="Wait for a better LTC entry near support.",
        verdict="wait",
    )
    assert "2 sentences" not in out.lower()
    assert "LTC" in out or "support" in out.lower()


def test_sanitize_keeps_real_brief():
    text = "Buy XRP — strong momentum and score support the entry."
    assert dai._sanitize_ai_brief(text, detail="x", verdict="approve") == text
