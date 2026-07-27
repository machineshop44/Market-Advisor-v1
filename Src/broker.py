import time
import math
from decimal import Decimal, ROUND_DOWN
import robin_stocks.robinhood as r

# Attempt to import Coinbase Advanced SDK
try:
    from coinbase.rest import RESTClient
    COINBASE_AVAILABLE = True
except ImportError:
    COINBASE_AVAILABLE = False

# Fallback crypto fetcher
try:
    import yfinance as yf
except ImportError:
    pass

class BaseBroker:
    """The master interface that all broker adapters must strictly follow."""
    def __init__(self):
        self.is_connected = False
        self.broker_id = "BASE"

    def login(self, credentials): raise NotImplementedError
    def logout(self): raise NotImplementedError
    def get_account_balances(self): raise NotImplementedError
    def get_current_holdings(self): raise NotImplementedError
    def get_live_price(self, ticker): raise NotImplementedError
    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours): raise NotImplementedError
    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours): raise NotImplementedError
    def confirm_order(self, order_id, is_crypto=False, timeout_sec=10):
        return False, "unsupported"


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

    def get_live_price(self, ticker):
        clean = str(ticker).replace("-USD", "").upper()
        cryptos = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
        if clean in cryptos:
            try:
                q = r.crypto.get_crypto_quote(clean)
                if q and 'mark_price' in q and float(q['mark_price']) > 0: return float(q['mark_price'])
            except Exception: pass
            try:
                df = yf.Ticker(f"{clean}-USD").history(period="1d")
                if not df.empty: return float(df['Close'].iloc[-1])
            except Exception: pass
            return 1.0
        try:
            p = r.stocks.get_latest_price(clean)
            if p and len(p) > 0: return float(p[0])
        except Exception: pass
        return 1.0

    def _get_crypto_qty_increment(self, ticker):
        if ticker not in self._crypto_inc_cache:
            try:
                info = r.crypto.get_crypto_info(ticker)
                inc = info.get('min_order_quantity_increment')
                self._crypto_inc_cache[ticker] = float(inc) if inc else (1.0 if ticker in ["PEPE", "SHIB", "BONK"] else 0.000001)
            except Exception:
                self._crypto_inc_cache[ticker] = 1.0 if ticker in ["PEPE", "SHIB", "BONK"] else 0.000001
        return self._crypto_inc_cache[ticker]

    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours):
        is_crypto = "crypto" in asset_type.lower() or ticker.upper() in {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
        if is_crypto:
            inc = self._get_crypto_qty_increment(ticker)
            d_inc = Decimal(str(inc))
            decimals = abs(d_inc.as_tuple().exponent)
            d_qty = Decimal(str(trade_dollars / price))
            valid_qty_dec = (d_qty / d_inc).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_inc
            if valid_qty_dec <= 0: return f"Skipped: Cannot afford 1 increment", 0.0, None
            safe_qty_str = format(float(valid_qty_dec), f".{decimals}f")
            actual_spent = float(valid_qty_dec) * price
            try:
                res = r.order_buy_crypto_by_quantity(ticker, safe_qty_str)
                if isinstance(res, dict) and 'id' in res:
                    oid = res['id']
                    conf, state = self.confirm_order(oid, is_crypto=True)
                    tag = "Filled" if conf else f"Pending/{state}"
                    return f"Crypto Buy {tag} ({actual_spent:.2f})", actual_spent, oid
                return f"Fail: {res}", 0.0, None
            except Exception as e: return f"Fail: {e}", 0.0, None

        if use_ext_hours:
            qty_to_buy = int(trade_dollars / price) if price > 0 else 0
            if qty_to_buy < 1: return "Skipped: Cannot afford 1 whole share for Ext. Hours.", 0.0, None
            limit_price = round(price * (1.0 + offset_pct), 4 if price < 1.0 else 2)
            res = r.order_buy_limit(symbol=ticker, quantity=qty_to_buy, limitPrice=limit_price, timeInForce='gfd', extendedHours=True)
            if isinstance(res, dict) and ("id" in res or "state" in res):
                oid = res.get('id')
                conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
                tag = "Filled" if conf else f"Pending/{state}"
                return f"Buy Limit {tag} ({qty_to_buy})", (qty_to_buy * limit_price), oid
            return f"Fail: {res}", 0.0, None

        res = r.order_buy_fractional_by_price(ticker, trade_dollars, timeInForce='gfd')
        if isinstance(res, dict) and ("id" in res or "state" in res):
            oid = res.get('id')
            conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
            tag = "Filled" if conf else f"Pending/{state}"
            return f"Buy Fractional {tag} ({trade_dollars:.2f})", trade_dollars, oid
        return f"Fail: {res}", 0.0, None

    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours):
        is_crypto = "crypto" in asset_type.lower() or ticker.upper() in {"BTC", "ETH", "SOL", "DOGE", "SHIB", "PEPE", "BONK", "XLM", "AVAX", "LINK", "UNI"}
        if is_crypto:
            inc = self._get_crypto_qty_increment(ticker)
            d_inc = Decimal(str(inc))
            decimals = abs(d_inc.as_tuple().exponent)
            d_qty = Decimal(str(shares_val))
            valid_qty_dec = (d_qty / d_inc).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_inc
            if valid_qty_dec <= 0: return "Skipped: Quantity too small", None
            safe_qty_str = format(float(valid_qty_dec), f".{decimals}f")
            try:
                res = r.order_sell_crypto_by_quantity(ticker, safe_qty_str)
                if isinstance(res, dict) and 'id' in res:
                    oid = res['id']
                    conf, state = self.confirm_order(oid, is_crypto=True)
                    tag = "Filled" if conf else f"Pending/{state}"
                    return f"Crypto Sell {tag} ({safe_qty_str})", oid
                return f"Fail: {res}", None
            except Exception as e: return f"Fail: {e}", None

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
            except Exception as e: return f"Fail: {e}", None

        if use_ext_hours: return "Skipped: Ext. Hours blocks fractional sells.", None
        if (shares_val * price) < 1.00: return f"Fail: Fractional value under $1.00", None

        res = r.order_sell_fractional_by_quantity(ticker, shares_val, timeInForce='gfd')
        if isinstance(res, dict) and ("id" in res or "state" in res):
            oid = res.get('id')
            conf, state = self.confirm_order(oid, is_crypto=False) if oid else (False, "unknown")
            tag = "Filled" if conf else f"Pending/{state}"
            return f"Sell {tag} ({shares_val})", oid
        return f"Fail: {res}", None

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


class CoinbaseAdapter(BaseBroker):
    """The Coinbase Advanced Trade translation layer."""
    def __init__(self):
        super().__init__()
        self.broker_id = "COINBASE"
        self.client = None

    def login(self, credentials):
        if not COINBASE_AVAILABLE:
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

    def get_live_price(self, ticker):
        clean = str(ticker).replace("-USD", "").upper()
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
        # yfinance fallback (paper mode / API hiccups)
        try:
            df = yf.Ticker(f"{clean}-USD").history(period="1d")
            if not df.empty:
                return float(df['Close'].iloc[-1])
        except Exception:
            pass
        return 0.0

    def _extract_order_id(self, data):
        if not isinstance(data, dict):
            return None
        sr = data.get('success_response') or {}
        if isinstance(sr, dict) and sr.get('order_id'):
            return sr.get('order_id')
        return data.get('order_id') or data.get('id')

    def place_buy_order(self, ticker, asset_type, price, trade_dollars, offset_pct, use_ext_hours):
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

    def place_sell_order(self, ticker, asset_type, price, shares_val, offset_pct, use_ext_hours):
        if not self.is_connected: return "Fail: Not connected", None
        clean = str(ticker).replace("-USD", "").upper()
        product_id = f"{clean}-USD"

        try:
            prod = self.client.get_product(product_id=product_id)
            prod_data = prod.to_dict() if hasattr(prod, 'to_dict') else prod
            base_increment = float(prod_data.get('base_increment', 0.00000001))

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