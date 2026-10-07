from niftystocks import ns
import pandas as pd
import yfinance as yf

# Fetches the Nifty 50 list with the '.NS' suffix ready for yfinance
nse_tickers = ns.get_nifty50_with_ns()

print(f"Successfully loaded {len(nse_tickers)} tickers!")
print(nse_tickers[:]) 
# Output: ['ADANIENT.NS', 'ADANIPORTS.NS', 'APOLLOHOSP.NS', 'ASIANPAINT.NS', 'AXISBANK.NS']
screened_data = []

for ticker in nse_tickers:
    stock = yf.Ticker(ticker)
    info = stock.info
    
    # Extract metrics safely
    price = info.get('currentPrice', None)
    pe_ratio = info.get('trailingPE', None)
    market_cap = info.get('marketCap', None)
    
    # Define screening condition: e.g., P/E ratio under 30 and valid price
    if price and pe_ratio and pe_ratio < 30:
        screened_data.append({
            'Ticker': ticker,
            'Price': price,
            'P/E Ratio': pe_ratio,
            'Market Cap': market_cap
        })

# Convert results into a DataFrame
df_screened = pd.DataFrame(screened_data)
print(df_screened)
