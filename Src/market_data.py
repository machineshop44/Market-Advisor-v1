def fetch_current_price(ticker_symbol):
    """Fetches current price with a fallback from yfinance to Robinhood."""
    # Lazy imports — avoid loading pandas/yfinance/robin at app startup
    try:
        import yfinance as yf
        ticker = yf.Ticker(ticker_symbol)
        price = ticker.fast_info.get('lastPrice')
        if price and price > 0:
            return float(price)
    except Exception:
        pass

    try:
        import robin_stocks.robinhood as r
        quote = r.stocks.get_latest_price(ticker_symbol, includeExtendedHours=True)
        if quote and len(quote) > 0 and quote[0] is not None:
            return float(quote[0])
    except Exception:
        pass

    return 0.0


def fetch_historical_data(ticker_symbol, period="1y"):
    """Fetches historical data with fallback mechanisms."""
    import pandas as pd

    try:
        import yfinance as yf
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period=period)
        if hist is not None and not hist.empty:
            return hist
    except Exception:
        pass

    try:
        import robin_stocks.robinhood as r
        span_map = {"1mo": "month", "3mo": "3month", "6mo": "3month", "1y": "year", "2y": "5year"}
        span = span_map.get(period, "year")

        historicals = r.stocks.get_stock_historicals(ticker_symbol, interval="day", span=span)
        if historicals and isinstance(historicals, list) and len(historicals) > 0:
            df = pd.DataFrame(historicals)
            df['Close'] = df['close_price'].astype(float)
            df['Open'] = df['open_price'].astype(float)
            df['High'] = df['high_price'].astype(float)
            df['Low'] = df['low_price'].astype(float)
            df['Volume'] = df['volume'].astype(float)
            if 'begins_at' in df.columns:
                df['Date'] = pd.to_datetime(df['begins_at'])
                df.set_index('Date', inplace=True)
            return df[['Open', 'High', 'Low', 'Close', 'Volume']]
    except Exception:
        pass

    return pd.DataFrame()
