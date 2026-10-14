import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Src"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_crypto_gates: run the wall-clock/network crypto entry gates for real",
    )


@pytest.fixture(autouse=True)
def _neutral_crypto_time_gates(request, monkeypatch):
    """Overnight-hours and live-BTC gates depend on the clock and Yahoo; keep other tests stable."""
    if request.node.get_closest_marker("real_crypto_gates"):
        yield
        return
    import scoring

    monkeypatch.setattr(scoring, "crypto_overnight_entry_ok", lambda settings=None, now=None: (True, ""))
    monkeypatch.setattr(scoring, "crypto_btc_drop_ok", lambda settings=None, **k: (True, ""))
    yield
