"""
OS keyring storage for broker secrets (Robinhood password, Coinbase API secret).

E*TRADE tokens live in etrade_broker.py. Plaintext fallbacks in settings.json are
migrated here on load and scrubbed on save when keyring holds the value.
"""
from __future__ import annotations

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None

KEYRING_RH = "MarketAdvisor.Robinhood"
KEYRING_CB = "MarketAdvisor.Coinbase"


def _set(service: str, name: str, value: str) -> bool:
    if keyring is None or not service or not name:
        return False
    try:
        if value:
            keyring.set_password(service, name, str(value))
        else:
            try:
                keyring.delete_password(service, name)
            except Exception:
                pass
        return True
    except Exception:
        return False


def _get(service: str, name: str) -> str:
    if keyring is None or not service or not name:
        return ""
    try:
        return keyring.get_password(service, name) or ""
    except Exception:
        return ""


def store_rh_password(value: str) -> bool:
    return _set(KEYRING_RH, "password", value or "")


def load_rh_password() -> str:
    return _get(KEYRING_RH, "password")


def clear_rh_password() -> bool:
    return _set(KEYRING_RH, "password", "")


def store_cb_api_secret(value: str) -> bool:
    return _set(KEYRING_CB, "api_secret", value or "")


def load_cb_api_secret() -> str:
    return _get(KEYRING_CB, "api_secret")


def clear_cb_api_secret() -> bool:
    return _set(KEYRING_CB, "api_secret", "")


def migrate_settings_secrets(settings: dict) -> bool:
    """
    Move legacy plaintext rh_password / cb_api_secret into keyring.
    Returns True when settings dict was mutated (caller should persist).
    """
    if not isinstance(settings, dict):
        return False
    dirty = False
    pwd = str(settings.get("rh_password") or "").strip()
    if pwd:
        if store_rh_password(pwd):
            settings["rh_password"] = ""
            dirty = True
    secret = str(settings.get("cb_api_secret") or "").strip()
    if secret:
        if store_cb_api_secret(secret):
            settings["cb_api_secret"] = ""
            dirty = True
    return dirty


def scrub_settings_for_disk(settings: dict) -> dict:
    """Drop plaintext secrets from a settings payload when keyring holds them."""
    out = dict(settings or {})
    if load_rh_password():
        out.pop("rh_password", None)
    elif not str(out.get("rh_password") or "").strip():
        out.pop("rh_password", None)
    if load_cb_api_secret():
        out.pop("cb_api_secret", None)
    elif not str(out.get("cb_api_secret") or "").strip():
        out.pop("cb_api_secret", None)
    return out
