"""Typed shared state and contracts for the new India trading-analysis graph.

The state deliberately separates raw evidence, deterministic analytics, agent
interpretations, risk review, and the final research verdict. This module does
not fetch market data, call an LLM, or place orders.
"""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class AnalysisRequest(TypedDict):
    symbol: str
    exchange: str
    as_of: str
    timeframes: list[str]
    analysis_mode: Literal["historical", "latest"]


class EvidenceRecord(TypedDict, total=False):
    source: str
    source_url: str
    published_at: str
    retrieved_at: str
    observed_at: str
    content: str
    data: dict[str, Any]


class AgentReport(TypedDict, total=False):
    status: Literal["ok", "limited", "unavailable", "error"]
    summary: str
    findings: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    evidence_refs: list[str]
    confidence: float
    missing_data: list[str]
    error: str


class TradeProposal(TypedDict, total=False):
    action: Literal["BUY", "SELL", "NO_TRADE", "NEEDS_REVIEW"]
    direction: str | None
    entry: float | None
    stop_loss: float | None
    target: float | None
    reward_risk_ratio: float | None
    entry_conditions: list[str]
    invalidation_conditions: list[str]
    rationale: list[str]


class RiskReport(TypedDict, total=False):
    status: Literal["approved_for_review", "rejected", "needs_review", "not_run"]
    checks: list[dict[str, Any]]
    hard_blocks: list[str]
    warnings: list[str]


class FinalVerdict(TypedDict, total=False):
    verdict: Literal["BUY", "SELL", "NO_TRADE", "NEEDS_REVIEW"]
    rationale: list[str]
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    risk_status: str
    confidence: float
    data_quality: Literal["good", "limited", "insufficient"]
    live_order_placed: Literal[False]


class TradingAnalysisState(TypedDict, total=False):
    # Immutable-by-convention request inputs
    request: AnalysisRequest
    run_id: str

    # Evidence package; all timestamps must preserve source semantics.
    market_data: dict[str, Any]
    fundamental_data: dict[str, Any]
    news_data: list[EvidenceRecord]
    events_data: list[EvidenceRecord]
    evidence_metadata: dict[str, Any]

    # Deterministic analytics and specialist outputs
    technical_data: dict[str, Any]
    fundamental_analysis: AgentReport
    technical_analysis: AgentReport
    trader_analysis: TradeProposal
    risk_analysis: RiskReport
    manager_analysis: AgentReport

    # Validated outcome and execution metadata
    final_verdict: FinalVerdict
    tool_calls: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    workflow_trace: list[str]
    status: Literal["initialized", "running", "completed", "partial", "failed"]
    collection_attempts: int

    # Optional caller-provided risk limits and portfolio snapshot.
    portfolio: NotRequired[dict[str, Any]]
    risk_limits: NotRequired[dict[str, Any]]
