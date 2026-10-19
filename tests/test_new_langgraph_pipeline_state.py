from __future__ import annotations

import pytest

from tradingagents.india.langgraph_pipeline.contracts import validate_final_verdict, validate_request


def test_validate_request_normalizes_symbol_and_defaults():
    request = validate_request({"symbol": "reliance", "as_of": "2026-10-18T09:30:00+05:30"})
    assert request["symbol"] == "RELIANCE"
    assert request["exchange"] == "NSE"
    assert request["timeframes"] == ["1d", "4h", "1h", "15m"]
    assert request["analysis_mode"] == "historical"


@pytest.mark.parametrize("as_of", ["2026-10-18", "not-a-date", None])
def test_validate_request_rejects_missing_or_naive_cutoff(as_of):
    with pytest.raises(ValueError):
        validate_request({"symbol": "RELIANCE", "as_of": as_of})


def test_validate_request_rejects_unknown_timeframe():
    with pytest.raises(ValueError, match="unsupported timeframes"):
        validate_request({"symbol": "RELIANCE", "as_of": "2026-10-18T09:30:00+05:30", "timeframes": ["1m"]})


def test_validate_final_verdict_enforces_research_only_contract():
    verdict = validate_final_verdict({"verdict": "NO_TRADE", "confidence": 0.3})
    assert verdict["live_order_placed"] is False
    assert verdict["data_quality"] == "insufficient"


def test_validate_final_verdict_rejects_order_claim():
    with pytest.raises(ValueError, match="must never claim"):
        validate_final_verdict({"verdict": "BUY", "confidence": 0.7, "live_order_placed": True})
