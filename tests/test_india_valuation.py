from pathlib import Path
from tradingagents.india.valuation import build_valuation_summary


def row(symbol, metric, value, industry="Banks - Regional", sector="Financial Services", stamp="2026-10-10T10:00:00+00:00"):
    return {"symbol": symbol, "metric_code": metric, "value": value, "period_type": "snapshot",
            "period_end": "2026-10-10", "retrieved_at": stamp, "yahoo_industry": industry,
            "yahoo_sector": sector}


def test_industry_median_excludes_self_negative_and_missing(tmp_path):
    p = tmp_path / "universe.csv"
    p.write_text("symbol,sector\nAAA,Financial Services\nBBB,Financial Services\nCCC,Financial Services\nDDD,Financial Services\nEEE,Financial Services\nFFF,Financial Services\nGGG,Financial Services\n", encoding="utf-8")
    rows = [row("AAA", "PE_RATIO", 8), row("AAA", "FORWARD_PE", 7), row("AAA", "PB_RATIO", 1.2), row("AAA", "ROE", .14)]
    for sym, pe in [("BBB", 8), ("CCC", 9), ("DDD", 10), ("EEE", 11), ("FFF", 12), ("GGG", -3)]:
        rows.append(row(sym, "PE_RATIO", pe))
    d = build_valuation_summary("AAA", rows, p)
    assert d["stock"]["trailing_pe"] == 8
    assert d["stock"]["forward_pe"] == 7
    assert d["peer_group"] == "industry"
    assert d["peer_count"] == 5
    assert d["peer_median"] == 10
    assert d["percent_vs_peers"] == -20


def test_insufficient_peer_data_never_guesses(tmp_path):
    p = tmp_path / "universe.csv"
    p.write_text("symbol,sector\nAAA,Tech\nBBB,Tech\nCCC,Tech\n", encoding="utf-8")
    rows = [row("AAA", "PE_RATIO", 12, industry=None, sector="Tech"), row("BBB", "PE_RATIO", -2, industry=None, sector="Tech")]
    d = build_valuation_summary("AAA", rows, p)
    assert d["status"] == "not_enough_peer_data"
    assert d["peer_median"] is None
    assert d["nifty500_median"] is None


def test_stale_peer_snapshots_are_excluded_and_reported(tmp_path):
    from datetime import datetime, timezone
    p = tmp_path / "universe.csv"
    p.write_text("symbol,sector\nAAA,Tech\nBBB,Tech\nCCC,Tech\nDDD,Tech\nEEE,Tech\nFFF,Tech\nGGG,Tech\n", encoding="utf-8")
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    rows = [row("AAA", "PE_RATIO", 8, industry="Tech", sector="Tech", stamp="2026-10-11T10:00:00+00:00")]
    for sym, pe in [("BBB", 8), ("CCC", 9), ("DDD", 10), ("EEE", 11), ("FFF", 12)]:
        rows.append(row(sym, "PE_RATIO", pe, industry="Tech", sector="Tech", stamp="2026-10-10T10:00:00+00:00"))
    rows.append(row("GGG", "PE_RATIO", 14, industry="Tech", sector="Tech", stamp="2026-08-01T10:00:00+00:00"))
    d = build_valuation_summary("AAA", rows, p, now=now, max_snapshot_age_days=30)
    assert d["peer_count"] == 5
    assert d["peer_median"] == 10
    assert d["stale_or_missing_pe_count"] == 1
    assert d["stock"]["snapshot_fresh"] is True


def test_stale_stock_snapshot_does_not_show_old_valuation(tmp_path):
    from datetime import datetime, timezone
    p = tmp_path / "universe.csv"
    p.write_text("symbol,sector\nAAA,Tech\nBBB,Tech\nCCC,Tech\nDDD,Tech\nEEE,Tech\nFFF,Tech\n", encoding="utf-8")
    rows = [row("AAA", "PE_RATIO", 8, industry="Tech", sector="Tech", stamp="2026-08-01T10:00:00+00:00")]
    for sym, pe in [("BBB", 8), ("CCC", 9), ("DDD", 10), ("EEE", 11), ("FFF", 12)]:
        rows.append(row(sym, "PE_RATIO", pe, industry="Tech", sector="Tech", stamp="2026-10-10T10:00:00+00:00"))
    d = build_valuation_summary("AAA", rows, p, now=datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert d["stock"]["trailing_pe"] is None
    assert d["stock"]["snapshot_fresh"] is False
    assert d["percent_vs_peers"] is None
