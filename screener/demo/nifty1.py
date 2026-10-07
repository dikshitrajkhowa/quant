import yfinance as yf

stock = yf.Ticker('ADANIENT.NS')
# info = stock.info
print(type(stock))


# List all public methods/attributes in the library
methods = [m for m in dir(yf.ticker.Ticker) if not m.startswith('_')]
print(methods)