"""Evidence-grounded Q&A over a persisted analysis, with optional fresh research.

Default answers are assembled from the saved analysis only. A research callback
may be injected for questions requiring missing/current information; its output
is marked as additional research and must carry publication timestamps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from tradingagents.india.langgraph_pipeline.persistence import AnalysisStore

Researcher = Callable[[str, dict[str, Any], str], dict[str, Any]]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed
    except ValueError:
        return None


def _walk_text(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"summary", "reason", "reasoning", "rationale", "content", "title", "text"} and isinstance(item, str):
                if item.strip():
                    found.append(item.strip())
            else:
                found.extend(_walk_text(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_text(item))
    return found


def answer_analysis_question(
    store: AnalysisStore,
    analysis_id: str,
    question: str,
    *,
    researcher: Researcher | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Answer from a saved snapshot; optionally research gaps without time leakage."""
    if not question or not question.strip():
        raise ValueError("question must not be empty")
    analysis = store.get_analysis(analysis_id)
    if analysis is None:
        raise KeyError(f"Unknown analysis_id: {analysis_id}")

    request = analysis.get("request") or {}
    cutoff = str(request.get("as_of") or "")
    cutoff_dt = _parse_time(cutoff)
    evidence = []
    for section in ("fundamental_data", "news_data", "events_data", "technical_data",
                    "fundamental_analysis", "technical_analysis", "trader_analysis",
                    "risk_analysis", "manager_analysis", "final_verdict"):
        value = analysis.get(section)
        if value:
            evidence.append({"section": section, "content": value})

    answer: dict[str, Any] = {
        "analysis_id": analysis_id,
        "question": question.strip(),
        "as_of": cutoff,
        "answer_type": "saved_analysis",
        "answer": None,
        "evidence_sections": [item["section"] for item in evidence],
        "evidence_quotes": [],
        "limitations": [],
        "additional_research": None,
    }
    if evidence:
        texts = []
        q_words = {word.lower().strip(".,?!:;()[]{}") for word in question.split() if len(word) > 3}
        for item in evidence:
            for snippet in _walk_text(item["content"]):
                score = sum(word in snippet.lower() for word in q_words)
                if score:
                    texts.append((score, item["section"], snippet))
        texts.sort(key=lambda row: row[0], reverse=True)
        selected = texts[:5]
        answer["evidence_quotes"] = [{"section": section, "text": text} for _, section, text in selected]
        verdict = (analysis.get("final_verdict") or {}).get("verdict")
        if verdict:
            answer["answer"] = f"Saved analysis verdict: {verdict}."
        if selected:
            answer["answer"] = (answer["answer"] + " " if answer["answer"] else "") + "Relevant saved findings: " + " ".join(text for _, _, text in selected)
        if not answer["answer"]:
            answer["answer"] = "The saved analysis does not contain enough detail to answer this question reliably."
            answer["limitations"].append("No relevant narrative evidence was found in the saved analysis.")
    else:
        answer["answer"] = "The saved analysis contains no evidence sections that can answer this question."
        answer["limitations"].append("No persisted evidence was available.")

    if researcher is not None:
        try:
            extra = researcher(question.strip(), analysis, cutoff)
            if not isinstance(extra, dict):
                raise TypeError("researcher must return a dictionary")
            published = extra.get("published_at")
            published_dt = _parse_time(published)
            # Fail closed: extra research is only safe when both the original
            # cutoff and the evidence publication timestamp are timezone-aware.
            if cutoff_dt is None or published_dt is None or published_dt > cutoff_dt:
                answer["limitations"].append(
                    "Additional research was excluded because its publication time "
                    "could not be verified as-of the analysis cutoff."
                )
            else:
                answer["additional_research"] = extra
                answer["answer_type"] = "saved_analysis_plus_research"
        except Exception as exc:
            answer["limitations"].append(f"Additional research failed: {type(exc).__name__}: {exc}")

    if persist:
        store.save_question(analysis_id, question.strip(), answer)
    return answer
