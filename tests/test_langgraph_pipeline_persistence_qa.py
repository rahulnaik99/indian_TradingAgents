from __future__ import annotations

from tradingagents.india.langgraph_pipeline.analysis_qa import answer_analysis_question
from tradingagents.india.langgraph_pipeline.persistence import AnalysisStore


def _state() -> dict:
    return {
        "run_id": "run-123",
        "request": {"symbol": "INFY", "exchange": "NSE", "as_of": "2026-10-01T10:00:00+05:30", "timeframes": ["1d"], "analysis_mode": "historical"},
        "status": "partial",
        "fundamental_analysis": {"status": "ok", "summary": "Revenue growth improved year over year."},
        "technical_analysis": {"status": "limited", "summary": "Price remained above EMA 200."},
        "final_verdict": {"verdict": "NEEDS_REVIEW", "rationale": ["Quarterly results were unavailable."], "live_order_placed": False},
    }


def test_persist_and_retrieve_analysis(tmp_path):
    store = AnalysisStore(tmp_path / "analysis.sqlite3")
    analysis_id = store.save_analysis(_state())
    loaded = store.get_analysis(analysis_id)
    assert loaded is not None
    assert loaded["request"]["symbol"] == "INFY"
    assert loaded["final_verdict"]["verdict"] == "NEEDS_REVIEW"
    assert store.list_analyses("infy")[0]["analysis_id"] == analysis_id


def test_qna_uses_saved_analysis_and_persists_question(tmp_path):
    store = AnalysisStore(tmp_path / "analysis.sqlite3")
    analysis_id = store.save_analysis(_state())
    result = answer_analysis_question(store, analysis_id, "Why was this marked review?")
    assert result["answer_type"] == "saved_analysis"
    assert "NEEDS_REVIEW" in result["answer"]
    assert store.list_questions(analysis_id)[0]["question"] == "Why was this marked review?"


def test_qna_rejects_future_research_for_historical_cutoff(tmp_path):
    store = AnalysisStore(tmp_path / "analysis.sqlite3")
    analysis_id = store.save_analysis(_state())

    def researcher(question, analysis, cutoff):
        return {"summary": "Later quarterly result published", "published_at": "2026-10-02T09:00:00+05:30"}

    result = answer_analysis_question(store, analysis_id, "Any quarterly results?", researcher=researcher)
    assert result["additional_research"] is None
    assert any("excluded" in item for item in result["limitations"])


def test_qna_allows_research_published_before_cutoff(tmp_path):
    store = AnalysisStore(tmp_path / "analysis.sqlite3")
    analysis_id = store.save_analysis(_state())

    def researcher(question, analysis, cutoff):
        return {"summary": "Official filing", "published_at": "2026-09-30T09:00:00+05:30", "source_url": "https://example.invalid/filing"}

    result = answer_analysis_question(store, analysis_id, "Find filing", researcher=researcher)
    assert result["additional_research"]["summary"] == "Official filing"
    assert result["answer_type"] == "saved_analysis_plus_research"


def test_qna_rejects_research_when_saved_cutoff_is_invalid(tmp_path):
    store = AnalysisStore(tmp_path / "analysis.sqlite3")
    state = _state()
    state["request"]["as_of"] = "not-a-timestamp"
    analysis_id = store.save_analysis(state)

    def researcher(question, analysis, cutoff):
        return {"summary": "Unverified research", "published_at": "2026-09-30T09:00:00+05:30"}

    result = answer_analysis_question(store, analysis_id, "Find filing", researcher=researcher)
    assert result["additional_research"] is None
    assert any("excluded" in item for item in result["limitations"])


def test_sqlite_enforces_question_foreign_key(tmp_path):
    import sqlite3

    store = AnalysisStore(tmp_path / "analysis.sqlite3")
    try:
        with store.connect() as connection:
            connection.execute(
                "INSERT INTO analysis_questions VALUES (?, ?, ?, ?, ?)",
                ("q-1", "missing-analysis", "2026-10-01T10:00:00+05:30", "question", "{}"),
            )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("SQLite should reject a question for an unknown analysis")
