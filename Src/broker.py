import os
import time
import math
import random
import pickle
import importlib
from decimal import Decimal, ROUND_DOWN, ROUND_UP


# Coinbase Advanced REST: mirror etrade_client gap + retry (no OAuth inventing).
_CB_MIN_REQUEST_GAP_SEC = 0.35
_CB_MAX_RETRIES = 4


def _as_dict(v):
    """API leaves are often strings/None — never call .get on a non-dict."""
    return v if isinstance(v, dict) else {}


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []


def _money_value(nested, default=0.0):
    """Extract float from Coinbase {value, currency} blobs or raw number/string."""
    if nested is None:
        return float(default)
    if isinstance(nested, dict):
        try:
            return float(nested.get("value", default) or default)
        except (TypeError, ValueError):
            return float(default)
    try:
        return float(nested)
    except (TypeError, ValueError):
        return float(default)


def _cb_response_dict(res):
    """Normalize SDK objects / odd payloads to a plain dict."""
    if res is None:
        return {}
    if hasattr(res, "to_dict"):
        try:
            res = res.to_dict()
        except Exception:
            return {}
    if isinstance(res, dict):
        return res
    if isinstance(res, list):
        return {"accounts": res}
    return {}


class _LazyModule:
    """Import a heavy module on first attribute access (keeps startup fast)."""

    def __init__(self, module_name):
        self._module_name = module_name
        self._mod = None

    def _load(self):
        if self._mod is None:
            self._mod = importlib.import_module(self._module_name)
        return self._mod

    def __getattr__(self, name):
        return getattr(self._load(), name)


r = _LazyModule("robin_stocks.robinhood")
yf = _LazyModule("yfinance")

_RESTClient = None
_coinbase_checked = False
COINBASE_AVAILABLE = False  # updated on first Coinbase login attempt


def _get_rest_client_class():
    global _RESTClient, _coinbase_checked, COINBASE_AVAILABLE
    if not _coinbase_checked:
        _coinbase_checked = True
        try:
            from coinbase.rest import RESTClient as _RC
            _RESTClient = _RC
            COINBASE_AVAILABLE = True
        except ImportError:
            _RESTClient = None
            COINBASE_AVAILABLE = False
    return _RESTClient

class BaseBroker:
    """The master interface that all broker adapters must strictly follow."""
    def __init__(self):
        self.is_connected = False
        self.broker_id = "BASE"
        # Declarative capabilities — GUI scheduler / arming / scanners must honor these.
        self.supports_equities = True
        self.supports_crypto = False
        self.supports_fractional_equities = True
        self.supports_extended_hours = False
        self.supports_options = False
        self.supports_protective_stops = False
        self.requires_daily_reauth = False
        self.min_equity_notional = 1.0

    def login(self, credentials): raise NotImplementedError
    def logout(self): raise NotImplementedError
    def get_account_balances(self): raise NotImplementedError
    def get_current_holdings(self): raise NotImplementedError
    def get_live_price(self, ticker, allow_yahoo_fallback=True): raise NotImplementedError
    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
                        market_hours="regular_hours", allow_fractional=True): raise NotImplementedError
    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                         market_hours="regular_hours", allow_fractional=True, sell_all=False):
        raise NotImplementedError
    def confirm_order(self, order_id, is_crypto=False, timeout_sec=10):
        return False, "unsupported"
    def cancel_order(self, order_id, is_crypto=False):
        """Cancel an open order by id. Returns (ok, message)."""
        return False, "unsupported"
    def place_protective_stop(self, ticker, asset_type, quantity, entry_price, stop_pct,
                              trail_pct=None):
        """
        Attach broker-side protective sell after a buy fill.
        Returns (ok: bool, order_id or None, message).
        Paper / unsupported brokers should no-op with a clear message.
        """
        return False, None, "protective stops unsupported"
    def position_is_dust(self, ticker, shares, price, asset_type=""):
        """Return (is_dust, reason). Default: under $1 notional."""
        try:
            notional = float(shares) * float(price)
            if notional < 1.0:
                return True, f"below $1 notional (${notional:.4f})"
        except Exception:
            return True, "invalid size/price"
        return False, ""

# Per-unit cost below this fraction of mark ⇒ dust / bogus RH cost_bases (unknown).
COST_BASIS_DUST_FRAC = 0.01


def cost_basis_is_dust(cost, mark_price, *, min_frac=COST_BASIS_DUST_FRAC):
    """True when cost is missing or absurdly small vs live mark (fake mega-ROI)."""
    try:
        c = float(cost or 0.0)
        px = float(mark_price or 0.0)
    except (TypeError, ValueError):
        return True
    if c <= 0:
        return True
    if px > 0 and c < px * float(min_frac):
        return True
    return False


def usable_avg_cost(cost, mark_price, *, min_frac=COST_BASIS_DUST_FRAC):
    """Return cost when sane vs mark; else 0.0 (unknown — do not invent ROI)."""
    try:
        c = float(cost or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if cost_basis_is_dust(c, mark_price, min_frac=min_frac):
        return 0.0
    return c

def _get_seeded_cost(broker_id, ticker):
    """Tier 4: Last-known (Seed) fallback for missing crypto cost basis."""
    try:
        import cost_basis as cb_mod
        return float(cb_mod.seed_lookup(broker_id, ticker) or 0.0)
    except Exception:
        return 0.0

def _rh_pos_mark_price(pos):
    """Best-effort mark from a RH crypto position payload (no network)."""
    if not isinstance(pos, dict):
        return 0.0

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    for key in (
        "mark_price", "markPrice",
        "current_price", "currentPrice",
        "last_trade_price", "lastTradePrice",
    ):
        raw = pos.get(key)
        if isinstance(raw, dict):
            v = _f(raw.get("amount") or raw.get("value"))
        else:
            v = _f(raw)
        if v > 0:
            return v
    return 0.0


def _rh_parse_money_total(raw):
    """Parse RH money leaf (string/number/{amount|value}) to float; 0 if missing."""
    if raw is None:
        return 0.0
    if isinstance(raw, dict):
        try:
            return float(raw.get("amount") or raw.get("value") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _rh_cost_bases_list_total(cb_list):
    """Sum direct/marked cost totals from cost_bases[] entries."""
    if not isinstance(cb_list, list) or not cb_list:
        return 0.0
    total = 0.0
    found = False
    for entry in cb_list:
        if not isinstance(entry, dict):
            continue
        entry_hit = False
        for key in (
            "direct_cost_basis", "directCostBasis",
            "cost_basis", "costBasis",
            "marked_cost_basis", "markedCostBasis",
            "intraday_cost_basis", "intradayCostBasis",
        ):
            v = _rh_parse_money_total(entry.get(key))
            if v > 0:
                total += v
                found = True
                entry_hit = True
                break
        if entry_hit:
            continue
        for nest_key in ("direct_cost_basis", "cost_basis", "amount"):
            nested = entry.get(nest_key)
            if isinstance(nested, dict):
                v = _rh_parse_money_total(nested)
                if v > 0:
                    total += v
                    found = True
                    break
    return total if found else 0.0


def _rh_crypto_avg_cost(pos, qty, mark_price=None):
    """
    Per-unit avg cost from a Robinhood crypto position payload.

    Prefer cost_bases[].direct_cost_basis (total) / qty, then singular top-level
    cost_basis (robin_stocks documents this key), then per-unit fields.
    Empty cost_bases[] must NOT block singular cost_basis.

    Returns 0.0 when unknown or dust vs mark — never invents a price / mega-ROI basis.
    """
    try:
        qty = float(qty or 0.0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty <= 0 or not isinstance(pos, dict):
        return 0.0

    try:
        mark = float(mark_price) if mark_price is not None else 0.0
    except (TypeError, ValueError):
        mark = 0.0
    if mark <= 0:
        mark = _rh_pos_mark_price(pos)

    def _accept(per_unit):
        return usable_avg_cost(per_unit, mark)

    # 1) cost_bases list (may be empty — fall through)
    list_total = _rh_cost_bases_list_total(pos.get("cost_bases"))
    if list_total > 0:
        accepted = _accept(list_total / qty)
        if accepted > 0:
            return accepted

    # 2) Singular cost_basis (total USD) — RH holdings API / robin_stocks docs
    singular = pos.get("cost_basis")
    if isinstance(singular, list):
        st = _rh_cost_bases_list_total(singular)
        if st > 0:
            accepted = _accept(st / qty)
            if accepted > 0:
                return accepted
    elif isinstance(singular, dict):
        v = 0.0
        for key in (
            "direct_cost_basis", "directCostBasis",
            "cost_basis", "costBasis",
            "amount", "value",
        ):
            v = _rh_parse_money_total(singular.get(key))
            if v > 0:
                break
        if v <= 0:
            v = _rh_parse_money_total(singular)
        if v > 0:
            accepted = _accept(v / qty)
            if accepted > 0:
                return accepted
    elif singular is not None:
        v = _rh_parse_money_total(singular)
        if v > 0:
            accepted = _accept(v / qty)
            if accepted > 0:
                return accepted

    # 3) Explicit per-unit fields (do not invent from mark price)
    for key in (
        "average_cost", "averageCost",
        "average_buy_price", "averageBuyPrice",
        "avg_cost", "avgCost",
        "average_entry_price", "averageEntryPrice",
    ):
        v = _rh_parse_money_total(pos.get(key))
        if v > 0:
            accepted = _accept(v)
            if accepted > 0:
                return accepted

    # 4) Other total cost fields (divide by qty)
    for key in (
        "total_cost_basis", "totalCostBasis",
        "cost_basis_total", "costBasisTotal",
    ):
        v = _rh_parse_money_total(pos.get(key))
        if v > 0:
            accepted = _accept(v / qty)
            if accepted > 0:
                return accepted
    return 0.0


def _cb_position_avg_entry(pos):
    """
    Per-unit avg entry from a Coinbase portfolio spot_positions row.
    Prefers average_entry_price; else cost_basis / qty. Dust-filters vs implied mark.
    Returns (cost, mark) with cost=0 when unknown/dust.
    """
    if not isinstance(pos, dict) or pos.get("is_cash"):
        return 0.0, 0.0
    asset = str(pos.get("asset") or "").replace("-USD", "").upper().strip()
    if not asset or asset in ("USD", "USDC", "USDT", "DAI"):
        return 0.0, 0.0
    try:
        qty = float(pos.get("total_balance_crypto") or 0.0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty <= 0:
        try:
            qty = float(pos.get("available_to_trade_crypto") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
    entry = _money_value(pos.get("average_entry_price"))
    if entry <= 0 and qty > 0:
        total_basis = _money_value(pos.get("cost_basis"))
        if total_basis > 0:
            entry = total_basis / qty

    mark = 0.0
    try:
        fiat = float(pos.get("total_balance_fiat") or 0.0)
        if fiat > 0 and qty > 0:
            mark = fiat / qty
    except (TypeError, ValueError):
        mark = 0.0

    entry = usable_avg_cost(entry, mark)
    
    # --- 1.30.1 SEED FIX (COINBASE) ---
    if entry <= 0.0:
        seeded = _get_seeded_cost("COINBASE", asset)
        if seeded > 0:
            entry = seeded
            print(f"[Coinbase] Cost basis seeded for {asset}: ${entry}")
    # ----------------------------------
    
    return (entry, mark) if entry > 0 else (0.0, mark)

def robinhood_pickle_path(pickle_name=""):
    """Default robin_stocks session file: ~/.tokens/robinhood.pickle"""
    return os.path.join(os.path.expanduser("~"), ".tokens", f"robinhood{pickle_name}.pickle")


class RobinhoodAdapter(BaseBroker):
    """The Robinhood translation layer."""
    def __init__(self):
        super().__init__()
        self.broker_id = "ROBINHOOD"
        self.supports_equities = True
        self.supports_crypto = True
        self.supports_fractional_equities = True
        self.supports_extended_hours = True
        self.supports_protective_stops = True
        self.min_equity_notional = 1.0
        self._crypto_inc_cache = {}

    def login(self, credentials):
        """
        Connect with email/password (may prompt SMS 2FA via patched input), or
        restore-only with empty credentials (loads pickle — never interactive).

        Important: restore must load ~/.tokens/robinhood.pickle into robin_stocks'
        in-memory Authorization header. Calling load_account_profile() alone does
        nothing if no session was loaded this process.
        """
        try:
            email = (credentials.get("email") or "").strip()
            password = credentials.get("password") or ""
            store_session = credentials.get("store_session", True)

            if email and password:
                login_data = r.login(
                    username=email,
                    password=password,
                    store_session=store_session,
                )
                if login_data and "access_token" in login_data:
                    self.is_connected = True
                    return True, "Success"
                # Fall through: maybe tokens were set but return shape differed
            else:
                # Restore-only — never call r.login() without creds (it prompts
                # username/password via input/getpass when pickle is missing/expired).
                ok, detail = self._restore_from_pickle()
                if ok:
                    self.is_connected = True
                    return True, detail
                return False, detail

            profile = r.profiles.load_account_profile()
            if profile and "account_number" in profile:
                self.is_connected = True
                return True, "Session Verified"
            return False, "Invalid Credentials"
        except Exception as e:
            self.is_connected = False
            return False, str(e)

    def _restore_from_pickle(self, pickle_name=""):
        """
        Load robin_stocks pickle into the live session and verify with a cheap API call.
        Returns (ok, detail). Never prompts for password or 2FA.
        """
        path = robinhood_pickle_path(pickle_name)
        if not os.path.isfile(path):
            return False, "no saved session"

        try:
            with open(path, "rb") as f:
                pickle_data = pickle.load(f)
            access_token = pickle_data["access_token"]
            token_type = pickle_data["token_type"]
        except Exception as e:
            return False, f"saved session unreadable ({e})"

        try:
            from robin_stocks.robinhood.helper import (
                set_login_state,
                update_session,
                request_get,
            )
            from robin_stocks.robinhood.urls import positions_url

            set_login_state(True)
            update_session("Authorization", f"{token_type} {access_token}")
            # Same validity check robin_stocks.login uses when loading pickle
            res = request_get(
                positions_url(),
                "pagination",
                {"nonzero": "true"},
                jsonify_data=False,
            )
            if res is None:
                raise RuntimeError("session check returned no response")
            res.raise_for_status()

            profile = r.profiles.load_account_profile()
            if not (isinstance(profile, dict) and profile.get("account_number")):
                raise RuntimeError("account profile unavailable")
            return True, "Session Verified"
        except Exception:
            try:
                from robin_stocks.robinhood.helper import set_login_state, update_session
                set_login_state(False)
                update_session("Authorization", None)
            except Exception:
                pass
            return False, "saved session expired"

    def logout(self):
        try: r.logout()
        except Exception: pass
        self.is_connected = False

    def get_account_balances(self):
        if not self.is_connected:
            raise RuntimeError("Robinhood not connected")
        # Do NOT swallow API failures as $0,$0 — that fake-trips day-loss limits upstream.
        acc = r.profiles.load_account_profile()
        if not isinstance(acc, dict):
            raise RuntimeError("Robinhood account profile unavailable")
        portfolio_value = float(acc.get('portfolio_equity', 0) or acc.get('equity', 0) or 0.0)
        cash = max(float(acc.get('buying_power', 0) or 0), float(acc.get('cash', 0) or 0))

        if portfolio_value == 0.0:
            holdings = r.build_holdings()
            if isinstance(holdings, dict):
                for t, d in holdings.items():
                    if not isinstance(d, dict):
                        continue
                    portfolio_value += float(
                        d.get('equity', 0)
                        or (float(d.get('quantity', 0) or 0) * float(d.get('price', 0) or 0))
                    )
            portfolio_value += cash

        crypto_positions = r.crypto.get_crypto_positions()
        if crypto_positions:
            for pos in crypto_positions:
                if not isinstance(pos, dict):
                    continue
                qty = float(pos.get('quantity', 0) or 0)
                if qty > 0:
                    cur = pos.get('currency')
                    if isinstance(cur, dict):
                        symbol = cur.get('code') or cur.get('id') or ""
                    else:
                        symbol = str(cur or "")
                    if not symbol:
                        continue
                    live_price = self.get_live_price(symbol)
                    portfolio_value += (qty * live_price)
        return portfolio_value, cash

    def get_current_holdings(self):
        assets = []
        if not self.is_connected: return assets
        try:
            holdings = r.build_holdings()
            if isinstance(holdings, dict):
                for ticker, data in holdings.items():
                    if not isinstance(data, dict):
                        continue
                    qty = float(data.get('quantity', 0) or 0)
                    if qty > 0:
                        cost = float(data.get('average_buy_price', 0) or 0)
                        assets.append({
                            'ticker': str(ticker).replace("-USD", "").upper(),
                            'shares': qty,
                            'cost': cost,
                            'type': 'Ready (Stock)',
                        })
        except Exception: pass

        try:
            crypto_positions = r.crypto.get_crypto_positions()
            if crypto_positions:
                for pos in crypto_positions:
                    if not isinstance(pos, dict):
                        continue
                    try:
                        qty = float(
                            pos.get("quantity")
                            or pos.get("quantity_available")
                            or 0
                        )
                    except (TypeError, ValueError):
                        qty = 0.0
                    if qty > 0:
                        cur = pos.get('currency')
                        if isinstance(cur, dict):
                            symbol = cur.get('code') or cur.get('id') or ""
                        else:
                            symbol = str(cur or "")
                        if not symbol:
                            continue
                        mark = _rh_pos_mark_price(pos)
                        if mark <= 0:
                            try:
                                q = r.crypto.get_crypto_quote(symbol)
                                if isinstance(q, dict):
                                    mp = q.get("mark_price")
                                    if mp is not None and float(mp) > 0:
                                        mark = float(mp)
                            except Exception:
                                pass

                        avg_cost = _rh_crypto_avg_cost(pos, qty, mark_price=mark)
                        
                        # --- 1.30.1 SEED FIX (ROBINHOOD) ---
                        if avg_cost <= 0.0:
                            seeded = _get_seeded_cost("ROBINHOOD", symbol)
                            if seeded > 0:
                                avg_cost = seeded
                                print(f"[Robinhood] Cost basis seeded for {str(symbol).replace('-USD', '').upper()}: ${avg_cost}")
                        # -----------------------------------

                        row = {
                            'ticker': str(symbol).replace("-USD", "").upper(),
                            'shares': qty,
                            'cost': avg_cost,
                            'type': 'Ready (Crypto)',
                        }
                        if mark > 0:
                            row['price'] = mark
                            row['mark'] = mark
                        assets.append(row)
        except Exception: pass
        return assets

    def get_live_price(self, ticker, allow_yahoo_fallback=True):
        clean = str(ticker).replace("-USD", "").upper()
        cryptos = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
        if clean in cryptos:
            try:
                q = r.crypto.get_crypto_quote(clean)
                if isinstance(q, dict):
                    mp = q.get('mark_price')
                    if mp is not None and float(mp) > 0:
                        return float(mp)
            except Exception: pass
            if not allow_yahoo_fallback:
                return 0.0
            try:
                df = yf.Ticker(f"{clean}-USD").history(period="1d")
                if not df.empty: return float(df['Close'].iloc[-1])
            except Exception: pass
            return 0.0
        try:
            p = r.stocks.get_latest_price(clean)
            if p and len(p) > 0 and p[0] is not None:
                price = float(p[0])
                if price > 0:
                    return price
        except Exception:
            pass
        return 0.0

    def _get_crypto_order_limits(self, ticker):
        """Return (qty_increment, min_order_qty) for Robinhood crypto."""
        if not hasattr(self, '_crypto_limits_cache'):
            self._crypto_limits_cache = {}
        if ticker in self._crypto_limits_cache:
            return self._crypto_limits_cache[ticker]
        inc = self._get_crypto_qty_increment(ticker)
        min_qty = inc
        try:
            raw_info = r.crypto.get_crypto_info(ticker)
            info = raw_info if isinstance(raw_info, dict) else {}
            for key in ('min_order_size', 'crypto_min_order_size', 'min_order_quantity', 'min_order_quantity_increment'):
                raw = info.get(key)
                if raw is None:
                    continue
                val = float(raw)
                if key.endswith('increment'):
                    if val > 0:
                        inc = val
                elif val > 0:
                    min_qty = max(min_qty, val)
            # Some RH pairs expose min only via increment that is already the practical floor
            if min_qty < inc:
                min_qty = inc
        except Exception:
            pass
        self._crypto_limits_cache[ticker] = (inc, min_qty)
        return inc, min_qty

    def position_is_dust(self, ticker, shares, price, asset_type=""):
        """True when RH cannot sell this size (crypto min qty or stock <$1 fractional)."""
        try:
            shares = float(shares)
            price = float(price)
        except Exception:
            return True, "invalid size/price"
        if shares <= 0 or price <= 0:
            return True, "invalid size/price"

        is_crypto = "crypto" in str(asset_type).lower() or str(ticker).upper() in {
            "BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"
        }
        notional = shares * price
        if is_crypto:
            inc, min_qty = self._get_crypto_order_limits(ticker)
            d_inc = Decimal(str(inc))
            valid = (Decimal(str(shares)) / d_inc).quantize(Decimal("1"), rounding=ROUND_DOWN) * d_inc
            if float(valid) <= 0:
                return True, f"below RH qty increment ({inc})"
            if float(valid) + 1e-12 < float(min_qty):
                return True, f"below RH min qty ({min_qty} {ticker})"
            # RH also rejects tiny notionals on some pairs
            if notional < 1.0:
                return True, f"below ~$1 RH crypto floor (${notional:.4f})"
            return False, ""
        # Whole shares are always sell-eligible (penny/OTC bags can be <$1 notional).
        # Only block sub-$1 *fractional* equity (RH rejects those).
        if self._qty_is_whole_shares(shares):
            return False, ""
        if notional < 1.0:
            return True, f"stock fractional under $1 (${notional:.4f})"
        return False, ""

    def _rh_instrument_id_from_positions(self, ticker):
        """
        Resolve instrument UUID from open stock positions when symbol search fails
        (common for OTC / *Q delisted leftovers like GOEVQ).
        """
        clean = str(ticker).replace("-USD", "").upper().strip()
        if not clean:
            return None
        try:
            positions = r.get_open_stock_positions() or []
        except Exception:
            try:
                positions = r.account.get_open_stock_positions() or []
            except Exception:
                return None
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            try:
                qty = float(pos.get("quantity") or pos.get("shares") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            inst_url = pos.get("instrument") or pos.get("instrument_url") or ""
            if not inst_url:
                continue
            try:
                from robin_stocks.robinhood.helper import request_get
                inst = request_get(inst_url, "regular")
                if not isinstance(inst, dict):
                    continue
                sym = str(inst.get("symbol") or "").upper().strip()
                if sym == clean:
                    iid = inst.get("id")
                    if iid:
                        return str(iid)
            except Exception:
                continue
        return None

    def _rh_equity_sellable(self, ticker):
        """
        Return (ok, instrument_id_or_none, reason).
        ok=False when RH has no tradeable instrument for this symbol (OTC/delisted).
        """
        clean = str(ticker).replace("-USD", "").upper().strip()
        try:
            from robin_stocks.robinhood.stocks import id_for_stock
            iid = id_for_stock(clean)
            if iid:
                return True, str(iid), ""
        except Exception:
            pass
        iid = self._rh_instrument_id_from_positions(clean)
        if iid:
            return True, iid, ""
        hint = ""
        if clean.endswith("Q") and len(clean) >= 4:
            hint = " (OTC/delisted *Q — often sell-only in Robinhood app, not API)"
        return False, None, (
            f"RH has no tradeable instrument for {clean}{hint}. "
            "Try selling in the Robinhood app; API cannot place the order."
        )

    def _get_crypto_qty_increment(self, ticker):
        if ticker not in self._crypto_inc_cache:
            try:
                info = r.crypto.get_crypto_info(ticker)
                inc = info.get('min_order_quantity_increment')
                self._crypto_inc_cache[ticker] = float(inc) if inc else (1.0 if ticker in ["PEPE", "SHIB", "BONK"] else 0.000001)
            except Exception:
                self._crypto_inc_cache[ticker] = 1.0 if ticker in ["PEPE", "SHIB", "BONK"] else 0.000001
        return self._crypto_inc_cache[ticker]

    def _rh_place_fractional_order(self, symbol, quantity, side, use_ext_hours=False, time_in_force="gfd"):
        """
        Place a fractional equity order without robin_stocks' extended-hours bug.

        robin_stocks.orders.order() does int(quantity) whenever market_hours is
        'extended_hours' / 'all_day_hours', which turns e.g. 0.12 META into 0 and
        RH rejects with: quantity: Ensure this value is greater than 0.
        """
        from uuid import uuid4
        from datetime import datetime
        from robin_stocks.robinhood.helper import round_price, request_post
        from robin_stocks.robinhood.urls import orders_url
        from robin_stocks.robinhood.account import load_account_profile
        from robin_stocks.robinhood.stocks import get_instruments_by_symbols, get_latest_price

        symbol = str(symbol).upper().strip()
        side = str(side).lower().strip()
        qty = float(
            Decimal(str(quantity)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        )
        if qty <= 0:
            return {"detail": "quantity rounded to 0", "quantity": ["Ensure this value is greater than 0."]}

        use_ext = bool(use_ext_hours)
        price_type = "ask_price" if side == "buy" else "bid_price"
        price = round_price(next(iter(get_latest_price(symbol, price_type, use_ext)), 0.00))
        ask = round_price(next(iter(get_latest_price(symbol, "ask_price", use_ext)), 0.00))
        bid = round_price(next(iter(get_latest_price(symbol, "bid_price", use_ext)), 0.00))
        instruments = get_instruments_by_symbols(symbol, info="url") or []
        if not instruments:
            return {"detail": f"No instrument URL for {symbol}"}

        market_hours = "extended_hours" if use_ext else "regular_hours"
        payload = {
            "account": load_account_profile(info="url"),
            "instrument": instruments[0],
            "symbol": symbol,
            "price": price,
            "ask_price": ask,
            "bid_ask_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            "bid_price": bid,
            "quantity": qty,  # keep fractional — do NOT int()
            "ref_id": str(uuid4()),
            "type": "limit" if use_ext else "market",
            "time_in_force": time_in_force,
            "trigger": "immediate",
            "side": side,
            "market_hours": market_hours,
            "extended_hours": use_ext,
            "order_form_version": 4,
        }
        if not use_ext and side == "sell" and payload["type"] == "market":
            payload.pop("price", None)
        elif not use_ext and side == "buy":
            # Match robin_stocks regular-hours fractional buy behavior
            payload["preset_percent_limit"] = "0.05"
            payload["type"] = "limit"

        return request_post(orders_url(), payload, jsonify_data=True)

    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
                        market_hours="regular_hours", allow_fractional=True):
        is_crypto = "crypto" in asset_type.lower() or ticker.upper() in {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
        if is_crypto:
            inc, min_qty = self._get_crypto_order_limits(ticker)
            d_inc = Decimal(str(inc))
            decimals = abs(d_inc.as_tuple().exponent)
            d_price = Decimal(str(price)) if price and price > 0 else Decimal("0")
            d_trade = Decimal(str(trade_dollars))
            d_qty = (d_trade / d_price) if d_price > 0 else Decimal("0")
            valid_qty_dec = (d_qty / d_inc).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_inc
            if valid_qty_dec <= 0:
                return f"Skipped: Cannot afford 1 increment", 0.0, None
            if float(valid_qty_dec) + 1e-12 < float(min_qty):
                return f"Skipped: Below RH min order ({min_qty} {ticker})", 0.0, None
            # RH often 422s sub-$5 crypto notionals (esp. BTC). Qty ROUND_DOWN from an
            # exact $5 size can land a hair under the floor while display still shows $5.00.
            min_notional = max(
                Decimal("5.00"),
                (Decimal(str(min_qty)) * d_price) if d_price > 0 else Decimal("5.00"),
            )
            actual_spent_dec = valid_qty_dec * d_price
            if actual_spent_dec < min_notional:
                # Only bump when the *intended* size was already at/above the floor —
                # ROUND_DOWN to the qty increment is what dipped us under (shows as $5.00 < $5.00).
                if d_trade + Decimal("0.01") < min_notional:
                    return (
                        f"Skipped: Below RH crypto floor "
                        f"(${float(actual_spent_dec):.2f} < ${float(min_notional):.2f})",
                        0.0,
                        None,
                    )
                need_qty = (min_notional / d_price) if d_price > 0 else Decimal("0")
                bumped = (need_qty / d_inc).to_integral_value(rounding=ROUND_UP) * d_inc
                if bumped < Decimal(str(min_qty)):
                    bumped = (
                        (Decimal(str(min_qty)) / d_inc).to_integral_value(rounding=ROUND_UP) * d_inc
                    )
                bumped_spent = bumped * d_price
                # If still short (price tick / float edge), add one more increment.
                if bumped_spent < min_notional:
                    bumped = bumped + d_inc
                    bumped_spent = bumped * d_price
                max_bump = max(d_trade, min_notional) + (d_inc * d_price) + Decimal("0.02")
                if bumped_spent <= max_bump and bumped_spent >= min_notional:
                    valid_qty_dec = bumped
                    actual_spent_dec = bumped_spent
                else:
                    return (
                        f"Skipped: Below RH crypto floor "
                        f"(${float(actual_spent_dec):.2f} < ${float(min_notional):.2f})",
                        0.0,
                        None,
                    )
            safe_qty_str = format(float(valid_qty_dec), f".{decimals}f")
            actual_spent = float(actual_spent_dec)
            try:
                res = r.order_buy_crypto_by_quantity(ticker, safe_qty_str)
                if isinstance(res, dict) and 'id' in res:
                    oid = res['id']
                    conf, state = self.confirm_order(oid, is_crypto=True)
                    if not conf and state and any(
                        x in str(state).lower() for x in ("reject", "fail", "cancel", "unconfirm")
                    ):
                        return f"Skipped: RH rejected ({state})", 0.0, None
                    tag = "Filled" if conf else f"Pending/{state}"
                    return f"Crypto Buy {tag} ({actual_spent:.2f})", actual_spent, oid
                err = self._format_rh_order_error(res, what=f"crypto buy {ticker}")
                if "422" in err or res is None:
                    return f"Skipped: RH rejected small/invalid crypto size ({err})", 0.0, None
                return f"Fail: {err}", 0.0, None
            except Exception as e:
                err = str(e) if str(e).strip() and str(e).strip().lower() != "none" else repr(e)
                if "422" in err:
                    return f"Skipped: RH 422 (size/limits) for {ticker}", 0.0, None
                return f"Fail: {err}", 0.0, None

        # Prefer dollar fractional when RH allows it for this session.
        # Overnight / late extended: fractionals OFF — whole-share limit only.
        if allow_fractional:
            try:
                want_ext = bool(use_ext_hours or market_hours == "extended_hours")
                if want_ext:
                    # Convert $ → shares ourselves; bypass robin_stocks int(qty) bug
                    ask = float(price) if price and price > 0 else 0.0
                    if ask <= 0:
                        try:
                            from robin_stocks.robinhood.stocks import get_latest_price
                            from robin_stocks.robinhood.helper import round_price
                            ask = float(round_price(next(iter(get_latest_price(ticker, "ask_price", True)), 0.0)))
                        except Exception:
                            ask = 0.0
                    frac_shares = (float(trade_dollars) / ask) if ask > 0 else 0.0
                    res = self._rh_place_fractional_order(
                        ticker, frac_shares, "buy", use_ext_hours=True, time_in_force="gfd"
                    )
                else:
                    res = r.order_buy_fractional_by_price(
                        ticker, trade_dollars, timeInForce="gfd",
                        extendedHours=False, market_hours="regular_hours",
                    )
                if isinstance(res, dict) and ("id" in res or "state" in res):
                    oid = res.get("id")
                    conf, state = self.confirm_order(oid, is_crypto=False, timeout_sec=45) if oid else (False, "unknown")
                    if oid and not conf:
                        self.cancel_order(oid, is_crypto=False)
                        return f"Skipped: Limit unfilled ({state}) — cancelled", 0.0, None
                    tag = "Filled" if conf else f"Pending/{state}"
                    suffix = " Ext" if want_ext else ""
                    return f"Buy Fractional{suffix} {tag} ({trade_dollars:.2f})", trade_dollars, oid
                frac_err = str(res)
            except Exception as e:
                frac_err = str(e)

            if not use_ext_hours and market_hours == "regular_hours":
                return f"Fail: {frac_err}", 0.0, None

            # Extended/overnight fallback: whole-share limit if we can afford 1 share
            qty_to_buy = int(trade_dollars / price) if price > 0 else 0
            if qty_to_buy < 1:
                return (
                    f"Skipped: Fractional unavailable ({frac_err[:80]}); "
                    f"cannot afford 1 whole share.",
                    0.0,
                    None,
                )
            limit_price = round(price * (1.0 + offset_pct), 4 if price < 1.0 else 2)
            res = r.order_buy_limit(
                symbol=ticker,
                quantity=qty_to_buy,
                limitPrice=limit_price,
                timeInForce="gfd",
                extendedHours=True,
            )
            if isinstance(res, dict) and ("id" in res or "state" in res):
                oid = res.get("id")
                conf, state = self.confirm_order(oid, is_crypto=False, timeout_sec=45) if oid else (False, "unknown")
                if oid and not conf:
                    self.cancel_order(oid, is_crypto=False)
                    return f"Skipped: Limit unfilled ({state}) — cancelled", 0.0, None
                tag = "Filled" if conf else f"Pending/{state}"
                return f"Buy Limit Ext {tag} ({qty_to_buy})", (qty_to_buy * limit_price), oid
            return f"Fail: {res}", 0.0, None

        # Session does not allow fractionals (overnight or late after-hours)
        qty_to_buy = int(trade_dollars / price) if price > 0 else 0
        if qty_to_buy < 1:
            return (
                "Skipped: Overnight/late session — RH blocks fractionals; "
                "need ≥1 whole share (fractionals resume ~7am ET / regular open).",
                0.0,
                None,
            )
        limit_price = round(price * (1.0 + offset_pct), 4 if price < 1.0 else 2)
        res = r.order_buy_limit(
            symbol=ticker,
            quantity=qty_to_buy,
            limitPrice=limit_price,
            timeInForce="gfd",
            extendedHours=True,
        )
        if isinstance(res, dict) and ("id" in res or "state" in res):
            oid = res.get("id")
            conf, state = self.confirm_order(oid, is_crypto=False, timeout_sec=45) if oid else (False, "unknown")
            if oid and not conf:
                self.cancel_order(oid, is_crypto=False)
                return f"Skipped: Limit unfilled ({state}) — cancelled", 0.0, None
            tag = "Filled" if conf else f"Pending/{state}"
            return f"Buy Limit Ext {tag} ({qty_to_buy})", (qty_to_buy * limit_price), oid
        return f"Fail: {res}", 0.0, None


    @staticmethod
    def _format_rh_order_error(res, what="order"):
        """Turn None/opaque RH responses into a journal-usable error string."""
        if res is None:
            return (
                f"RH {what} returned empty response (None) — "
                "auth expired, rate-limit, or API unavailable"
            )
        if isinstance(res, dict):
            for key in ("detail", "error", "message", "msg", "non_field_errors", "reason"):
                val = res.get(key)
                if val:
                    return f"{what}: {val}"
            for key in ("errors", "error_response"):
                val = res.get(key)
                if val:
                    return f"{what}: {val}"
            if not res:
                return f"RH {what} returned empty dict"
            return f"{what}: {res}"
        s = str(res).strip()
        if not s or s.lower() == "none":
            return f"RH {what} returned no detail"
        return s

    def _live_sellable_qty(self, ticker, is_crypto):
        """Fresh broker position qty for full exits (avoids stale UI / truncated sells)."""
        clean = str(ticker).replace("-USD", "").upper()
        try:
            if is_crypto:
                positions = r.crypto.get_crypto_positions() or []
                for pos in positions:
                    if not isinstance(pos, dict):
                        continue
                    cur = pos.get("currency")
                    if isinstance(cur, dict):
                        symbol = (cur.get("code") or cur.get("id") or "").upper()
                    else:
                        symbol = str(cur or "").upper()
                    if symbol != clean:
                        continue
                    qty = float(pos.get("quantity", 0) or 0)
                    if qty > 0:
                        return qty
                return None
            holdings = r.build_holdings()
            if isinstance(holdings, dict):
                data = holdings.get(clean) or holdings.get(ticker)
                if isinstance(data, dict):
                    qty = float(data.get("quantity", 0) or 0)
                    if qty > 0:
                        return qty
        except Exception:
            pass
        return None

    @staticmethod
    def _qty_is_whole_shares(shares_val):
        try:
            d = Decimal(str(shares_val))
            return d >= 1 and d == d.to_integral_value()
        except Exception:
            return False

    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                         market_hours="regular_hours", allow_fractional=True, sell_all=False):
        is_crypto = "crypto" in asset_type.lower() or ticker.upper() in {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
        # RH has no native sell-all endpoint — refresh live qty and never int-truncate fractionals.
        if sell_all:
            live_qty = self._live_sellable_qty(ticker, is_crypto)
            if live_qty is not None and live_qty > 0:
                shares_val = live_qty

        all_tag = "Sell-All" if sell_all else "Sell"

        if is_crypto:
            inc, min_qty = self._get_crypto_order_limits(ticker)
            d_inc = Decimal(str(inc))
            decimals = abs(d_inc.as_tuple().exponent)
            d_qty = Decimal(str(shares_val))
            valid_qty_dec = (d_qty / d_inc).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_inc
            if valid_qty_dec <= 0:
                return "Skipped: Quantity too small", None
            if float(valid_qty_dec) + 1e-12 < float(min_qty):
                return f"Skipped: Dust below RH min ({min_qty} {ticker})", None
            safe_qty_str = format(float(valid_qty_dec), f".{decimals}f")
            try:
                res = r.order_sell_crypto_by_quantity(ticker, safe_qty_str)
                if isinstance(res, dict) and 'id' in res:
                    oid = res['id']
                    conf, state = self.confirm_order(oid, is_crypto=True)
                    tag = "Filled" if conf else f"Pending/{state}"
                    return f"Crypto {all_tag} {tag} ({safe_qty_str})", oid
                # RH sometimes returns validation errors as dict without id (or None)
                err = self._format_rh_order_error(res, what=f"crypto sell {ticker}")
                if "too small" in err.lower() or "at least" in err.lower():
                    return f"Skipped: {err}", None
                return f"Fail: {err}", None
            except Exception as e:
                err = str(e) if str(e).strip() and str(e).strip().lower() != "none" else repr(e)
                if "too small" in err.lower() or "at least" in err.lower():
                    return f"Skipped: {err}", None
                return f"Fail: {err}", None

        if price <= 0:
            return "Skipped: No valid market price (delisted/untradeable)", None

        sellable, _iid, why_blocked = self._rh_equity_sellable(ticker)
        if not sellable:
            return f"Skipped: {why_blocked}", None

        def _is_instrument_miss(err):
            e = str(err or "").lower()
            return (
                "list index" in e
                or "not a valid" in e
                or "no instrument" in e
                or "instrument" in e and "none" in e
            )

        # Whole-share-only path (limit → market fallback). Never int() a fractional full exit.
        if self._qty_is_whole_shares(shares_val):
            qty_to_sell = int(Decimal(str(shares_val)).to_integral_value())
            limit_price = round(price * (1.0 - offset_pct), 4 if price < 1.0 else 2)
            try:
                res = r.order_sell_limit(
                    symbol=ticker, quantity=qty_to_sell, limitPrice=limit_price,
                    timeInForce="gfd", extendedHours=use_ext_hours,
                )
                if isinstance(res, dict) and ("id" in res or "state" in res):
                    oid = res.get("id")
                    conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
                    tag = "Filled" if conf else f"Pending/{state}"
                    return f"{all_tag} {tag} ({qty_to_sell})", oid
                # Limit rejected / empty — try market once (OTC leftovers sometimes need it)
                try:
                    res_m = r.order_sell_market(
                        symbol=ticker, quantity=qty_to_sell,
                        timeInForce="gfd", extendedHours=use_ext_hours,
                    )
                    if isinstance(res_m, dict) and ("id" in res_m or "state" in res_m):
                        oid = res_m.get("id")
                        conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
                        tag = "Filled" if conf else f"Pending/{state}"
                        return f"{all_tag} market {tag} ({qty_to_sell})", oid
                    return f"Fail: {res_m or res}", None
                except Exception as e2:
                    if _is_instrument_miss(e2):
                        return (
                            f"Skipped: RH cannot trade {str(ticker).upper()} via API "
                            "(OTC/delisted). Sell in the Robinhood app if the position still shows.",
                            None,
                        )
                    return f"Fail: {e2}", None
            except Exception as e:
                err = str(e)
                if _is_instrument_miss(err):
                    # Market fallback before giving up
                    try:
                        res_m = r.order_sell_market(
                            symbol=ticker, quantity=qty_to_sell,
                            timeInForce="gfd", extendedHours=False,
                        )
                        if isinstance(res_m, dict) and ("id" in res_m or "state" in res_m):
                            oid = res_m.get("id")
                            conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
                            tag = "Filled" if conf else f"Pending/{state}"
                            return f"{all_tag} market {tag} ({qty_to_sell})", oid
                    except Exception as e2:
                        if _is_instrument_miss(e2) or _is_instrument_miss(err):
                            return (
                                f"Skipped: RH cannot trade {str(ticker).upper()} via API "
                                "(OTC/delisted). Sell in the Robinhood app if the position still shows.",
                                None,
                            )
                        return f"Fail: {e2}", None
                    return (
                        f"Skipped: RH cannot trade {str(ticker).upper()} via API "
                        "(OTC/delisted). Sell in the Robinhood app if the position still shows.",
                        None,
                    )
                return f"Fail: {e}", None

        # Fractional remainder / mixed integer+fractional / sub-1 share — sell exact qty
        if (shares_val * price) < 1.00:
            return f"Skipped: Fractional value under $1.00", None

        if not allow_fractional:
            return (
                "Skipped: Overnight/late session — RH blocks fractional equity sells "
                "(OK again in extended ~7am ET or regular hours; after-hours fractionals end ~7:30pm ET).",
                None,
            )

        try:
            want_ext = bool(use_ext_hours or market_hours == "extended_hours")
            if want_ext:
                # Bypass robin_stocks int(qty) bug on extended_hours fractionals
                res = self._rh_place_fractional_order(
                    ticker, shares_val, "sell", use_ext_hours=True, time_in_force="gfd"
                )
            else:
                res = r.order_sell_fractional_by_quantity(
                    ticker, shares_val, timeInForce="gfd",
                    extendedHours=False, market_hours="regular_hours",
                )
            if isinstance(res, dict) and ("id" in res or "state" in res):
                oid = res.get("id")
                conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
                tag = "Filled" if conf else f"Pending/{state}"
                suffix = " Ext" if want_ext else ""
                qty_lbl = format(float(shares_val), ".6f").rstrip("0").rstrip(".")
                return f"{all_tag}{suffix} {tag} ({qty_lbl})", oid
            err = str(res)
            if want_ext:
                return f"Skipped: Ext. Hours fractional not eligible ({err[:100]})", None
            return f"Fail: {res}", None
        except Exception as e:
            err = str(e)
            if _is_instrument_miss(err):
                return (
                    f"Skipped: RH cannot trade {str(ticker).upper()} via API "
                    "(OTC/delisted). Sell in the Robinhood app if the position still shows.",
                    None,
                )
            if use_ext_hours or market_hours == "extended_hours":
                return f"Skipped: Ext. Hours fractional rejected ({err[:100]})", None
            return f"Fail: {e}", None

    def confirm_order(self, order_id, is_crypto=False, timeout_sec=10):
        """Poll Robinhood until filled/cancelled/rejected or timeout."""
        if not order_id:
            return False, "no_id"
        self._last_fill_fee = None
        deadline = time.time() + timeout_sec
        last_state = "unknown"
        while time.time() < deadline:
            try:
                if is_crypto:
                    info = r.orders.get_crypto_order_info(order_id)
                else:
                    info = r.orders.get_stock_order_info(order_id)
                if isinstance(info, dict):
                    last_state = str(info.get('state') or info.get('status') or "unknown").lower()
                    if last_state in ("filled", "completed"):
                        try:
                            from analytics import extract_fee_dollars_from_order
                            self._last_fill_fee = extract_fee_dollars_from_order(info)
                        except Exception:
                            # RH fees field is common on stock fills
                            try:
                                if info.get("fees") is not None:
                                    self._last_fill_fee = float(info.get("fees"))
                            except (TypeError, ValueError):
                                pass
                        return True, last_state
                    if last_state in ("cancelled", "canceled", "rejected", "failed"):
                        return False, last_state
            except Exception:
                pass
            time.sleep(1.2)
        return False, last_state

    def cancel_order(self, order_id, is_crypto=False):
        if not order_id:
            return False, "no_id"
        try:
            if is_crypto:
                cancel_fn = getattr(r, "cancel_crypto_order", None) or getattr(r.orders, "cancel_crypto_order", None)
                cancel_fn(order_id)
            else:
                cancel_fn = getattr(r, "cancel_stock_order", None) or getattr(r.orders, "cancel_stock_order", None)
                cancel_fn(order_id)
            return True, "cancelled"
        except Exception as e:
            return False, str(e)

    def place_protective_stop(self, ticker, asset_type, quantity, entry_price, stop_pct,
                              trail_pct=None):
        """
        RH equities: prefer trailing stop at hard_stop % (disaster trail), else fixed stop-loss.
        RH crypto: no stop API — caller keeps software TTP.
        """
        is_crypto = "crypto" in str(asset_type).lower() or str(ticker).upper() in {
            "BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"
        }
        if is_crypto:
            return False, None, "RH crypto has no stop/trailing API — software TTP only"
        try:
            qty = float(quantity or 0)
            entry = float(entry_price or 0)
            stop_d = abs(float(stop_pct or 0))
            if qty <= 0 or entry <= 0 or stop_d <= 0:
                return False, None, "invalid qty/entry/stop"
            # RH rejects stops on fractional qty — TTP only (do not retry as "missing")
            if not self._qty_is_whole_shares(qty):
                return False, None, "fractional — broker stop N/A, TTP only"
            # RH rejects >8 decimal qty and non-integer trailing_peg percentages.
            qty_dec = Decimal(str(qty)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if qty_dec <= 0:
                return False, None, "qty rounded to 0"
            qty_arg = int(qty_dec)  # whole shares only after gate above
            # Integer percent points (ceil so trail is at least as wide as configured)
            trail_pct_points = max(1, int(math.ceil(stop_d * 100.0 - 1e-12)))
            # 1) Trailing stop — rises with price; software TTP still handles tighter trails
            try:
                place_trail = getattr(r, "order_sell_trailing_stop", None) or getattr(r.orders, "order_sell_trailing_stop", None)
                res = place_trail(
                    str(ticker).upper(), qty_arg, trail_pct_points, "percentage",
                    timeInForce="gtc", extendedHours=False,
                )
                if isinstance(res, dict) and res.get("id"):
                    return True, res["id"], f"RH trailing stop {trail_pct_points}% (id={res['id'][:8]}…)"
                trail_err = str(res)
            except Exception as e:
                trail_err = str(e)
            # 2) Fixed stop-loss at hard_stop distance
            stop_price = round(entry * (1.0 - stop_d), 4 if entry < 1.0 else 2)
            try:
                place_stop = getattr(r, "order_sell_stop_loss", None) or getattr(r.orders, "order_sell_stop_loss", None)
                res = place_stop(
                    str(ticker).upper(), qty_arg, stop_price,
                    timeInForce="gtc", extendedHours=False,
                )
                if isinstance(res, dict) and res.get("id"):
                    return True, res["id"], f"RH stop-loss @ {stop_price} (id={res['id'][:8]}…)"
                # 3) Stop-limit fallback (limit a hair below stop)
                limit_price = round(stop_price * 0.995, 4 if entry < 1.0 else 2)
                place_sl = getattr(r, "order_sell_stop_limit", None) or getattr(r.orders, "order_sell_stop_limit", None)
                res2 = place_sl(
                    str(ticker).upper(), qty_arg, limit_price, stop_price,
                    timeInForce="gtc", extendedHours=False,
                )
                if isinstance(res2, dict) and res2.get("id"):
                    return True, res2["id"], f"RH stop-limit @ {stop_price}/{limit_price}"
                return False, None, f"RH stop failed (trail: {trail_err}; stop: {res})"
            except Exception as e:
                return False, None, f"RH stop failed (trail: {trail_err}; stop: {e})"
        except Exception as e:
            return False, None, f"RH protective stop error: {e}"


class CoinbaseAdapter(BaseBroker):
    """The Coinbase Advanced Trade translation layer."""
    def __init__(self):
        super().__init__()
        self.broker_id = "COINBASE"
        self.supports_equities = False
        self.supports_crypto = True
        self.supports_fractional_equities = False
        self.supports_extended_hours = False
        self.supports_protective_stops = False
        self.min_equity_notional = 1.0
        self.client = None
        # Kill switch (default True — CB has no sandbox; uncheck to block live orders).
        self.live_trading_enabled = True
        self._last_request_ts = 0.0

    def login(self, credentials):
        RESTClient = _get_rest_client_class()
        if not RESTClient or not COINBASE_AVAILABLE:
            return False, "coinbase-advanced-py not installed"

        api_key = credentials.get('api_key')
        api_secret = credentials.get('api_secret')

        if not api_key or not api_secret:
            return False, "Missing CDP API Key or Secret"

        try:
            self.live_trading_enabled = bool(credentials.get("live_trading_enabled", True))
            self.client = RESTClient(api_key=api_key, api_secret=api_secret)
            data = self._cb_payload(self._cb_call(self.client.get_accounts, limit=1))

            if data and ("accounts" in data or _as_list(data.get("accounts"))):
                self.is_connected = True
                return True, "Success"
            return False, "Authentication Failed"
        except Exception as e:
            return False, str(e)

    def logout(self):
        self.client = None
        self.is_connected = False

    def _orders_allowed(self):
        if self.live_trading_enabled:
            return True, ""
        return False, (
            "Coinbase live trading is disabled. "
            "Enable coinbase_live_trading in Settings / Coinbase login to place orders."
        )

    def _cb_throttle(self):
        gap = _CB_MIN_REQUEST_GAP_SEC - (time.time() - self._last_request_ts)
        if gap > 0:
            time.sleep(gap)

    def _cb_retryable(self, exc):
        msg = str(exc or "").lower()
        status = getattr(exc, "status_code", None)
        if status in (429, 500, 502, 503, 504):
            return True
        return any(
            tok in msg
            for tok in (
                "429", "500", "502", "503", "504",
                "rate limit", "too many", "timeout", "temporar", "unavailable",
            )
        )

    def _cb_call(self, fn, *args, **kwargs):
        """Gap + exponential backoff on 429/5xx / transient SDK errors (ET-style)."""
        last_err = None
        for attempt in range(_CB_MAX_RETRIES):
            self._cb_throttle()
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_err = e
                if attempt >= _CB_MAX_RETRIES - 1 or not self._cb_retryable(e):
                    raise
                time.sleep(min(8.0, (0.6 * (2 ** attempt)) + random.random() * 0.3))
            finally:
                self._last_request_ts = time.time()
        if last_err:
            raise last_err
        raise RuntimeError("Coinbase request failed after retries")

    def _cb_payload(self, res):
        return _cb_response_dict(res)

    def _fetch_all_accounts(self):
        """Helper to bypass Coinbase's 49-item default pagination limit."""
        all_accounts = []
        cursor = ""
        try:
            while True:
                if cursor:
                    res = self._cb_call(self.client.get_accounts, limit=250, cursor=cursor)
                else:
                    res = self._cb_call(self.client.get_accounts, limit=250)
                data = self._cb_payload(res)
                for acc in _as_list(data.get("accounts")):
                    if isinstance(acc, dict):
                        all_accounts.append(acc)

                cursor = data.get("cursor", "") or ""
                has_next = bool(data.get("has_next", False))
                if not has_next or not cursor:
                    break
        except Exception as e:
            print(f"Coinbase pagination error: {e}")
        return all_accounts

    def get_account_balances(self):
        if not self.is_connected:
            raise RuntimeError("Coinbase not connected")
        try:
            total_value = 0.0
            buying_power = 0.0
            accounts = self._fetch_all_accounts()

            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                # Nested {value,currency} can be string/None — never .get-chain on non-dicts
                avail = _money_value(acc.get("available_balance"))
                hold = _money_value(acc.get("hold"))
                total_qty = avail + hold
                currency = acc.get("currency")

                if total_qty > 0:
                    if currency == "USD" or currency == "USDC":
                        buying_power += avail
                        total_value += total_qty
                    else:
                        price = self.get_live_price(currency)
                        total_value += (total_qty * price)

            return total_value, buying_power
        except Exception as e:
            print(f"Coinbase get_account_balances error: {e}")
            raise

    def _cb_spot_avg_costs(self):
        """
        Map asset symbol → {"cost": per-unit avg, "mark": optional mark}.
        Prefers average_entry_price; else cost_basis total / crypto qty.
        Returns {} when portfolios API unavailable / unauthorized.
        """
        out = {}
        if not self.is_connected or not self.client:
            return out
        try:
            plist = self._cb_payload(self._cb_call(self.client.get_portfolios))
        except Exception:
            return out
        portfolios = _as_list(plist.get("portfolios"))
        if not portfolios:
            return out

        def _ptype(p):
            return str((p or {}).get("type") or "").upper()

        ordered = sorted(
            [p for p in portfolios if isinstance(p, dict)],
            key=lambda p: (0 if _ptype(p) == "DEFAULT" else 1, str(p.get("name") or "")),
        )
        for port in ordered:
            uuid = str(port.get("uuid") or "").strip()
            if not uuid:
                continue
            try:
                br = self._cb_payload(
                    self._cb_call(
                        self.client.get_portfolio_breakdown,
                        portfolio_uuid=uuid,
                        currency="USD",
                    )
                )
            except Exception:
                try:
                    br = self._cb_payload(
                        self._cb_call(self.client.get_portfolio_breakdown, portfolio_uuid=uuid)
                    )
                except Exception:
                    continue
            breakdown = br.get("breakdown") if isinstance(br.get("breakdown"), dict) else br
            spots = _as_list(
                (breakdown or {}).get("spot_positions")
                if isinstance(breakdown, dict)
                else None
            )
            for pos in spots:
                entry, mark = _cb_position_avg_entry(pos)
                if entry <= 0:
                    continue
                asset = str(pos.get("asset") or "").replace("-USD", "").upper().strip()
                if not asset:
                    continue
                prev = out.get(asset)
                if not prev or float(prev.get("cost") or 0) <= 0:
                    out[asset] = {"cost": entry, "mark": mark}
        return out

    def get_current_holdings(self):
        """
        Spot crypto with sellable size. Tiny leftovers Coinbase's app hides
        (sub-$1 / below product mins) are filtered so they don't show as phantoms.
        """
        assets = []
        if not self.is_connected:
            return assets
        # Match CB app hide-dust behavior (~$1) and our sell dust gate
        min_display_notional = 1.0
        try:
            accounts = self._fetch_all_accounts()
            avg_by_asset = {}
            try:
                avg_by_asset = self._cb_spot_avg_costs() or {}
            except Exception:
                avg_by_asset = {}

            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                avail = _money_value(acc.get("available_balance"))
                hold = _money_value(acc.get("hold"))
                total_qty = avail + hold
                currency = str(acc.get("currency") or "").replace("-USD", "").upper().strip()

                if total_qty > 0 and currency not in ["USD", "USDC", "USDT", "DAI"]:
                    meta = avg_by_asset.get(currency) or {}
                    try:
                        broker_cost = float(meta.get("cost") or 0.0)
                    except (TypeError, ValueError):
                        broker_cost = 0.0
                    try:
                        mark = float(meta.get("mark") or 0.0)
                    except (TypeError, ValueError):
                        mark = 0.0
                    if mark <= 0:
                        try:
                            mark = float(
                                self.get_live_price(currency, allow_yahoo_fallback=True) or 0.0
                            )
                        except Exception:
                            mark = 0.0
                    if mark > 0:
                        notional = float(total_qty) * mark
                        if notional + 1e-9 < min_display_notional:
                            continue
                        dust, _why = self.position_is_dust(
                            currency, total_qty, mark, "crypto"
                        )
                        if dust:
                            continue
                    row = {
                        "ticker": currency,
                        "shares": total_qty,
                        "cost": float(broker_cost or 0.0),
                        "type": "Ready (Crypto)",
                    }
                    if mark > 0:
                        row["price"] = mark
                        row["mark"] = mark
                    assets.append(row)
        except Exception as e:
            print(f"Coinbase get_current_holdings error: {e}")
            pass
        return assets

    def get_live_price(self, ticker, allow_yahoo_fallback=True):
        clean = str(ticker).replace("-USD", "").upper()
        # Hard block equity/ETF symbols — Coinbase has no SPY-USD etc (stops 404 spam)
        equity_block = {
            "SPY", "QQQ", "TQQQ", "SOXL", "NVDA", "AAPL", "TSLA", "AMD", "MSFT", "META",
            "AMZN", "VOO", "VTI", "IWM", "DIA", "PLTR", "SOUN", "SNDL", "PLUG", "GOEVQ",
            "SPCX", "NFLX", "GOOG", "GOOGL", "INTC", "BAC", "F", "GE", "DIS",
        }
        if clean in equity_block or clean.endswith("Q") and len(clean) >= 4:
            return 0.0

        if self.is_connected and self.client:
            try:
                product_id = f"{clean}-USD"
                data = self._cb_payload(self._cb_call(self.client.get_product, product_id=product_id))
                if data.get("price"):
                    price = float(data["price"])
                    if price > 0:
                        return price
            except Exception:
                pass
        if not allow_yahoo_fallback:
            return 0.0
        try:
            df = yf.Ticker(f"{clean}-USD").history(period="1d")
            if not df.empty:
                return float(df["Close"].iloc[-1])
        except Exception:
            pass
        return 0.0

    def _get_product_limits(self, ticker):
        """
        Return dict with base_increment, base_min_size, quote_min_size for a CB product.
        Cached per ticker.
        """
        clean = str(ticker).replace("-USD", "").upper()
        if not hasattr(self, "_product_limits_cache"):
            self._product_limits_cache = {}
        if clean in self._product_limits_cache:
            return self._product_limits_cache[clean]

        limits = {
            "base_increment": 0.00000001,
            "base_min_size": 0.0,
            "quote_min_size": 1.0,
            "quote_increment": 0.01,
        }
        if self.is_connected and self.client:
            try:
                data = self._cb_payload(
                    self._cb_call(self.client.get_product, product_id=f"{clean}-USD")
                )
                for key in (
                    "base_increment",
                    "base_min_size",
                    "quote_min_size",
                    "quote_increment",
                    "min_market_funds",
                ):
                    raw = data.get(key)
                    if raw is None:
                        continue
                    try:
                        val = float(raw)
                    except Exception:
                        continue
                    if key == "min_market_funds" and val > 0:
                        limits["quote_min_size"] = max(limits["quote_min_size"], val)
                    elif val > 0:
                        limits[key] = val
            except Exception:
                pass
        if limits["base_min_size"] <= 0:
            limits["base_min_size"] = limits["base_increment"]
        self._product_limits_cache[clean] = limits
        return limits

    def position_is_dust(self, ticker, shares, price, asset_type=""):
        """True when Coinbase cannot market-sell this size (base/quote mins)."""
        try:
            shares = float(shares)
            price = float(price)
        except Exception:
            return True, "invalid size/price"
        if shares <= 0 or price <= 0:
            return True, "invalid size/price"

        limits = self._get_product_limits(ticker)
        inc = limits["base_increment"]
        min_base = limits["base_min_size"]
        min_quote = limits["quote_min_size"]
        d_inc = Decimal(str(inc))
        valid = (Decimal(str(shares)) / d_inc).quantize(Decimal("1"), rounding=ROUND_DOWN) * d_inc
        if float(valid) <= 0:
            return True, f"below CB base increment ({inc})"
        if float(valid) + 1e-12 < float(min_base):
            return True, f"below CB min size ({min_base} {str(ticker).upper()})"
        notional = float(valid) * price
        if min_quote > 0 and notional + 1e-9 < float(min_quote):
            return True, f"below CB min notional (${min_quote:.2f}, have ${notional:.4f})"
        return False, ""

    def _extract_order_id(self, data):
        data = _as_dict(data)
        sr = _as_dict(data.get("success_response"))
        if sr.get("order_id"):
            return sr.get("order_id")
        return data.get("order_id") or data.get("id")

    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
                        market_hours="regular_hours", allow_fractional=True):
        if not self.is_connected:
            return "Fail: Not connected", 0.0, None
        ok, reason = self._orders_allowed()
        if not ok:
            return f"Fail: {reason}", 0.0, None
        clean = str(ticker).replace("-USD", "").upper()
        product_id = f"{clean}-USD"

        try:
            client_order_id = str(int(time.time() * 1000))
            off = float(offset_pct or 0)
            use_limit = off > 0 and float(price or 0) > 0
            if use_limit:
                limits = self._get_product_limits(clean)
                base_inc = float(limits.get("base_increment", 0.00000001) or 0.00000001)
                quote_inc = float(limits.get("quote_increment", 0.01) or 0.01)
                limit_px = float(price) * (1.0 + off)
                # Round limit to quote increment
                d_q = Decimal(str(quote_inc))
                limit_dec = (Decimal(str(limit_px)) / d_q).to_integral_value(
                    rounding=ROUND_UP
                ) * d_q
                d_inc = Decimal(str(base_inc))
                d_qty = Decimal(str(trade_dollars)) / limit_dec if limit_dec > 0 else Decimal("0")
                base_qty = (d_qty / d_inc).quantize(Decimal("1"), rounding=ROUND_DOWN) * d_inc
                if base_qty <= 0:
                    return "Fail: Quantity too small for limit buy", 0.0, None
                res = self._cb_call(
                    self.client.limit_order_gtc_buy,
                    client_order_id=client_order_id,
                    product_id=product_id,
                    base_size=format(base_qty, "f"),
                    limit_price=format(limit_dec, "f"),
                    post_only=False,
                )
            else:
                res = self._cb_call(
                    self.client.market_order_buy,
                    client_order_id=client_order_id,
                    product_id=product_id,
                    quote_size=str(round(trade_dollars, 2)),
                )
            data = self._cb_payload(res)

            if data.get("success"):
                oid = self._extract_order_id(data)
                conf, state = self.confirm_order(oid, is_crypto=True) if oid else (False, "no_id")
                tag = "Filled" if conf else f"Pending/{state}"
                kind = "limit" if use_limit else "market"
                return f"Coinbase Buy {tag} ({kind} {trade_dollars:.2f})", trade_dollars, oid
            return f"Fail: {data.get('error_response', 'Unknown Error')}", 0.0, None
        except Exception as e:
            return f"Fail: {e}", 0.0, None

    def _available_base_qty(self, ticker):
        """Sellable (available, not hold) base size for a currency."""
        clean = str(ticker).replace("-USD", "").upper()
        try:
            for acc in self._fetch_all_accounts():
                if not isinstance(acc, dict):
                    continue
                if str(acc.get("currency") or "").upper() != clean:
                    continue
                return _money_value(acc.get("available_balance"))
        except Exception:
            pass
        return 0.0

    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                         market_hours="regular_hours", allow_fractional=True, sell_all=False):
        if not self.is_connected:
            return "Fail: Not connected", None
        ok, reason = self._orders_allowed()
        if not ok:
            return f"Fail: {reason}", None
        clean = str(ticker).replace("-USD", "").upper()
        product_id = f"{clean}-USD"

        try:
            # Full exit: use available balance (not hold) and prefer native close_position.
            if sell_all:
                avail = self._available_base_qty(clean)
                if avail > 0:
                    shares_val = avail

            dust, reason = self.position_is_dust(clean, shares_val, price, asset_type)
            if dust:
                return f"Skipped: Dust ({reason})", None

            limits = self._get_product_limits(clean)
            base_increment = float(limits.get("base_increment", 0.00000001))

            d_inc = Decimal(str(base_increment))
            decimals = abs(d_inc.as_tuple().exponent)
            d_qty = Decimal(str(shares_val))
            valid_qty_dec = (d_qty / d_inc).quantize(Decimal("1"), rounding=ROUND_DOWN) * d_inc

            if valid_qty_dec <= 0:
                return "Skipped: Quantity too small", None
            safe_qty_str = format(float(valid_qty_dec), f".{decimals}f")

            client_order_id = str(int(time.time() * 1000))

            if sell_all and hasattr(self.client, "close_position"):
                try:
                    # Native close — size optional in API; pass available base to close the bag.
                    res = self._cb_call(
                        self.client.close_position,
                        client_order_id=client_order_id,
                        product_id=product_id,
                        size=safe_qty_str,
                    )
                    data = self._cb_payload(res)
                    if data.get("success"):
                        oid = self._extract_order_id(data)
                        conf, state = self.confirm_order(oid, is_crypto=True) if oid else (False, "no_id")
                        tag = "Filled" if conf else f"Pending/{state}"
                        return f"Coinbase Sell-All {tag} ({safe_qty_str})", oid
                    # Fall through to market sell if close_position rejects (e.g. spot-only quirks)
                except Exception:
                    pass
                client_order_id = str(int(time.time() * 1000))

            res = self._cb_call(
                self.client.market_order_sell,
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=safe_qty_str,
            )
            data = self._cb_payload(res)

            if data.get("success"):
                oid = self._extract_order_id(data)
                conf, state = self.confirm_order(oid, is_crypto=True) if oid else (False, "no_id")
                tag = "Filled" if conf else f"Pending/{state}"
                label = "Coinbase Sell-All" if sell_all else "Coinbase Sell"
                return f"{label} {tag} ({safe_qty_str})", oid
            return f"Fail: {data.get('error_response', 'Unknown Error')}", None
        except Exception as e:
            return f"Fail: {e}", None

    def confirm_order(self, order_id, is_crypto=False, timeout_sec=10):
        """Poll Coinbase until FILLED/CANCELLED/FAILED or timeout."""
        if not order_id or not self.client:
            return False, "no_id"
        self._last_fill_fee = None
        deadline = time.time() + timeout_sec
        last_state = "unknown"
        while time.time() < deadline:
            try:
                data = self._cb_payload(self._cb_call(self.client.get_order, order_id))
                order = _as_dict(data.get("order"))
                blob = order or data or {}
                if order:
                    last_state = str(order.get("status") or "unknown").upper()
                elif data:
                    last_state = str(data.get("status") or "unknown").upper()
                if last_state in ("FILLED", "COMPLETED"):
                    try:
                        from analytics import extract_fee_dollars_from_order
                        self._last_fill_fee = extract_fee_dollars_from_order(blob)
                    except Exception:
                        for k in ("total_fees", "total_fees_value", "commission"):
                            if blob.get(k) is None:
                                continue
                            try:
                                self._last_fill_fee = float(blob.get(k))
                                break
                            except (TypeError, ValueError):
                                pass
                    return True, last_state.lower()
                if last_state in ("CANCELLED", "CANCELED", "EXPIRED", "FAILED", "REJECTED"):
                    return False, last_state.lower()
            except Exception:
                pass
            time.sleep(1.2)
        return False, last_state.lower() if last_state else "unknown"

    def cancel_order(self, order_id, is_crypto=False):
        if not order_id or not self.client:
            return False, "no_id"
        try:
            data = self._cb_payload(self._cb_call(self.client.cancel_orders, [str(order_id)]))
            return True, f"cancelled ({data})"
        except Exception as e:
            return False, str(e)

    def place_protective_stop(self, ticker, asset_type, quantity, entry_price, stop_pct,
                              trail_pct=None):
        """
        Coinbase Advanced: stop-limit GTC sell (no native trailing).
        Limit sits slightly below stop for slippage buffer.
        """
        if not self.is_connected or not self.client:
            return False, None, "not connected"
        ok, reason = self._orders_allowed()
        if not ok:
            return False, None, reason
        try:
            clean = str(ticker).replace("-USD", "").upper()
            qty = float(quantity or 0)
            entry = float(entry_price or 0)
            stop_d = abs(float(stop_pct or 0))
            if qty <= 0 or entry <= 0 or stop_d <= 0:
                return False, None, "invalid qty/entry/stop"
            limits = self._get_product_limits(clean)
            inc = float(limits.get("base_increment", 0.00000001))
            d_inc = Decimal(str(inc))
            decimals = abs(d_inc.as_tuple().exponent)
            valid = (Decimal(str(qty)) / d_inc).quantize(Decimal("1"), rounding=ROUND_DOWN) * d_inc
            if float(valid) <= 0:
                return False, None, "qty below CB increment"
            stop_price = entry * (1.0 - stop_d)
            limit_price = stop_price * 0.995
            px_dec = 2 if entry >= 1.0 else 6
            stop_s = f"{stop_price:.{px_dec}f}"
            limit_s = f"{limit_price:.{px_dec}f}"
            size_s = format(float(valid), f".{decimals}f")
            client_order_id = f"prot-{int(time.time() * 1000)}"
            res = self._cb_call(
                self.client.stop_limit_order_gtc_sell,
                client_order_id=client_order_id,
                product_id=f"{clean}-USD",
                base_size=size_s,
                limit_price=limit_s,
                stop_price=stop_s,
                stop_direction="STOP_DIRECTION_STOP_DOWN",
            )
            data = self._cb_payload(res)
            if data.get("success"):
                oid = self._extract_order_id(data)
                return True, oid, f"CB stop-limit @ {stop_s}/{limit_s}"
            err = data.get("error_response") if data else data
            return False, None, f"CB stop-limit rejected: {err}"
        except Exception as e:
            return False, None, f"CB protective stop error: {e}"
