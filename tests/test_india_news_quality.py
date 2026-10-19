from datetime import datetime, timezone

import pytest

from tradingagents.india.news_quality import build_recent_news_query, filter_news_items, score_news_item
from tradingagents.india.storage import IndiaStore

NOW = datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc)


def item(title, url, published="2026-10-10T10:00:00+00:00", **extra):
    row = {"event_id": extra.pop("event_id", title.lower().replace(" ", "-")), "title": title,
           "summary": "Canara Bank stock and financial update", "source_name": "Test source",
           "source_url": url, "published_at": published, "event_type": "quarterly_results",
           "direction": "uncertain", "retrieved_at": "2026-10-11T08:00:00+00:00",
           "severity": "low", "confidence": 0.5, "provider": "test"}
    row.update(extra)
    return row


@pytest.mark.unit
def test_financial_news_source_recent_english_title_scores_good():
    result = score_news_item(item("Canara Bank quarterly results profit rises", "https://www.moneycontrol.com/news/canara.html"), now=NOW)
    assert result["source_type"] == "news"
    assert result["relevance_score"] >= 60
    assert result["quality"] == "good"
    assert "results" in result["quality_reasons"]["keyword_hits"]


@pytest.mark.unit
def test_marketing_wikipedia_and_youtube_are_weak():
    for url in ("https://en.wikipedia.org/wiki/Canara_Bank", "https://music.youtube.com/watch?v=abc",
                "https://canarabank.com/netbanking"):
        result = score_news_item(item("Canara Bank net banking open account", url), now=NOW)
        assert result["quality"] == "weak"
        assert result["relevance_score"] < 60


@pytest.mark.unit
def test_filter_defaults_to_good_recent_dated_events():
    rows = [
        item("Canara Bank results profit", "https://www.moneycontrol.com/news/a", "2026-10-10T10:00:00Z"),
        item("Canara Bank contact us", "https://canarabank.com/contact-us", "2026-10-10T10:00:00Z"),
        item("Canara Bank results profit", "https://www.moneycontrol.com/news/old", "2026-06-01T10:00:00Z"),
        item("Canara Bank results profit", "https://www.moneycontrol.com/news/undated", None),
    ]
    result = filter_news_items(rows, quality="good", days=30, now=NOW)
    assert len(result) == 1
    assert result[0]["source_url"].endswith("/a")


@pytest.mark.unit
def test_filters_source_direction_and_text():
    rows = [item("Canara Bank dividend declared", "https://www.nseindia.com/announcement", direction="positive")]
    result = filter_news_items(rows, source_type="official", quality="all", days=30,
                               direction="positive", text="dividend", now=NOW)
    assert len(result) == 1
    assert result[0]["source_type"] == "official"


@pytest.mark.unit
def test_undated_can_be_included_explicitly_but_not_by_default():
    rows = [item("Canara Bank quarterly results", "https://www.moneycontrol.com/news/a", None)]
    assert filter_news_items(rows, quality="all", days=30, now=NOW) == []
    assert len(filter_news_items(rows, quality="all", days=30, include_undated=True, now=NOW)) == 1


@pytest.mark.unit
def test_recent_query_adds_financial_terms_and_window_once():
    query = build_recent_news_query('"Canara Bank"')
    assert "when:30d" in query
    assert "NPA" in query
    assert build_recent_news_query(query) == query


@pytest.mark.unit
def test_hide_is_persistent_and_does_not_delete(tmp_path):
    store = IndiaStore(tmp_path / "events.sqlite3")
    row = item("Canara Bank quarterly results", "https://www.moneycontrol.com/news/a", event_id="evt-1")
    store.save_event(row)
    assert len(store.list_events()) == 1
    assert store.hide_event("evt-1") is True
    assert store.list_events() == []
    hidden = store.list_events(include_hidden=True)
    assert len(hidden) == 1
    assert hidden[0]["hidden"] is True
    assert store.hide_event("evt-1", hidden=False) is True
    assert len(store.list_events()) == 1


@pytest.mark.unit
def test_cleanup_hides_low_quality_and_preserves_records(tmp_path):
    store = IndiaStore(tmp_path / "events.sqlite3")
    store.save_event(item("Canara Bank contact us", "https://canarabank.com/contact-us", event_id="junk"))
    store.save_event(item("Canara Bank quarterly results profit rises", "https://www.moneycontrol.com/news/a", event_id="good"))
    count = store.hide_events_below_score(60)
    assert count >= 1
    assert len(store.list_events()) == 1
    assert len(store.list_events(include_hidden=True)) == 2


@pytest.mark.unit
def test_google_news_redirect_uses_publisher_name_for_source_classification():
    row = item("Canara Bank quarterly results profit rises", "https://news.google.com/rss/articles/example",
               source_name="Moneycontrol")
    result = score_news_item(row, now=NOW)
    assert result["source_type"] == "news"
    assert result["quality"] == "good"
