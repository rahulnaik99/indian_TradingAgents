from tradingagents.india import execution_audit as audit


def test_execution_audit_snapshots_full_registry_and_records_statuses(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "audit.sqlite3")
    run_id = audit.create_run("integrated_research", "SBIN", {"use_llm": False})
    audit.mark_component(run_id, "fyers_daily_history", "running", event_type="tool_started")
    audit.mark_component(run_id, "fyers_daily_history", "completed", details={"candles": 42})
    audit.mark_component(run_id, "fyers_15m_history", "skipped", reason="Not requested.")
    audit.finish_run(run_id, "completed", {"technical_bias": "mixed_or_neutral"})

    run = audit.get_run(run_id)
    assert run is not None
    by_id = {item["component_id"]: item for item in run["components"]}
    assert len(by_id) == len(audit.COMPONENTS)
    assert by_id["fyers_daily_history"]["status"] == "completed"
    assert by_id["fyers_daily_history"]["invocation_count"] == 1
    assert by_id["fyers_daily_history"]["details"]["candles"] == 42
    assert by_id["fyers_15m_history"]["status"] == "skipped"
    # Registered for integrated_research, but no invocation was recorded in this test.
    assert by_id["upstream_market_analyst"]["status"] == "not_invoked"
    assert any(event["event_type"] == "tool_started" for event in run["events"])


def test_failed_run_marks_unreached_components(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "audit.sqlite3")
    run_id = audit.create_run("integrated_research", "SBIN")
    audit.mark_component(run_id, "fyers_daily_history", "running")
    audit.finish_run(run_id, "failed", {"error_type": "RuntimeError"})
    run = audit.get_run(run_id)
    by_id = {item["component_id"]: item for item in run["components"]}
    assert by_id["fyers_daily_history"]["status"] == "failed"
    assert by_id["technical_analysis"]["status"] == "not_reached"
