from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "tradingagents" / "web" / "static" / "index.html"


def test_analyze_page_has_evidence_transparency_panels():
    source = HTML.read_text(encoding="utf-8")
    for panel in (
        "Market regime & breadth",
        "Corporate catalysts",
        "Portfolio risk",
        "Evidence quality & exclusions",
    ):
        assert panel in source
    assert 'id="evidencePanels"' in source


def test_evidence_renderer_handles_missing_assessment_sections():
    source = HTML.read_text(encoding="utf-8")
    assert "function renderEvidencePanels(d)" in source
    assert "a.market_regime||{}" in source
    assert "a.corporate_catalysts||{}" in source
    assert "a.portfolio_risk||{}" in source
    assert "renderEvidencePanels(d)" in source


def test_evidence_renderer_uses_escaped_dynamic_text():
    source = HTML.read_text(encoding="utf-8")
    assert "${esc(title)}" in source
    assert "${esc(String(value??'Unknown').replaceAll('_',' '))}" in source
    assert "${esc(note||'No additional detail supplied.')}" in source
