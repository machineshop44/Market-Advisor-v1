"""
OS keyring storage for broker / app secrets.

Plaintext fallbacks in settings.json are migrated here on load and scrubbed on
save when keyring holds the value. hydrate_settings_secrets() restores values
into the in-memory settings dict after migrate so existing .get() call sites work.
"""
from __future__ import annotations

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None

KEYRING_RH = "MarketAdvisor.Robinhood"
KEYRING_CB = "MarketAdvisor.Coinbase"
KEYRING_AI = "MarketAdvisor.AdvisorAI"
KEYRING_APP = "MarketAdvisor.App"
KEYRING_ET = "MarketAdvisor.ETrade"


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


def store_cb_api_key(value: str) -> bool:
    return _set(KEYRING_CB, "api_key", value or "")


def load_cb_api_key() -> str:
    return _get(KEYRING_CB, "api_key")


def store_advisor_api_key(value: str) -> bool:
    return _set(KEYRING_AI, "api_key", value or "")


def load_advisor_api_key() -> str:
    return _get(KEYRING_AI, "api_key")


def clear_advisor_api_key() -> bool:
    return _set(KEYRING_AI, "api_key", "")


def store_monitor_pass(value: str) -> bool:
    return _set(KEYRING_APP, "monitor_pass", value or "")


def load_monitor_pass() -> str:
    return _get(KEYRING_APP, "monitor_pass")


def store_discord_webhook(value: str) -> bool:
    return _set(KEYRING_APP, "discord_webhook", value or "")


def load_discord_webhook() -> str:
    return _get(KEYRING_APP, "discord_webhook")


def store_cursor_monitor_token(value: str) -> bool:
    return _set(KEYRING_APP, "cursor_monitor_token", value or "")


def load_cursor_monitor_token() -> str:
    return _get(KEYRING_APP, "cursor_monitor_token")


def store_etrade_consumer_key(value: str) -> bool:
    return _set(KEYRING_ET, "consumer_key", value or "")


def load_etrade_consumer_key() -> str:
    return _get(KEYRING_ET, "consumer_key")


def resolve_advisor_api_key(settings: dict | None = None) -> str:
    key = load_advisor_api_key()
    if key:
        return key
    return str((settings or {}).get("advisor_ai_api_key") or "").strip()


def persist_advisor_api_key(api_key: str, settings: dict | None = None) -> bool:
    api_key = str(api_key or "").strip()
    if not api_key:
        if settings is not None:
            settings["advisor_ai_api_key"] = ""
        return True
    ok = store_advisor_api_key(api_key)
    if settings is not None:
        # Keep in memory for UI; scrub drops from disk when keyring holds it
        settings["advisor_ai_api_key"] = api_key if ok else api_key
        if ok:
            pass
    return ok


def resolve_rh_password(settings: dict | None = None) -> str:
    """Prefer OS keyring; fall back to legacy settings.json plaintext once."""
    pwd = load_rh_password()
    if pwd:
        return pwd
    return str((settings or {}).get("rh_password") or "").strip()


def resolve_cb_api_secret(settings: dict | None = None) -> str:
    secret = load_cb_api_secret()
    if secret:
        return secret
    return str((settings or {}).get("cb_api_secret") or "").strip()


def resolve_cb_api_key(settings: dict | None = None) -> str:
    key = load_cb_api_key()
    if key:
        return key
    return str((settings or {}).get("cb_api_key") or "").strip()


def resolve_monitor_pass(settings: dict | None = None) -> str:
    pwd = load_monitor_pass()
    if pwd:
        return pwd
    return str((settings or {}).get("monitor_pass") or "")


def resolve_discord_webhook(settings: dict | None = None) -> str:
    wh = load_discord_webhook()
    if wh:
        return wh
    return str((settings or {}).get("discord_webhook") or "").strip()


def resolve_cursor_monitor_token(settings: dict | None = None) -> str:
    tok = load_cursor_monitor_token()
    if tok:
        return tok
    return str((settings or {}).get("cursor_monitor_token") or "").strip()


def resolve_etrade_consumer_key(settings: dict | None = None) -> str:
    key = load_etrade_consumer_key()
    if key:
        return key
    return str((settings or {}).get("etrade_consumer_key") or "").strip()


def persist_rh_password(password: str, settings: dict | None = None) -> bool:
    """
    Store RH password in keyring and clear plaintext from settings dict.
    Returns True if keyring accepted the value (or value was empty).
    """
    password = str(password or "").strip()
    if not password:
        if settings is not None:
            settings["rh_password"] = ""
        return True
    ok = store_rh_password(password)
    if settings is not None:
        settings["rh_password"] = "" if ok else password
    return ok


def persist_cb_api_secret(secret: str, settings: dict | None = None) -> bool:
    secret = str(secret or "").strip()
    if not secret:
        if settings is not None:
            settings["cb_api_secret"] = ""
        return True
    ok = store_cb_api_secret(secret)
    if settings is not None:
        settings["cb_api_secret"] = "" if ok else secret
    return ok


def persist_cb_api_key(api_key: str, settings: dict | None = None) -> bool:
    api_key = str(api_key or "").strip()
    if not api_key:
        if settings is not None:
            settings["cb_api_key"] = ""
        return True
    ok = store_cb_api_key(api_key)
    if settings is not None:
        settings["cb_api_key"] = api_key  # memory; scrubbed on disk
    return ok


def persist_monitor_pass(password: str, settings: dict | None = None) -> bool:
    password = str(password or "")
    if not password:
        if settings is not None:
            settings["monitor_pass"] = ""
        return True
    ok = store_monitor_pass(password)
    if settings is not None:
        settings["monitor_pass"] = password
    return ok


def persist_discord_webhook(url: str, settings: dict | None = None) -> bool:
    url = str(url or "").strip()
    if not url:
        if settings is not None:
            settings["discord_webhook"] = ""
        clear = store_discord_webhook("")
        return clear
    ok = store_discord_webhook(url)
    if settings is not None:
        settings["discord_webhook"] = url
    return ok


def persist_cursor_monitor_token(token: str, settings: dict | None = None) -> bool:
    token = str(token or "").strip()
    if not token:
        if settings is not None:
            settings["cursor_monitor_token"] = ""
        return True
    ok = store_cursor_monitor_token(token)
    if settings is not None:
        settings["cursor_monitor_token"] = token
    return ok


def persist_etrade_consumer_key(key: str, settings: dict | None = None) -> bool:
    key = str(key or "").strip()
    if not key:
        if settings is not None:
            settings["etrade_consumer_key"] = ""
        return True
    ok = store_etrade_consumer_key(key)
    if settings is not None:
        settings["etrade_consumer_key"] = key
    return ok


def migrate_settings_secrets(settings: dict) -> bool:
    """
    Move legacy plaintext secrets into keyring.
    Returns True when settings dict was mutated (caller should persist scrubbed file).
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
    ai_key = str(settings.get("advisor_ai_api_key") or "").strip()
    if ai_key:
        if store_advisor_api_key(ai_key):
            settings["advisor_ai_api_key"] = ""
            dirty = True
    cb_key = str(settings.get("cb_api_key") or "").strip()
    if cb_key:
        if store_cb_api_key(cb_key):
            dirty = True  # keep in memory; scrub on disk write
    mon = str(settings.get("monitor_pass") or "")
    if mon:
        if store_monitor_pass(mon):
            dirty = True
    wh = str(settings.get("discord_webhook") or "").strip()
    if wh:
        if store_discord_webhook(wh):
            dirty = True
    ctok = str(settings.get("cursor_monitor_token") or "").strip()
    if ctok:
        if store_cursor_monitor_token(ctok):
            dirty = True
    et_key = str(settings.get("etrade_consumer_key") or "").strip()
    if et_key:
        if store_etrade_consumer_key(et_key):
            dirty = True
    return dirty


def hydrate_settings_secrets(settings: dict) -> None:
    """Restore keyring secrets into the in-memory settings dict (not for disk)."""
    if not isinstance(settings, dict):
        return
    # RH password + CB secret stay out of settings memory (resolve at connect).
    ai = load_advisor_api_key()
    if ai:
        settings["advisor_ai_api_key"] = ai
    cb_key = load_cb_api_key()
    if cb_key:
        settings["cb_api_key"] = cb_key
    mon = load_monitor_pass()
    if mon:
        settings["monitor_pass"] = mon
    wh = load_discord_webhook()
    if wh:
        settings["discord_webhook"] = wh
    ctok = load_cursor_monitor_token()
    if ctok:
        settings["cursor_monitor_token"] = ctok
    et = load_etrade_consumer_key()
    if et:
        settings["etrade_consumer_key"] = et


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
    if load_advisor_api_key():
        out.pop("advisor_ai_api_key", None)
    elif not str(out.get("advisor_ai_api_key") or "").strip():
        out.pop("advisor_ai_api_key", None)
    if load_cb_api_key():
        out.pop("cb_api_key", None)
    elif not str(out.get("cb_api_key") or "").strip():
        out.pop("cb_api_key", None)
    if load_monitor_pass():
        out.pop("monitor_pass", None)
    elif not str(out.get("monitor_pass") or ""):
        out.pop("monitor_pass", None)
    if load_discord_webhook():
        out.pop("discord_webhook", None)
    elif not str(out.get("discord_webhook") or "").strip():
        out.pop("discord_webhook", None)
    if load_cursor_monitor_token():
        out.pop("cursor_monitor_token", None)
    elif not str(out.get("cursor_monitor_token") or "").strip():
        out.pop("cursor_monitor_token", None)
    if load_etrade_consumer_key():
        out.pop("etrade_consumer_key", None)
    elif not str(out.get("etrade_consumer_key") or "").strip():
        out.pop("etrade_consumer_key", None)
    return out
