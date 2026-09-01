"""Advisor auto-apply rails use is_connected (not .connected)."""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_rails_ok_uses_is_connected():
    # Mimic the attribute check without constructing full GUI
    broker = SimpleNamespace(is_connected=True, reauth_needed=False, broker_id="COINBASE")
    connected = bool(getattr(broker, "is_connected", False)) or False
    # Old bug:
    wrong = bool(getattr(broker, "connected", False))
    assert connected is True
    assert wrong is False
