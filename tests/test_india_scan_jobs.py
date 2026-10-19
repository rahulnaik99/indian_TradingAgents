from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import tradingagents.web.app as web_app
from tradingagents.india.contracts import OHLCVCandle, SwingTimeframe
from tradingagents.india.storage import IndiaStore


class FakeFyers:
    def get_history(self, symbol, start_date, end_date, timeframe):
        tf = SwingTimeframe(timeframe)
        step = {"1d": timedelta(days=1), "1h": timedelta(hours=1), "15m": timedelta(minutes=15)}[timeframe]
        start = datetime(2026, 1, 1, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
        out = []
        price = 100.0
        for i in range(230):
            price += 0.1
            if timeframe == "1h":
                open_time = start + timedelta(days=i // 7, hours=i % 7)
                close_time = min(open_time + timedelta(hours=1), open_time.replace(hour=15, minute=30)) if open_time.hour == 15 else open_time + timedelta(hours=1)
            else:
                open_time = start + i * step
                close_time = open_time + step
            out.append(OHLCVCandle(symbol=symbol, timeframe=tf, open_time=open_time,
                close_time=close_time, open=price - 0.05, high=price + 0.2,
                low=price - 0.2, close=price, volume=1000, source_provider="fake",
                retrieved_at=close_time, is_complete=True))
        return tuple(out)


def test_scan_job_storage_roundtrip(tmp_path):
    store = IndiaStore(tmp_path / "jobs.sqlite3")
    store.create_scan_job("job1", {"symbols": ["SBIN"]}, "2026-10-10T00:00:00+00:00")
    store.update_scan_job("job1", status="completed", now="2026-10-10T00:01:00+00:00", result={"count_completed": 1})
    job = store.get_scan_job("job1")
    assert job["status"] == "completed"
    assert job["params"]["symbols"] == ["SBIN"]
    assert job["result"]["count_completed"] == 1


def test_batch_scan_worker_records_result(monkeypatch, tmp_path):
    store = IndiaStore(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(web_app, "STORE", store)
    monkeypatch.setattr(web_app, "_fyers_client", lambda: FakeFyers())
    store.create_scan_job("job2", {"symbols": ["SBIN"]}, "2026-10-10T00:00:00+00:00")
    web_app._run_scan_job("job2", ["SBIN"], 365, False)
    job = store.get_scan_job("job2")
    assert job["status"] in {"completed", "completed_with_errors"}
    assert job["result"]["count_requested"] == 1
    assert job["result"]["count_completed"] == 1


def test_scan_endpoint_fails_fast_without_fyers_credentials(monkeypatch):
    monkeypatch.delenv("FYERS_APP_ID", raising=False)
    monkeypatch.delenv("FYERS_ACCESS_TOKEN", raising=False)
    client = TestClient(web_app.app)
    response = client.post("/api/scan", json={"symbols": [], "lookback_days": 365})
    assert response.status_code == 503
