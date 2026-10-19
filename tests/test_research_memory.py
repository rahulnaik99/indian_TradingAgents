import json

from tradingagents.india import research_memory


def test_save_and_list_snapshots_persist_across_calls(tmp_path, monkeypatch):
    db = tmp_path / "memory.sqlite3"
    monkeypatch.setenv("TRADINGAGENTS_DB_PATH", str(db))
    analysis = {
        "run_id": "run-001", "as_of": "2026-10-15T10:00:00+05:30",
        "final_agent_rating": "BUY", "final_conclusion": "Bullish if support holds.",
        "technical": {"multi_timeframe_bias": "bullish", "multi_timeframe": {"1D": {"ema20": 100}}},
        "assessment": {"technical_bias": "bullish", "risk_flag_count": 1},
        "data_quality_warnings": ["Delayed candle data"],
    }
    saved = research_memory.save_snapshot("SBIN", analysis)
    assert saved["run_id"] == "run-001"
    listed = research_memory.list_snapshots("SBIN")
    assert len(listed) == 1
    assert listed[0]["rating"] == "BUY"
    assert listed[0]["data_quality_warnings"] == ["Delayed candle data"]


def test_compare_snapshots_reports_outlook_and_warning_changes():
    previous = {"run_id": "old", "as_of": "2026-10-14", "rating": "BUY", "technical_bias": "bullish",
                "conclusion": "Old", "workflow_status": "completed", "assessment": {"risk_flag_count": 1},
                "data_quality_warnings": ["Missing news timestamp"]}
    current = {"run_id": "new", "as_of": "2026-10-15", "rating": "REVIEW", "technical_bias": "mixed",
               "conclusion": "New", "workflow_status": "completed", "assessment": {"risk_flag_count": 2},
               "data_quality_warnings": ["Delayed price data"]}
    result = research_memory.compare_snapshots(previous, current)
    assert result["assessment"] == "outlook_or_bias_changed"
    assert {item["field"] for item in result["changes"]} >= {"rating", "technical_bias", "assessment.risk_flag_count"}
    assert "Delayed price data" in result["new_data_quality_warnings"]
    assert "Missing news timestamp" in result["resolved_data_quality_warnings"]


def test_duplicate_run_id_updates_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DB_PATH", str(tmp_path / "memory.sqlite3"))
    research_memory.save_snapshot("SBIN", {"run_id": "same", "final_agent_rating": "BUY"})
    research_memory.save_snapshot("SBIN", {"run_id": "same", "final_agent_rating": "REVIEW"})
    runs = research_memory.list_snapshots("SBIN")
    assert len(runs) == 1
    assert runs[0]["rating"] == "REVIEW"


def test_snapshot_extracts_technical_metrics_news_and_catalysts(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DB_PATH", str(tmp_path / "memory.sqlite3"))
    analysis = {
        "run_id": "run-rich", "as_of": "2026-10-15T10:00:00+05:30",
        "final_agent_rating": "BUY",
        "technical": {"multi_timeframe": {"1D": {"ema20": 100.0, "ema50": 98.0, "rsi": 61.0}}},
        "evidence_package": {
            "news_and_events": [{"title": "Results announced", "published_at": "2026-10-14T09:00:00+05:30", "url": "https://example.com/results", "source": "Example"}],
            "calendar_events": [{"title": "Board meeting", "event_date": "2026-10-20", "known_at_utc": "2026-10-10T00:00:00+00:00", "url": "https://example.com/calendar", "source": "Exchange"}],
            "web_research": {"sources": [{"title": "Filing", "url": "https://example.com/filing", "published_at": "2026-10-14", "source": "Exchange"}]},
        },
    }
    snapshot = research_memory.save_snapshot("SBIN", analysis)
    assert snapshot["technical_metrics"]["1D.ema20"] == 100.0
    assert snapshot["news_evidence"][0]["title"] == "Results announced"
    assert snapshot["catalyst_evidence"][0]["title"] == "Board meeting"


def test_compare_reports_technical_news_and_catalyst_changes():
    previous = {
        "run_id": "old", "as_of": "2026-10-10T10:00:00+05:30", "rating": "BUY", "technical_bias": "bullish",
        "technical_metrics": {"1D.ema20": 100.0, "1D.rsi": 55.0},
        "news_evidence": [{"signature": "news|sbin|old headline|2026-10-09||", "title": "Old headline"}],
        "catalyst_evidence": [{"signature": "calendar|sbin|board meeting|2026-10-12|https://example.com/board", "title": "Board meeting", "date": "2026-10-12", "source": "Exchange"}],
        "data_quality_warnings": [],
    }
    current = {
        "run_id": "new", "as_of": "2026-10-15T10:00:00+05:30", "rating": "REVIEW", "technical_bias": "mixed",
        "technical_metrics": {"1D.ema20": 102.0, "1D.rsi": 48.0, "1D.ema50": 99.0},
        "news_evidence": [{"signature": "news|sbin|new headline|2026-10-14||", "title": "New headline"}],
        "catalyst_evidence": [{"signature": "calendar|sbin|board meeting|2026-10-12|https://example.com/board", "title": "Board meeting", "date": "2026-10-12", "source": "Exchange"}],
        "data_quality_warnings": [],
    }
    result = research_memory.compare_snapshots(previous, current)
    assert result["assessment"] == "outlook_or_bias_changed"
    assert any(row["metric"] == "1D.ema20" and row["absolute_change"] == 2.0 for row in result["technical_metric_changes"])
    assert any(row["metric"] == "1D.ema50" and row["change_type"] == "new_metric" for row in result["technical_metric_changes"])
    assert result["evidence_deltas"]["news_evidence"]["added"][0]["title"] == "New headline"
    assert result["catalyst_changes"]["past_upcoming_transitions"][0]["current_state"] == "past"
    assert "occurrence and outcome are not verified" in " ".join(result["limitations"])
