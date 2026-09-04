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
