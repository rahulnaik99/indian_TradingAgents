from pathlib import Path

from tradingagents.agents.context import get_evidence_discipline_instruction


def test_shared_evidence_discipline_covers_india_package_domains():
    instruction = get_evidence_discipline_instruction()
    for term in (
        "market breadth/Market Movers",
        "news_and_events",
        "calendar_events",
        "financial_facts",
        "fundamental_analysis",
        "stated cutoff",
        "entry/stop levels",
        "final decision",
    ):
        assert term in instruction


def test_existing_decision_agents_attach_shared_evidence_discipline():
    root = Path(__file__).resolve().parents[1] / "tradingagents" / "agents"
    paths = [
        root / "analysts" / "market_analyst.py",
        root / "analysts" / "news_analyst.py",
        root / "analysts" / "fundamentals_analyst.py",
        root / "researchers" / "bull_researcher.py",
        root / "researchers" / "bear_researcher.py",
        root / "managers" / "research_manager.py",
        root / "trader" / "trader.py",
        root / "risk_mgmt" / "aggressive_debator.py",
        root / "risk_mgmt" / "conservative_debator.py",
        root / "risk_mgmt" / "neutral_debator.py",
        root / "managers" / "portfolio_manager.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "get_evidence_discipline_instruction" in source, path
