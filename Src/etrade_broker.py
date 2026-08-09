"""
E*TRADE BaseBroker adapter — equities/ETFs only (CORE / Breakouts stocks & ETFs).

No crypto trading path: supports_crypto=False; buy/sell reject crypto tickers/asset types;
orders use equity XML via ETradeClient.preview/place_equity_order only.
(Andrew retail schedule lists crypto at 0.50% — unused by this autotrader.)

Live order placement is gated by credentials['live_trading_enabled'] (default False).
Sandbox environment may place against apisb sample responses for integration testing.
"""
from __future__ import annotations

import math
import time
import uuid
from decimal import Decimal, ROUND_DOWN

from broker import BaseBroker
from etrade_client import ETradeClient, ETradeAPIError, midnight_et_epoch

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None

try:
    from broker import yf as _yf_lazy
except Exception:  # pragma: no cover
    _yf_lazy = None

KEYRING_SERVICE = "MarketAdvisor.ETrade"
FRACTIONAL_DECIMALS = 3
MIN_EQUITY_NOTIONAL = 5.0


def _keyring_set(name, value):
    if keyring is None or not name:
        return False
    try:
        if value:
            keyring.set_password(KEYRING_SERVICE, name, str(value))
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        return True
    except Exception:
        return False


def _keyring_get(name):
    if keyring is None or not name:
        return ""
    try:
        return keyring.get_password(KEYRING_SERVICE, name) or ""
    except Exception:
        return ""


def store_etrade_secret(kind, environment, value):
    """kind: consumer_secret | access_token | access_token_secret | request_token | request_token_secret"""
    return _keyring_set(f"{environment}:{kind}", value)


def load_etrade_secret(kind, environment):
    return _keyring_get(f"{environment}:{kind}")


def clear_pending_oauth(environment):
    """Drop mid-flow OAuth request token pair (Authorize → Complete)."""
    for kind in ("request_token", "request_token_secret"):
        _keyring_set(f"{environment}:{kind}", "")


def clear_etrade_secrets(environment):
    for kind in (
        "consumer_secret",
        "access_token",
        "access_token_secret",
        "request_token",
        "request_token_secret",
    ):
        _keyring_set(f"{environment}:{kind}", "")


_MISSING_REQUEST_TOKEN_MSG = (
    "OAuth request token missing or expired. "
    "Click Authorize in Browser again, then paste the NEW verification code "
    "(do not reuse an old code; keep the same Environment)."
)


def round_fractional_qty(qty):
    """E*TRADE fractional equities: up to 3 decimal places, round down."""
    d = Decimal(str(qty)).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    return float(d)


def qty_for_notional(dollars, price, allow_fractional=True):
    dollars = float(dollars)
    price = float(price)
    if price <= 0 or dollars <= 0:
        return 0.0
    raw = dollars / price
    if allow_fractional:
        return round_fractional_qty(raw)
    return float(math.floor(raw))


def build_equity_order_xml(
    *,
    client_order_id,
    symbol,
    order_action,
    quantity,
    price_type="MARKET",
    limit_price=None,
    order_term="GOOD_FOR_DAY",
    market_session="REGULAR",
    preview_id=None,
):
    """Build PreviewOrderRequest / PlaceOrderRequest XML body."""
    symbol = str(symbol).upper().replace("-USD", "")
    qty = f"{float(quantity):.3f}".rstrip("0").rstrip(".") if float(quantity) % 1 else str(int(float(quantity)))
    limit_xml = f"<limitPrice>{float(limit_price):.2f}</limitPrice>" if limit_price is not None else "<limitPrice></limitPrice>"
    preview_xml = ""
    if preview_id is not None:
        preview_xml = f"<PreviewIds><previewId>{int(preview_id)}</previewId></PreviewIds>"
    root = "PlaceOrderRequest" if preview_id is not None else "PreviewOrderRequest"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<{root}>"
        "<orderType>EQ</orderType>"
        f"<clientOrderId>{client_order_id}</clientOrderId>"
        f"{preview_xml}"
        "<Order>"
        "<allOrNone>false</allOrNone>"
        f"<priceType>{price_type}</priceType>"
        f"<orderTerm>{order_term}</orderTerm>"
        f"<marketSession>{market_session}</marketSession>"
        "<stopPrice></stopPrice>"
        f"{limit_xml}"
        "<Instrument>"
        "<Product>"
        "<securityType>EQ</securityType>"
        f"<symbol>{symbol}</symbol>"
        "</Product>"
        f"<orderAction>{order_action}</orderAction>"
        "<quantityType>QUANTITY</quantityType>"
        f"<quantity>{qty}</quantity>"
        "</Instrument>"
        "</Order>"
        f"</{root}>"
    )


def _is_ira_account(acc: dict) -> bool:
    blob = " ".join(
        str(acc.get(k) or "")
        for k in ("accountType", "accountMode", "accountDesc", "accountName", "institutionType")
    ).upper()
    return any(x in blob for x in ("IRA", "ROTH", "SEP", "SIMPLE_IRA", "BENF"))


def label_account(acc: dict) -> str:
    name = acc.get("accountName") or acc.get("accountDesc") or acc.get("accountId") or "Account"
    aid = acc.get("accountId") or ""
    kind = "IRA/Retirement" if _is_ira_account(acc) else "Brokerage"
    mode = acc.get("accountMode") or ""
    bits = [str(name), kind]
    if aid:
        bits.append(f"#{aid}")
    if mode:
        bits.append(str(mode))
    return " · ".join(bits)


def prefer_taxable_account(accounts):
    if not accounts:
        return None
    taxable = [a for a in accounts if not _is_ira_account(a)]
    pool = taxable or accounts
    return pool[0]


class ETradeAdapter(BaseBroker):
    """Equities/ETFs via E*TRADE Developer Platform."""

    def __init__(self):
        super().__init__()
        self.broker_id = "ETRADE"
        self.supports_equities = True
        self.supports_crypto = False
        self.supports_fractional_equities = True
        self.supports_extended_hours = False  # deferred phase
        self.supports_options = False
        self.supports_protective_stops = False
        self.requires_daily_reauth = True
        self.min_equity_notional = MIN_EQUITY_NOTIONAL
        self.client = None
        self.environment = "sandbox"
        self.account_id_key = None
        self.accounts = []
        self.token_expires_at = None
        self.live_trading_enabled = False
        self.consumer_key = ""
        self._last_order_meta = {}
        # Survives dialog close + login() recreating ETradeClient between Authorize and Complete.
        self._pending_request_token = None
        self._pending_request_token_secret = None

    def _save_pending_oauth(self, env, token, token_secret):
        self._pending_request_token = token
        self._pending_request_token_secret = token_secret
        store_etrade_secret("request_token", env, token or "")
        store_etrade_secret("request_token_secret", env, token_secret or "")

    def _clear_pending_oauth(self, env=None):
        self._pending_request_token = None
        self._pending_request_token_secret = None
        clear_pending_oauth(env or self.environment)

    def _resolve_pending_oauth(self, env, credentials=None):
        """Request token pair from in-memory client/adapter, then Credential Manager."""
        credentials = credentials or {}
        client = self.client
        token = (
            (client.request_token if client else None)
            or self._pending_request_token
            or credentials.get("request_token")
            or load_etrade_secret("request_token", env)
            or ""
        )
        secret = (
            (client.request_token_secret if client else None)
            or self._pending_request_token_secret
            or credentials.get("request_token_secret")
            or load_etrade_secret("request_token_secret", env)
            or ""
        )
        return (str(token).strip() or None), (str(secret).strip() or None)

    # --------------------------------------------------------------- auth
    def login(self, credentials):
        """
        credentials keys:
          environment, consumer_key, consumer_secret,
          verifier (complete OAuth), access_token/secret (restore),
          account_id_key, live_trading_enabled,
          start_oauth (bool) — only fetch request token + auth URL
        """
        # Stay disconnected until a full login succeeds (OAuth URL step is not a session).
        self.is_connected = False
        try:
            env = "live" if str(credentials.get("environment", "sandbox")).lower() == "live" else "sandbox"
            # Capture pending OAuth before env/client swap (Authorize → Complete).
            prior_rt, prior_rts = self._resolve_pending_oauth(self.environment, credentials)
            if env != self.environment:
                # Env switch invalidates the in-flight authorize session for the old env.
                self._clear_pending_oauth(self.environment)
                prior_rt, prior_rts = self._resolve_pending_oauth(env, credentials)
            self.environment = env
            self.live_trading_enabled = bool(credentials.get("live_trading_enabled", False))
            key = (credentials.get("consumer_key") or "").strip()
            secret = (credentials.get("consumer_secret") or "").strip()
            if not secret:
                secret = load_etrade_secret("consumer_secret", env)
            if not key or not secret:
                return False, "Missing E*TRADE consumer key/secret"

            self.consumer_key = key
            store_etrade_secret("consumer_secret", env, secret)
            # Always rebuild client (fresh signing material) but restore pending request tokens.
            self.client = ETradeClient(key, secret, environment=env)
            if prior_rt and prior_rts and not credentials.get("start_oauth"):
                self.client.request_token = prior_rt
                self.client.request_token_secret = prior_rts

            if credentials.get("start_oauth"):
                token, req_secret = self.client.get_request_token()
                self._save_pending_oauth(env, token, req_secret)
                url = self.client.authorization_url(token)
                return True, f"AUTH_URL::{url}"

            verifier = (credentials.get("verifier") or "").strip()
            if verifier:
                rt, rts = self._resolve_pending_oauth(env, credentials)
                if not rt or not rts:
                    return False, _MISSING_REQUEST_TOKEN_MSG
                try:
                    self.client.get_access_token(verifier, request_token=rt, request_token_secret=rts)
                except ETradeAPIError as e:
                    if "Missing request token" in str(e):
                        return False, _MISSING_REQUEST_TOKEN_MSG
                    raise
                self._clear_pending_oauth(env)
                store_etrade_secret("access_token", env, self.client.access_token)
                store_etrade_secret("access_token_secret", env, self.client.access_token_secret)
                self.token_expires_at = midnight_et_epoch()
            else:
                at = credentials.get("access_token") or load_etrade_secret("access_token", env)
                ats = credentials.get("access_token_secret") or load_etrade_secret("access_token_secret", env)
                if not at or not ats:
                    return False, "No access token — authorize in browser and enter verification code"
                self.client.set_access_token(at, ats)
                # Probe / renew
                try:
                    self.accounts = self.client.list_accounts()
                except ETradeAPIError as e:
                    if e.status_code in (401, 403) or "inactive" in str(e).lower():
                        try:
                            self.client.renew_access_token()
                            store_etrade_secret("access_token", env, self.client.access_token)
                            store_etrade_secret("access_token_secret", env, self.client.access_token_secret)
                            self.accounts = self.client.list_accounts()
                        except Exception as renew_err:
                            return False, f"Reauthorization required ({renew_err})"
                    else:
                        return False, str(e)
                self.token_expires_at = float(credentials.get("token_expires_at") or midnight_et_epoch())

            if not self.accounts:
                self.accounts = self.client.list_accounts()

            selected = credentials.get("account_id_key")
            if selected:
                self.account_id_key = selected
            else:
                pref = prefer_taxable_account(self.accounts)
                self.account_id_key = (pref or {}).get("accountIdKey")

            if not self.account_id_key:
                return False, "No brokerage account found on this E*TRADE login"

            # Final probe
            self.client.get_balance(self.account_id_key)
            self.is_connected = True
            store_etrade_secret("access_token", env, self.client.access_token)
            store_etrade_secret("access_token_secret", env, self.client.access_token_secret)
            return True, "Success"
        except Exception as e:
            self.is_connected = False
            return False, str(e)

    def logout(self):
        try:
            if self.client:
                self.client.revoke_access_token()
        except Exception:
            pass
        self._clear_pending_oauth(self.environment)
        clear_etrade_secrets(self.environment)
        self.client = None
        self.is_connected = False
        self.account_id_key = None
        self.accounts = []

    def ensure_session(self):
        """Renew if idle-inactive; return (ok, message)."""
        if not self.client or not self.client.access_token:
            return False, "Not connected"
        if self.token_expires_at and time.time() > float(self.token_expires_at):
            self.is_connected = False
            return False, "Access token expired at midnight ET — reauthorize"
        try:
            self.client.get_balance(self.account_id_key)
            return True, "ok"
        except ETradeAPIError:
            try:
                self.client.renew_access_token()
                store_etrade_secret("access_token", self.environment, self.client.access_token)
                store_etrade_secret("access_token_secret", self.environment, self.client.access_token_secret)
                self.client.get_balance(self.account_id_key)
                return True, "renewed"
            except Exception as e:
                self.is_connected = False
                return False, f"Reauthorization required ({e})"

    def list_account_choices(self):
        return [(a.get("accountIdKey"), label_account(a), _is_ira_account(a)) for a in (self.accounts or [])]

    # ----------------------------------------------------------- account
    def get_account_balances(self):
        if not self.is_connected or not self.client or not self.account_id_key:
            return 0.0, 0.0
        try:
            data = self.client.get_balance(self.account_id_key)
            return parse_etrade_balances(data)
        except Exception:
            # Propagate so GUI keeps last-good equity (do not invent $0).
            raise

    def get_current_holdings(self):
        """Return list of holding dicts (same shape as Robinhood/Coinbase adapters)."""
        if not self.is_connected or not self.client or not self.account_id_key:
            return []
        try:
            data = self.client.get_portfolio(self.account_id_key)
            return normalize_etrade_holdings(data)
        except Exception:
            return []

    def get_live_price(self, ticker, allow_yahoo_fallback=True):
        clean = str(ticker).upper().replace("-USD", "")
        if self.is_connected and self.client:
            try:
                data = self.client.get_quotes(clean)
                px = parse_etrade_quote_price(data, clean)
                if px > 0:
                    return px
            except Exception:
                pass
        if allow_yahoo_fallback:
            try:
                import yfinance as yf
                t = yf.Ticker(clean)
                px = t.fast_info.last_price if hasattr(t, "fast_info") else None
                if px and float(px) > 0:
                    return float(px)
                hist = t.history(period="1d")
                if hist is not None and len(hist):
                    return float(hist["Close"].iloc[-1])
            except Exception:
                pass
        return 0.0

    # ------------------------------------------------------------- orders
    def _orders_allowed(self):
        if self.environment == "sandbox":
            return True, ""
        if self.live_trading_enabled:
            return True, ""
        return False, (
            "E*TRADE live trading is disabled (sandbox-first guard). "
            "Enable etrade_live_trading in Settings after read-only validation."
        )

    def _reject_crypto(self, ticker, asset_type):
        at = str(asset_type or "").upper()
        if "CRYPTO" in at:
            return True
        # Known crypto tickers should never route here from scheduler, but belt+suspenders
        from scoring import CRYPTO_TICKERS
        clean = str(ticker).upper().replace("-USD", "")
        return clean in CRYPTO_TICKERS

    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
                        market_hours="regular_hours", allow_fractional=True):
        if self._reject_crypto(ticker, asset_type):
            return "E*TRADE does not support crypto via API", 0.0, None
        ok, reason = self._orders_allowed()
        if not ok:
            return reason, 0.0, None
        if not self.is_connected or not self.client or not self.account_id_key:
            return "E*TRADE not connected", 0.0, None

        price = float(price or 0) or self.get_live_price(ticker)
        dollars = float(trade_dollars or 0)
        if price <= 0 or dollars <= 0:
            return "Invalid price/size", 0.0, None
        if dollars + 1e-9 < self.min_equity_notional:
            return f"Below E*TRADE ${self.min_equity_notional:.0f} minimum", 0.0, None

        frac = bool(allow_fractional and self.supports_fractional_equities)
        qty = qty_for_notional(dollars, price, allow_fractional=frac)
        if qty <= 0:
            return "Quantity rounds to zero", 0.0, None
        if qty < 1.0 and not frac:
            return "Fractional shares required for this size", 0.0, None

        # Sub-1 share must be MARKET + day + regular session on E*TRADE
        if qty < 1.0:
            price_type = "MARKET"
            limit_price = None
            market_session = "REGULAR"
        else:
            use_limit = float(offset_pct or 0) > 0
            if use_limit:
                price_type = "LIMIT"
                limit_price = round(price * (1.0 + float(offset_pct) / 100.0), 2)
            else:
                price_type = "MARKET"
                limit_price = None
            market_session = "EXTENDED" if (use_ext_hours and self.supports_extended_hours) else "REGULAR"

        client_order_id = uuid.uuid4().hex[:18]
        try:
            preview_xml = build_equity_order_xml(
                client_order_id=client_order_id,
                symbol=ticker,
                order_action="BUY",
                quantity=qty,
                price_type=price_type,
                limit_price=limit_price,
                market_session=market_session,
            )
            preview = self.client.preview_equity_order(self.account_id_key, preview_xml)
            preview_id = _extract_preview_id(preview)
            if preview_id is None:
                return f"Preview failed: {preview}", 0.0, None
            place_xml = build_equity_order_xml(
                client_order_id=client_order_id,
                symbol=ticker,
                order_action="BUY",
                quantity=qty,
                price_type=price_type,
                limit_price=limit_price,
                market_session=market_session,
                preview_id=preview_id,
            )
            placed = self.client.place_equity_order(self.account_id_key, place_xml)
            order_id = _extract_order_id(placed)
            spent = round(qty * price, 4)
            self._last_order_meta[str(order_id)] = {"side": "BUY", "qty": qty, "symbol": str(ticker).upper()}
            return f"E*TRADE Buy submitted ({price_type} {qty} {str(ticker).upper()})", spent, order_id
        except Exception as e:
            return f"E*TRADE buy error: {e}", 0.0, None

    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                         market_hours="regular_hours", allow_fractional=True, sell_all=False):
        if self._reject_crypto(ticker, asset_type):
            return "E*TRADE does not support crypto via API", None
        ok, reason = self._orders_allowed()
        if not ok:
            return reason, None
        if not self.is_connected or not self.client or not self.account_id_key:
            return "E*TRADE not connected", None

        # No native sell-all / close-position in E*TRADE equity XML — refresh live qty on full exit.
        if sell_all:
            try:
                sym = str(ticker).replace("-USD", "").upper()
                for h in self.get_current_holdings() or []:
                    if str(h.get("ticker") or "").upper() == sym:
                        live = float(h.get("shares") or 0)
                        if live > 0:
                            shares_val = live
                        break
            except Exception:
                pass

        qty = round_fractional_qty(shares_val) if allow_fractional else float(math.floor(float(shares_val)))
        if qty <= 0:
            return "Nothing to sell", None
        price = float(price or 0) or self.get_live_price(ticker)

        if qty < 1.0:
            price_type = "MARKET"
            limit_price = None
            market_session = "REGULAR"
        else:
            use_limit = float(offset_pct or 0) > 0
            if use_limit:
                price_type = "LIMIT"
                limit_price = round(price * (1.0 - float(offset_pct) / 100.0), 2)
            else:
                price_type = "MARKET"
                limit_price = None
            market_session = "EXTENDED" if (use_ext_hours and self.supports_extended_hours) else "REGULAR"

        client_order_id = uuid.uuid4().hex[:18]
        try:
            preview_xml = build_equity_order_xml(
                client_order_id=client_order_id,
                symbol=ticker,
                order_action="SELL",
                quantity=qty,
                price_type=price_type,
                limit_price=limit_price,
                market_session=market_session,
            )
            preview = self.client.preview_equity_order(self.account_id_key, preview_xml)
            preview_id = _extract_preview_id(preview)
            if preview_id is None:
                return f"Preview failed: {preview}", None
            place_xml = build_equity_order_xml(
                client_order_id=client_order_id,
                symbol=ticker,
                order_action="SELL",
                quantity=qty,
                price_type=price_type,
                limit_price=limit_price,
                market_session=market_session,
                preview_id=preview_id,
            )
            placed = self.client.place_equity_order(self.account_id_key, place_xml)
            order_id = _extract_order_id(placed)
            self._last_order_meta[str(order_id)] = {"side": "SELL", "qty": qty, "symbol": str(ticker).upper()}
            label = "Sell-All" if sell_all else "Sell"
            return f"E*TRADE {label} submitted ({price_type} {qty} {str(ticker).upper()})", order_id
        except Exception as e:
            return f"E*TRADE sell error: {e}", None

    def confirm_order(self, order_id, is_crypto=False, timeout_sec=10):
        if is_crypto:
            return False, "crypto unsupported"
        if not self.client or not self.account_id_key:
            return False, "not connected"
        self._last_fill_fee = None
        deadline = time.time() + max(3, int(timeout_sec))
        while time.time() < deadline:
            try:
                data = self.client.list_orders(self.account_id_key, status=None)
                # Search OPEN + recent — sandbox shapes vary
                if _order_filled(data, order_id):
                    try:
                        from analytics import extract_fee_dollars_from_order
                        fee = _extract_etrade_order_fee(data, order_id)
                        if fee is None:
                            fee = extract_fee_dollars_from_order(data)
                        self._last_fill_fee = fee
                    except Exception:
                        self._last_fill_fee = _extract_etrade_order_fee(data, order_id)
                    return True, "FILLED"
                if _order_terminal_reject(data, order_id):
                    return False, "REJECTED"
            except Exception as e:
                return False, str(e)
            time.sleep(1.0)
        return False, "PENDING"

    def cancel_order(self, order_id, is_crypto=False):
        if is_crypto:
            return False, "crypto unsupported"
        if not self.client or not self.account_id_key:
            return False, "not connected"
        try:
            self.client.cancel_order(self.account_id_key, order_id)
            return True, "cancelled"
        except Exception as e:
            return False, str(e)

    def position_is_dust(self, ticker, shares, price, asset_type=""):
        try:
            notional = float(shares) * float(price)
            if notional < 1.0:
                return True, f"below $1 notional (${notional:.4f})"
        except Exception:
            return True, "invalid size/price"
        return False, ""


def _f(v):
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except Exception:
        return 0.0


def _as_dict(v):
    """XML/JSON leaves are often strings — never call .get on a non-dict."""
    return v if isinstance(v, dict) else {}


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    # Scalar / string from malformed XML — not iterable as records
    return []


def normalize_etrade_holdings(data):
    """
    Parse portfolio payload into RH/CB-shaped list:
      [{'ticker', 'shares', 'price', 'cost', 'type', 'equity'}, ...]
    """
    holdings = []
    if not isinstance(data, dict):
        return holdings
    root = _as_dict(data.get("PortfolioResponse") or data.get("portfolioResponse") or data)
    accounts = _as_list(root.get("AccountPortfolio") or root.get("accountPortfolio"))
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        positions = _as_list(acct.get("Position") or acct.get("position"))
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            product_raw = pos.get("Product") or pos.get("product")
            if isinstance(product_raw, str) and product_raw.strip():
                # XML leaf: <Product>AAPL</Product> (no nested symbol)
                sym = product_raw.strip().upper()
                product = {}
            else:
                product = _as_dict(product_raw)
                sym = str(product.get("symbol") or pos.get("symbol") or "").upper()
            if not sym:
                continue
            qty = _f(pos.get("quantity") or pos.get("qty") or 0)
            if qty <= 0:
                continue
            quick = _as_dict(pos.get("Quick") or pos.get("quick"))
            price = (
                _f(quick.get("lastTrade"))
                or _f(pos.get("price"))
                or _f(pos.get("marketValue", 0) / qty if qty else 0)
            )
            cost = _f(pos.get("costPerShare") or pos.get("pricePaid") or price)
            holdings.append({
                "ticker": sym,
                "shares": qty,
                "price": price,
                "cost": cost,
                "type": "stock",
                "equity": _f(pos.get("marketValue")) or (qty * price),
            })
    return holdings


def parse_etrade_balances(data):
    """Return (equity, buying_power) from a balance payload; safe on odd XML shapes.

    Sandbox payloads often omit CashBuyingPower or nest BP under alternate keys
    (marginBuyingPower, settledCash, cashBalance). Live accounts usually have
    Computed.CashBuyingPower. When every BP field is missing/zero, callers should
    surface Sandbox / no BP UX rather than treating $0 as real buying power.
    """
    if not isinstance(data, dict):
        return 0.0, 0.0
    bal = _as_dict(data.get("BalanceResponse") or data.get("balanceResponse") or data)
    computed = _as_dict(bal.get("Computed") or bal.get("computed"))
    rt = _as_dict(bal.get("RealTimeValues") or bal.get("realTimeValues"))
    if not rt:
        rt = _as_dict(computed.get("RealTimeValues") or computed.get("realTimeValues"))
    bp_details = _as_dict(
        computed.get("BuyingPowerDetails")
        or computed.get("buyingPowerDetails")
        or bal.get("BuyingPowerDetails")
        or bal.get("buyingPowerDetails")
    )
    cash = _as_dict(bal.get("Cash") or bal.get("cash") or computed.get("Cash") or computed.get("cash"))

    equity = (
        _f(rt.get("totalAccountValue") or rt.get("TotalAccountValue"))
        or _f(bal.get("accountBalance") or bal.get("AccountBalance"))
        or _f(computed.get("accountBalance"))
        or 0.0
    )
    # Prefer cash/settled BP; fall back through common sandbox + margin aliases
    bp_candidates = [
        computed.get("CashBuyingPower"), computed.get("cashBuyingPower"),
        computed.get("MarginBuyingPower"), computed.get("marginBuyingPower"),
        computed.get("BuyingPower"), computed.get("buyingPower"),
        bp_details.get("cashBuyingPower"), bp_details.get("CashBuyingPower"),
        bp_details.get("marginBuyingPower"), bp_details.get("MarginBuyingPower"),
        bp_details.get("buyingPower"), bp_details.get("BuyingPower"),
        bal.get("cashBuyingPower"), bal.get("CashBuyingPower"),
        bal.get("buyingPower"), bal.get("BuyingPower"),
        cash.get("fundsForTrading"), cash.get("settledCash"), cash.get("cashAvailableForInvestment"),
        cash.get("moneyMktBalance"), computed.get("settledCash"), computed.get("SettledCash"),
        computed.get("cashBalance"), computed.get("CashBalance"),
        rt.get("totalCash"), rt.get("netMv"),
    ]
    bp = 0.0
    for c in bp_candidates:
        v = _f(c)
        if v > 0:
            bp = v
            break
    if equity <= 0:
        equity = (
            _f(computed.get("CashBuyingPower") or computed.get("cashBuyingPower"))
            or bp
            or 0.0
        )
    if equity <= 0 and bp > 0:
        equity = bp
    return float(equity), float(bp)


def parse_etrade_quote_price(data, symbol=None):
    """Extract last trade from a quote payload; 0.0 if shape is wrong."""
    if not isinstance(data, dict):
        return 0.0
    qroot = _as_dict(data.get("QuoteResponse") or data.get("quoteResponse") or data)
    quotes = _as_list(qroot.get("QuoteData") or qroot.get("quoteData"))
    want = str(symbol or "").upper()
    for q in quotes:
        if not isinstance(q, dict):
            continue
        product = _as_dict(q.get("Product") or q.get("product"))
        sym = str(product.get("symbol") or q.get("symbol") or "").upper()
        if want and sym and sym != want:
            continue
        all_q = _as_dict(q.get("All") or q.get("all"))
        intraday = _as_dict(q.get("Intraday") or q.get("intraday"))
        px = (
            _f(all_q.get("lastTrade"))
            or _f(all_q.get("lastTradePrice"))
            or _f(q.get("lastTrade"))
            or _f(intraday.get("lastTrade"))
        )
        if px > 0:
            return px
    return 0.0


def _extract_preview_id(data):
    if not data:
        return None
    # Walk common shapes
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in ("previewid", "preview_id") and v not in (None, ""):
                    try:
                        return int(v)
                    except Exception:
                        pass
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _extract_order_id(data):
    if not data:
        return None
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in ("orderid", "order_id") and v not in (None, ""):
                    return str(v)
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return f"et-{uuid.uuid4().hex[:10]}"


def _extract_etrade_order_fee(data, order_id):
    """Pull estimatedCommission / estimatedFees from a matching ET order node."""
    oid = str(order_id)
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if str(node.get("orderId") or node.get("order_id") or "") == oid:
                for key in (
                    "estimatedCommission",
                    "estimatedFees",
                    "commission",
                    "Commission",
                    "fees",
                    "fee",
                ):
                    if node.get(key) is None:
                        continue
                    try:
                        val = float(node.get(key))
                        if val >= 0:
                            return val
                    except (TypeError, ValueError):
                        continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _order_filled(data, order_id):
    oid = str(order_id)
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if str(node.get("orderId") or node.get("order_id") or "") == oid:
                st = str(node.get("orderStatus") or node.get("status") or "").upper()
                if "FILL" in st or st in ("EXECUTED", "DONE_TRADE_EXECUTED"):
                    return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _order_terminal_reject(data, order_id):
    oid = str(order_id)
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if str(node.get("orderId") or node.get("order_id") or "") == oid:
                st = str(node.get("orderStatus") or node.get("status") or "").upper()
                if any(x in st for x in ("REJECT", "CANCEL", "EXPIRED")):
                    return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False
