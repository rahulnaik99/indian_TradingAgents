from tradingagents.india.fundamentals import refresh_yahoo_snapshot
from tradingagents.india.storage import IndiaStore


def test_refresh_yahoo_snapshot_persists_sourced_asof_metrics(tmp_path, monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            assert symbol == "CANBK.NS"
        def get_info(self):
            return {"trailingPE": 7.2, "priceToBook": 0.9, "returnOnEquity": 0.12, "debtToEquity": 850.0, "trailingEps": 22.5, "dividendYield": 0.025, "marketCap": 100000000000}
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    store = IndiaStore(tmp_path / "facts.sqlite3")
    result = refresh_yahoo_snapshot("CANBK", store)
    assert result["inserted"] == 7
    facts = store.list_facts("CANBK")
    assert len(facts) == 7
    assert all(f["source_name"] == "Yahoo Finance (yfinance)" for f in facts)
    assert all(f["as_of_date"] == result["as_of_date"] for f in facts)
    assert all(f["observation_type"] == "retrieval_time_snapshot" for f in facts)
    dividend = next(f for f in facts if f["metric_code"] == "DIVIDEND_YIELD")
    assert dividend["value"] == 2.5
    assert dividend["unit"] == "percent"


def test_refresh_yahoo_snapshot_omits_missing_values(tmp_path, monkeypatch):
    class FakeTicker:
        def __init__(self, symbol): pass
        def get_info(self): return {"trailingPE": 5.0}
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    result = refresh_yahoo_snapshot("CANBK", IndiaStore(tmp_path / "facts.sqlite3"))
    assert result["inserted"] == 1
    assert "ROE" in result["unavailable_metrics"]
