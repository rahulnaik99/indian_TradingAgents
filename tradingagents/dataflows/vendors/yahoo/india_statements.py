"""Yahoo ticker handle for the India statement ingestion.

The yfinance import lives in the data layer (see tests/test_layering.py);
``tradingagents.india.statements`` calls ``open_ticker`` and reads the statement
DataFrames from the returned object.
"""
import yfinance as yf


def open_ticker(provider_symbol: str):
    return yf.Ticker(provider_symbol)
