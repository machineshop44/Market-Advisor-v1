"""
E*TRADE OAuth 1.0a + signed REST client.

Sandbox:  https://apisb.etrade.com
Live:     https://api.etrade.com

Access tokens expire at midnight US/Eastern and go inactive after ~2h idle
(renewable same day via renew_access_token).
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
from typing import Any, Optional
from xml.etree import ElementTree as ET

try:
    import requests
    from requests_oauthlib import OAuth1
except ImportError:  # pragma: no cover
    requests = None
    OAuth1 = None


SANDBOX_BASE = "https://apisb.etrade.com"
LIVE_BASE = "https://api.etrade.com"
AUTHORIZE_URL = "https://us.etrade.com/e/t/etws/authorize"

_MIN_REQUEST_GAP_SEC = 0.35
_MAX_RETRIES = 4


class ETradeAPIError(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ETradeClient:
    """Thin OAuth1 client for E*TRADE Accounts / Market / Order APIs."""

    def __init__(self, consumer_key, consumer_secret, environment="sandbox"):
        if requests is None or OAuth1 is None:
            raise ImportError("requests and requests-oauthlib are required for E*TRADE")
        self.consumer_key = str(consumer_key or "").strip()
        self.consumer_secret = str(consumer_secret or "").strip()
        self.environment = "live" if str(environment).lower() == "live" else "sandbox"
        self.base_url = LIVE_BASE if self.environment == "live" else SANDBOX_BASE
        self.access_token = None
        self.access_token_secret = None
        self.request_token = None
        self.request_token_secret = None
        self._last_request_ts = 0.0
        self.session = requests.Session()

    # ------------------------------------------------------------------ auth
    def _oauth(self, resource_owner_key=None, resource_owner_secret=None, callback_uri=None, verifier=None):
        kwargs = {
            "client_key": self.consumer_key,
            "client_secret": self.consumer_secret,
            "signature_method": "HMAC-SHA1",
            "signature_type": "AUTH_HEADER",
        }
        if resource_owner_key is not None:
            kwargs["resource_owner_key"] = resource_owner_key
        if resource_owner_secret is not None:
            kwargs["resource_owner_secret"] = resource_owner_secret
        if callback_uri is not None:
            kwargs["callback_uri"] = callback_uri
        # E*TRADE requires oauth_verifier in the Authorization header (not query string).
        if verifier is not None:
            kwargs["verifier"] = verifier
        return OAuth1(**kwargs)

    def get_request_token(self):
        url = f"{self.base_url}/oauth/request_token"
        auth = self._oauth(callback_uri="oob")
        resp = self.session.get(url, auth=auth, timeout=30)
        if resp.status_code != 200:
            raise ETradeAPIError(
                _format_oauth_error("request_token", resp),
                status_code=resp.status_code,
                body=resp.text,
            )
        data = dict(urllib.parse.parse_qsl(resp.text))
        self.request_token = data.get("oauth_token")
        self.request_token_secret = data.get("oauth_token_secret")
        if not self.request_token:
            raise ETradeAPIError("request_token response missing oauth_token", body=resp.text)
        return self.request_token, self.request_token_secret

    def authorization_url(self, request_token=None):
        token = request_token or self.request_token
        if not token:
            raise ETradeAPIError("No request token — call get_request_token first")
        return f"{AUTHORIZE_URL}?key={urllib.parse.quote(self.consumer_key)}&token={urllib.parse.quote(token)}"

    def get_access_token(self, verifier, request_token=None, request_token_secret=None):
        token = request_token or self.request_token
        secret = request_token_secret or self.request_token_secret
        if not token or not secret:
            raise ETradeAPIError(
                "Missing request token/secret for access_token exchange — "
                "click Authorize in Browser again, then paste the NEW verification code"
            )
        verifier = str(verifier or "").strip()
        if not verifier:
            raise ETradeAPIError("Missing oauth_verifier — paste the verification code from E*TRADE")
        url = f"{self.base_url}/oauth/access_token"
        # Pass verifier= so OAuth1 signs oauth_verifier into the Authorization header.
        # Query-string oauth_verifier is ignored by E*TRADE and yields HTTP 400.
        auth = self._oauth(
            resource_owner_key=token,
            resource_owner_secret=secret,
            verifier=verifier,
        )
        resp = self.session.get(url, auth=auth, timeout=30)
        if resp.status_code != 200:
            raise ETradeAPIError(
                _format_oauth_error("access_token", resp),
                status_code=resp.status_code,
                body=resp.text,
            )
        data = dict(urllib.parse.parse_qsl(resp.text))
        self.access_token = data.get("oauth_token")
        self.access_token_secret = data.get("oauth_token_secret")
        self.request_token = None
        self.request_token_secret = None
        if not self.access_token:
            raise ETradeAPIError("access_token response missing oauth_token", body=resp.text)
        return self.access_token, self.access_token_secret

    def set_access_token(self, token, token_secret):
        self.access_token = token
        self.access_token_secret = token_secret

    def renew_access_token(self):
        self._require_access()
        url = f"{self.base_url}/oauth/renew_access_token"
        auth = self._oauth(self.access_token, self.access_token_secret)
        resp = self.session.get(url, auth=auth, timeout=30)
        if resp.status_code != 200:
            raise ETradeAPIError(
                f"renew_access_token failed ({resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
                body=resp.text,
            )
        return True

    def revoke_access_token(self):
        if not self.access_token:
            return True
        url = f"{self.base_url}/oauth/revoke_access_token"
        auth = self._oauth(self.access_token, self.access_token_secret)
        try:
            self.session.get(url, auth=auth, timeout=30)
        finally:
            self.access_token = None
            self.access_token_secret = None
        return True

    def _require_access(self):
        if not self.access_token or not self.access_token_secret:
            raise ETradeAPIError("Not authorized — complete OAuth first")

    # --------------------------------------------------------------- HTTP
    def _throttle(self):
        gap = _MIN_REQUEST_GAP_SEC - (time.time() - self._last_request_ts)
        if gap > 0:
            time.sleep(gap)

    def request(self, method, path, params=None, json_body=None, xml_body=None, accept="json"):
        self._require_access()
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.base_url}{path}"
        headers = {}
        if accept == "json":
            headers["Accept"] = "application/json"
        data = None
        if xml_body is not None:
            headers["Content-Type"] = "application/xml"
            data = xml_body if isinstance(xml_body, (bytes, bytearray)) else str(xml_body).encode("utf-8")
        elif json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode("utf-8")

        last_err = None
        for attempt in range(_MAX_RETRIES):
            self._throttle()
            auth = self._oauth(self.access_token, self.access_token_secret)
            try:
                resp = self.session.request(
                    method.upper(),
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    auth=auth,
                    timeout=45,
                )
            except Exception as e:
                last_err = e
                time.sleep(min(8.0, (0.5 * (2 ** attempt)) + random.random() * 0.2))
                continue
            finally:
                self._last_request_ts = time.time()

            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(8.0, (0.6 * (2 ** attempt)) + random.random() * 0.3))
                last_err = ETradeAPIError(
                    f"HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    body=resp.text,
                )
                continue

            if resp.status_code >= 400:
                raise ETradeAPIError(
                    f"E*TRADE {method.upper()} {path} failed ({resp.status_code}): {resp.text[:400]}",
                    status_code=resp.status_code,
                    body=resp.text,
                )

            if not resp.content:
                return {}
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype or (resp.text[:1] in "{["):
                try:
                    return resp.json()
                except Exception:
                    pass
            # XML fallback
            try:
                root = ET.fromstring(resp.text)
                return _xml_to_dict(root)
            except Exception:
                return {"raw": resp.text}

        if last_err:
            raise last_err
        raise ETradeAPIError("request failed after retries")

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post_xml(self, path, xml_body):
        return self.request("POST", path, xml_body=xml_body)

    def put_xml(self, path, xml_body):
        return self.request("PUT", path, xml_body=xml_body)

    # ----------------------------------------------------------- helpers
    def list_accounts(self):
        data = self.get("/v1/accounts/list")
        return _extract_accounts(data)

    def get_balance(self, account_id_key, inst_type="BROKERAGE"):
        return self.get(
            f"/v1/accounts/{account_id_key}/balance",
            params={"instType": inst_type, "realTimeNAV": "true"},
        )

    def get_portfolio(self, account_id_key, count=50):
        return self.get(
            f"/v1/accounts/{account_id_key}/portfolio",
            params={"count": count, "view": "COMPLETE"},
        )

    def get_quotes(self, symbols):
        if isinstance(symbols, (list, tuple, set)):
            sym = ",".join(str(s).upper() for s in symbols)
        else:
            sym = str(symbols).upper()
        return self.get(f"/v1/market/quote/{urllib.parse.quote(sym)}")

    def list_orders(self, account_id_key, status="OPEN"):
        params = {}
        if status:
            params["status"] = status
        return self.get(f"/v1/accounts/{account_id_key}/orders", params=params or None)

    def preview_equity_order(self, account_id_key, xml_body):
        return self.post_xml(f"/v1/accounts/{account_id_key}/orders/preview", xml_body)

    def place_equity_order(self, account_id_key, xml_body):
        return self.post_xml(f"/v1/accounts/{account_id_key}/orders/place", xml_body)

    def cancel_order(self, account_id_key, order_id):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<CancelOrderRequest><orderId>{int(order_id)}</orderId></CancelOrderRequest>"
        )
        return self.put_xml(f"/v1/accounts/{account_id_key}/orders/cancel", xml)


def _xml_to_dict(elem):
    children = list(elem)
    if not children:
        return elem.text
    out = {}
    for child in children:
        val = _xml_to_dict(child)
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(val)
        else:
            out[tag] = val
    return out


def _extract_accounts(data: Any) -> list:
    """Normalize list-accounts JSON/XML into a list of account dicts."""
    if not data:
        return []
    # Common JSON shape: AccountListResponse.Accounts.Account
    node = data
    for key in ("AccountListResponse", "accountListResponse"):
        if isinstance(node, dict) and key in node:
            node = node[key]
            break
    accounts_node = None
    if isinstance(node, dict):
        accounts_node = node.get("Accounts") or node.get("accounts") or node
    if isinstance(accounts_node, dict):
        acc = accounts_node.get("Account") or accounts_node.get("account")
        if acc is None:
            return []
        if isinstance(acc, list):
            return [a for a in acc if isinstance(a, dict)]
        return [acc] if isinstance(acc, dict) else []
    if isinstance(accounts_node, list):
        return [a for a in accounts_node if isinstance(a, dict)]
    return []


def _format_oauth_error(step, resp) -> str:
    """Surface a usable E*TRADE OAuth failure (HTML Tomcat pages are common on 400)."""
    status = getattr(resp, "status_code", None)
    raw = (getattr(resp, "text", None) or "")[:800]
    compact = " ".join(raw.split())
    # Strip boilerplate HTML titles when present
    for noise in (
        "HTTP Status 400 – Bad Request",
        "HTTP Status 400 - Bad Request",
        "HTTP Status 401 – Unauthorized",
        "HTTP Status 401 - Unauthorized",
    ):
        if noise in compact:
            compact = compact.replace(noise, "").strip(" -–|")
    hint = ""
    if status == 400:
        hint = (
            " — usually a bad/used verifier, stale request token, or mismatched "
            "sandbox/live key. Click Authorize in Browser once, paste the NEW code, "
            "then Complete Connection immediately."
        )
    elif status in (401, 403):
        hint = (
            " — check consumer key/secret for this Environment and that the "
            "verification code matches the latest Authorize step."
        )
    body_bit = compact[:280] if compact else "(empty body)"
    return f"{step} failed ({status}): {body_bit}{hint}"


def midnight_et_epoch(now_ts: Optional[float] = None) -> float:
    """Next (or today's end) midnight US/Eastern as epoch seconds — conservative token expiry."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        tz = ZoneInfo("America/New_York")
        now = datetime.fromtimestamp(now_ts or time.time(), tz)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if now >= end:
            end = end + timedelta(days=1)
        return end.timestamp()
    except Exception:
        # Fallback: ~24h from now
        return (now_ts or time.time()) + 24 * 3600
