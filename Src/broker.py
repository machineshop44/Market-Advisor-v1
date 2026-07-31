import time
import math
import importlib
from decimal import Decimal, ROUND_DOWN, ROUND_UP


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

    def login(self, credentials): raise NotImplementedError
    def logout(self): raise NotImplementedError
    def get_account_balances(self): raise NotImplementedError
    def get_current_holdings(self): raise NotImplementedError
    def get_live_price(self, ticker, allow_yahoo_fallback=True): raise NotImplementedError
    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
                        market_hours="regular_hours", allow_fractional=True): raise NotImplementedError
    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                         market_hours="regular_hours", allow_fractional=True): raise NotImplementedError
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

class RobinhoodAdapter(BaseBroker):
    """The Robinhood translation layer."""
    def __init__(self):
        super().__init__()
        self.broker_id = "ROBINHOOD"
        self._crypto_inc_cache = {}

    def login(self, credentials):
        try:
            if credentials.get('email') and credentials.get('password'):
                login_data = r.login(username=credentials['email'], password=credentials['password'], store_session=credentials.get('store_session', True))
                if login_data and 'access_token' in login_data:
                    self.is_connected = True
                    return True, "Success"
            
            profile = r.profiles.load_account_profile()
            if profile and "account_number" in profile:
                self.is_connected = True
                return True, "Session Verified"
            return False, "Invalid Credentials"
        except Exception as e:
            return False, str(e)

    def logout(self):
        try: r.logout()
        except Exception: pass
        self.is_connected = False

    def get_account_balances(self):
        if not self.is_connected: return 0.0, 0.0
        try:
            acc = r.profiles.load_account_profile()
            portfolio_value = float(acc.get('portfolio_equity', 0) or acc.get('equity', 0) or 0.0)
            cash = max(float(acc.get('buying_power', 0)), float(acc.get('cash', 0)))
            
            if portfolio_value == 0.0:
                holdings = r.build_holdings()
                if holdings:
                    for t, d in holdings.items():
                        portfolio_value += float(d.get('equity', 0) or (float(d.get('quantity', 0)) * float(d.get('price', 0))))
                portfolio_value += cash
                
            crypto_positions = r.crypto.get_crypto_positions()
            if crypto_positions:
                for pos in crypto_positions:
                    qty = float(pos.get('quantity', 0))
                    if qty > 0:
                        symbol = pos['currency']['code']
                        live_price = self.get_live_price(symbol)
                        portfolio_value += (qty * live_price)
            return portfolio_value, cash
        except Exception: return 0.0, 0.0

    def get_current_holdings(self):
        assets = []
        if not self.is_connected: return assets
        try:
            holdings = r.build_holdings()
            if holdings:
                for ticker, data in holdings.items():
                    qty = float(data.get('quantity', 0))
                    if qty > 0:
                        cost = float(data.get('average_buy_price', 0))
                        assets.append({'ticker': ticker, 'shares': qty, 'cost': cost, 'type': 'Ready (Stock)'})
        except Exception: pass

        try:
            crypto_positions = r.crypto.get_crypto_positions()
            if crypto_positions:
                for pos in crypto_positions:
                    qty = float(pos.get('quantity', 0))
                    if qty > 0:
                        symbol = pos['currency']['code']
                        cb = pos.get('cost_bases')
                        avg_cost = float(cb[0].get('direct_cost_basis', 0)) / qty if cb and len(cb) > 0 and qty > 0 else 0.0
                        assets.append({'ticker': symbol, 'shares': qty, 'cost': avg_cost, 'type': 'Ready (Crypto)'})
        except Exception: pass
        return assets

    def get_live_price(self, ticker, allow_yahoo_fallback=True):
        clean = str(ticker).replace("-USD", "").upper()
        cryptos = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
        if clean in cryptos:
            try:
                q = r.crypto.get_crypto_quote(clean)
                if q and 'mark_price' in q and float(q['mark_price']) > 0: return float(q['mark_price'])
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
            info = r.crypto.get_crypto_info(ticker) or {}
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
        if notional < 1.0:
            return True, f"stock fractional under $1 (${notional:.4f})"
        return False, ""

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
                err = str(res)
                if "422" in err or res is None:
                    return f"Skipped: RH rejected small/invalid crypto size ({res})", 0.0, None
                return f"Fail: {res}", 0.0, None
            except Exception as e:
                err = str(e)
                if "422" in err:
                    return f"Skipped: RH 422 (size/limits) for {ticker}", 0.0, None
                return f"Fail: {e}", 0.0, None

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

    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                         market_hours="regular_hours", allow_fractional=True):
        is_crypto = "crypto" in asset_type.lower() or ticker.upper() in {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
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
                    return f"Crypto Sell {tag} ({safe_qty_str})", oid
                # RH sometimes returns validation errors as dict without id
                err = str(res)
                if "too small" in err.lower() or "at least" in err.lower():
                    return f"Skipped: {res}", None
                return f"Fail: {res}", None
            except Exception as e:
                err = str(e)
                if "too small" in err.lower() or "at least" in err.lower():
                    return f"Skipped: {err}", None
                return f"Fail: {e}", None

        if price <= 0:
            return "Skipped: No valid market price (delisted/untradeable)", None

        if shares_val >= 1.0:
            qty_to_sell = int(shares_val)
            limit_price = round(price * (1.0 - offset_pct), 4 if price < 1.0 else 2)
            try:
                res = r.order_sell_limit(symbol=ticker, quantity=qty_to_sell, limitPrice=limit_price, timeInForce='gfd', extendedHours=use_ext_hours)
                if isinstance(res, dict) and ("id" in res or "state" in res):
                    oid = res.get('id')
                    conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
                    tag = "Filled" if conf else f"Pending/{state}"
                    return f"Sell {tag} ({qty_to_sell})", oid
                return f"Fail: {res}", None
            except Exception as e:
                err = str(e)
                if "list index" in err.lower() or "not a valid" in err.lower():
                    return f"Skipped: Untradeable ticker ({err})", None
                return f"Fail: {e}", None

        # Fractional remainder / sub-1 share positions
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
                return f"Sell{suffix} {tag} ({shares_val})", oid
            err = str(res)
            if want_ext:
                return f"Skipped: Ext. Hours fractional not eligible ({err[:100]})", None
            return f"Fail: {res}", None
        except Exception as e:
            err = str(e)
            if "list index" in err.lower() or "not a valid" in err.lower():
                return f"Skipped: Untradeable ticker ({err})", None
            if use_ext_hours or market_hours == "extended_hours":
                return f"Skipped: Ext. Hours fractional rejected ({err[:100]})", None
            return f"Fail: {e}", None

    def confirm_order(self, order_id, is_crypto=False, timeout_sec=10):
        """Poll Robinhood until filled/cancelled/rejected or timeout."""
        if not order_id:
            return False, "no_id"
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
            # RH rejects >8 decimal qty and non-integer trailing_peg percentages.
            qty_dec = Decimal(str(qty)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if qty_dec <= 0:
                return False, None, "qty rounded to 0"
            qty_arg = int(qty_dec) if qty_dec == qty_dec.to_integral_value() else float(qty_dec)
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
        self.client = None

    def login(self, credentials):
        RESTClient = _get_rest_client_class()
        if not RESTClient or not COINBASE_AVAILABLE:
            return False, "coinbase-advanced-py not installed"
        
        api_key = credentials.get('api_key')
        api_secret = credentials.get('api_secret')
        
        if not api_key or not api_secret:
            return False, "Missing CDP API Key or Secret"
            
        try:
            self.client = RESTClient(api_key=api_key, api_secret=api_secret)
            # Make a test call to verify authentication
            res = self.client.get_accounts(limit=1)
            data = res.to_dict() if hasattr(res, 'to_dict') else res
            
            if data and 'accounts' in data:
                self.is_connected = True
                return True, "Success"
            return False, "Authentication Failed"
        except Exception as e:
            return False, str(e)

    def logout(self):
        self.client = None
        self.is_connected = False

    def _fetch_all_accounts(self):
        """Helper to bypass Coinbase's 49-item default pagination limit."""
        all_accounts = []
        cursor = ""
        try:
            while True:
                res = self.client.get_accounts(limit=250, cursor=cursor) if cursor else self.client.get_accounts(limit=250)
                data = res.to_dict() if hasattr(res, 'to_dict') else res
                all_accounts.extend(data.get('accounts', []))
                
                cursor = data.get('cursor', "")
                has_next = data.get('has_next', False)
                if not has_next or not cursor:
                    break
        except Exception as e:
            print(f"Coinbase pagination error: {e}")
        return all_accounts

    def get_account_balances(self):
        if not self.is_connected: return 0.0, 0.0
        try:
            total_value = 0.0
            buying_power = 0.0
            
            # Fetch all accounts via the pagination looper
            accounts = self._fetch_all_accounts()
            
            for acc in accounts:
                # Add available balance AND funds held in open limit orders
                avail = float(acc.get('available_balance', {}).get('value', 0))
                hold = float(acc.get('hold', {}).get('value', 0))
                total_qty = avail + hold
                currency = acc.get('currency')
                
                if total_qty > 0:
                    if currency == "USD" or currency == "USDC":
                        buying_power += avail  # Buying power is strictly available cash
                        total_value += total_qty
                    else:
                        price = self.get_live_price(currency)
                        total_value += (total_qty * price)
                        
            return total_value, buying_power
        except Exception as e:
            print(f"Coinbase get_account_balances error: {e}")
            return 0.0, 0.0

    def get_current_holdings(self):
        assets = []
        if not self.is_connected: return assets
        try:
            accounts = self._fetch_all_accounts()
            
            for acc in accounts:
                # Add available balance AND funds held in open limit orders
                avail = float(acc.get('available_balance', {}).get('value', 0))
                hold = float(acc.get('hold', {}).get('value', 0))
                total_qty = avail + hold
                currency = acc.get('currency')
                
                if total_qty > 0 and currency not in ["USD", "USDC"]:
                    # Do NOT use live price as cost — that zeros out ROI and blocks sells.
                    # GUI overlays tracked buy cost via cost_basis_cache.
                    assets.append({
                        'ticker': currency, 
                        'shares': total_qty, 
                        'cost': 0.0, 
                        'type': 'Ready (Crypto)'
                    })
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
                res = self.client.get_product(product_id=product_id)
                data = res.to_dict() if hasattr(res, 'to_dict') else res
                if data and data.get('price'):
                    price = float(data['price'])
                    if price > 0:
                        return price
            except Exception:
                pass
        if not allow_yahoo_fallback:
            return 0.0
        # yfinance crypto fallback only (never treat as stock)
        try:
            df = yf.Ticker(f"{clean}-USD").history(period="1d")
            if not df.empty:
                return float(df['Close'].iloc[-1])
        except Exception:
            pass
        return 0.0

    def _get_product_limits(self, ticker):
        """
        Return dict with base_increment, base_min_size, quote_min_size for a CB product.
        Cached per ticker.
        """
        clean = str(ticker).replace("-USD", "").upper()
        if not hasattr(self, '_product_limits_cache'):
            self._product_limits_cache = {}
        if clean in self._product_limits_cache:
            return self._product_limits_cache[clean]

        limits = {
            'base_increment': 0.00000001,
            'base_min_size': 0.0,
            'quote_min_size': 1.0,  # sensible default USD floor
        }
        if self.is_connected and self.client:
            try:
                prod = self.client.get_product(product_id=f"{clean}-USD")
                data = prod.to_dict() if hasattr(prod, 'to_dict') else prod
                if isinstance(data, dict):
                    for key in ('base_increment', 'base_min_size', 'quote_min_size', 'min_market_funds'):
                        raw = data.get(key)
                        if raw is None:
                            continue
                        try:
                            val = float(raw)
                        except Exception:
                            continue
                        if key == 'min_market_funds' and val > 0:
                            limits['quote_min_size'] = max(limits['quote_min_size'], val)
                        elif val > 0:
                            limits[key] = val
            except Exception:
                pass
        if limits['base_min_size'] <= 0:
            limits['base_min_size'] = limits['base_increment']
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
        inc = limits['base_increment']
        min_base = limits['base_min_size']
        min_quote = limits['quote_min_size']
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
        if not isinstance(data, dict):
            return None
        sr = data.get('success_response') or {}
        if isinstance(sr, dict) and sr.get('order_id'):
            return sr.get('order_id')
        return data.get('order_id') or data.get('id')

    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours,
                        market_hours="regular_hours", allow_fractional=True):
        if not self.is_connected: return "Fail: Not connected", 0.0, None
        clean = str(ticker).replace("-USD", "").upper()
        product_id = f"{clean}-USD"

        try:
            client_order_id = str(int(time.time() * 1000))
            res = self.client.market_order_buy(
                client_order_id=client_order_id,
                product_id=product_id,
                quote_size=str(round(trade_dollars, 2))
            )
            data = res.to_dict() if hasattr(res, 'to_dict') else res

            if data.get('success'):
                oid = self._extract_order_id(data)
                conf, state = self.confirm_order(oid, is_crypto=True) if oid else (False, "no_id")
                tag = "Filled" if conf else f"Pending/{state}"
                return f"Coinbase Buy {tag} ({trade_dollars:.2f})", trade_dollars, oid
            return f"Fail: {data.get('error_response', 'Unknown Error')}", 0.0, None
        except Exception as e:
            return f"Fail: {e}", 0.0, None

    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours,
                         market_hours="regular_hours", allow_fractional=True):
        if not self.is_connected: return "Fail: Not connected", None
        clean = str(ticker).replace("-USD", "").upper()
        product_id = f"{clean}-USD"

        try:
            dust, reason = self.position_is_dust(clean, shares_val, price, asset_type)
            if dust:
                return f"Skipped: Dust ({reason})", None

            limits = self._get_product_limits(clean)
            base_increment = float(limits.get('base_increment', 0.00000001))

            d_inc = Decimal(str(base_increment))
            decimals = abs(d_inc.as_tuple().exponent)
            d_qty = Decimal(str(shares_val))
            valid_qty_dec = (d_qty / d_inc).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_inc

            if valid_qty_dec <= 0: return "Skipped: Quantity too small", None
            safe_qty_str = format(float(valid_qty_dec), f".{decimals}f")

            client_order_id = str(int(time.time() * 1000))
            res = self.client.market_order_sell(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=safe_qty_str
            )
            data = res.to_dict() if hasattr(res, 'to_dict') else res

            if data.get('success'):
                oid = self._extract_order_id(data)
                conf, state = self.confirm_order(oid, is_crypto=True) if oid else (False, "no_id")
                tag = "Filled" if conf else f"Pending/{state}"
                return f"Coinbase Sell {tag} ({safe_qty_str})", oid
            return f"Fail: {data.get('error_response', 'Unknown Error')}", None
        except Exception as e:
            return f"Fail: {e}", None

    def confirm_order(self, order_id, is_crypto=False, timeout_sec=10):
        """Poll Coinbase until FILLED/CANCELLED/FAILED or timeout."""
        if not order_id or not self.client:
            return False, "no_id"
        deadline = time.time() + timeout_sec
        last_state = "unknown"
        while time.time() < deadline:
            try:
                res = self.client.get_order(order_id)
                data = res.to_dict() if hasattr(res, 'to_dict') else res
                order = data.get('order') if isinstance(data, dict) else None
                if isinstance(order, dict):
                    last_state = str(order.get('status') or "unknown").upper()
                elif isinstance(data, dict):
                    last_state = str(data.get('status') or "unknown").upper()
                if last_state in ("FILLED", "COMPLETED"):
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
            res = self.client.cancel_orders([str(order_id)])
            data = res.to_dict() if hasattr(res, "to_dict") else res
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
            # Format prices sensibly
            px_dec = 2 if entry >= 1.0 else 6
            stop_s = f"{stop_price:.{px_dec}f}"
            limit_s = f"{limit_price:.{px_dec}f}"
            size_s = format(float(valid), f".{decimals}f")
            client_order_id = f"prot-{int(time.time() * 1000)}"
            res = self.client.stop_limit_order_gtc_sell(
                client_order_id=client_order_id,
                product_id=f"{clean}-USD",
                base_size=size_s,
                limit_price=limit_s,
                stop_price=stop_s,
                stop_direction="STOP_DIRECTION_STOP_DOWN",
            )
            data = res.to_dict() if hasattr(res, "to_dict") else res
            if isinstance(data, dict) and data.get("success"):
                oid = self._extract_order_id(data)
                return True, oid, f"CB stop-limit @ {stop_s}/{limit_s}"
            err = data.get("error_response") if isinstance(data, dict) else data
            return False, None, f"CB stop-limit rejected: {err}"
        except Exception as e:
            return False, None, f"CB protective stop error: {e}"