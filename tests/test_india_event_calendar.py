"""Event calendar: static seeds, NSE/CoinMarketCal/IndianAPI providers (fake HTTP), news detection, API."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
import requests
from fastapi.testclient import TestClient

from tradingagents.india import event_calendar as ecal
from tradingagents.india import indianapi_news
from tradingagents.india.news_quality import filter_news_items
from tradingagents.india.storage import IndiaStore

TODAY = date(2026, 10, 11)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload, self.status_code = payload, status

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self.payload is None:
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Routes by URL suffix; records every call (url, params, headers)."""

    def __init__(self, routes=None, error=None):
        self.routes, self.error, self.calls = routes or {}, error, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        if self.error:
            raise self.error
        for suffix, response in self.routes.items():
            if url.endswith(suffix):
                return response
        return FakeResponse(None, 404)


@pytest.fixture()
def store(tmp_path):
    return IndiaStore(tmp_path / "t.sqlite3")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("NSE_COOKIES", "INDIANAPI_API_KEY", "COINMARKETCAL_API_KEY", "INDIANAPI_CACHE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    indianapi_news.clear_cache()


# ----------------------------------------------------------------- parsing / classification
def test_parse_date_any_handles_nse_and_iso_formats():
    assert ecal.parse_date_any("12-Oct-2026") == date(2026, 10, 12)
    assert ecal.parse_date_any("12-10-2026") == date(2026, 10, 12)
    assert ecal.parse_date_any("2026-10-14T10:00:00") == date(2026, 10, 14)
    assert ecal.parse_date_any("11-Oct-2026 15:30:00") == date(2026, 10, 11)
    assert ecal.parse_date_any("garbage") is None and ecal.parse_date_any(None) is None


@pytest.mark.parametrize("text,primary,tag", [
    ("Financial Results/Dividend", "quarterly_result", "dividend"),
    ("To consider buyback of equity shares", "buyback", "buyback"),
    ("Bonus issue 1:1", "bonus_split", "bonus_split"),
    ("Fund raising by way of QIP", "board_meeting", "board_meeting"),
])
def test_classify_purpose(text, primary, tag):
    category, tags = ecal.classify_purpose(text)
    assert category == primary and tag in tags


# ----------------------------------------------------------------- static seeds
def test_static_events_contain_verified_fed_and_rbi_dates():
    events = ecal.static_events()
    fomc = [e for e in events if e["category"] == "fed_meeting" and e["date"] == "2026-10-27"]
    assert len(fomc) == 1 and fomc[0]["end_date"] == "2026-10-28" and fomc[0]["verified"] is True
    assert set(fomc[0]["assets"]) == {"NSE", "CRYPTO"}
    rbi = [e for e in events if e["category"] == "rbi_policy" and e["date"] == "2026-12-02"]
    assert rbi and rbi[0]["end_date"] == "2026-12-04" and "rbi.org.in" in rbi[0]["source_url"]
    tentative = [e for e in events if e["date"].startswith("2027") and e["category"] == "fed_meeting"]
    assert tentative and all(not e["verified"] and e["status"] == "tentative" for e in tentative)
    assert any(e["category"] == "market_holiday" and e["date"] == "2026-10-20" for e in events)


# ----------------------------------------------------------------- NSE provider
NSE_EVENT_CAL = [{"symbol": "SBIN", "company": "State Bank of India", "purpose": "Financial Results", "bm_desc": "Q2 results", "date": "30-Oct-2026"}]
NSE_BOARD = [{"bm_symbol": "TCS", "sm_name": "Tata Consultancy", "bm_purpose": "Financial Results/Dividend", "bm_desc": "", "bm_date": "12-Oct-2026"}]
NSE_ACTIONS = [{"symbol": "INFY", "comp": "Infosys", "subject": "Dividend - Rs 21 Per Share", "exDate": "20-Oct-2026", "recDate": "21-Oct-2026"},
               {"symbol": "WIPRO", "comp": "Wipro", "subject": "Buyback of equity shares", "exDate": "-", "recDate": "05-Nov-2026"}]
NSE_BLOCK = {"timestamp": "11-Oct-2026 15:30:00", "data": [{"symbol": "RELIANCE", "qty": 1000}, {"symbol": "RELIANCE", "qty": 500}, {"symbol": "ITC", "qty": 10}]}


def nse_session():
    return FakeSession({"/api/event-calendar": FakeResponse(NSE_EVENT_CAL), "/api/corporate-board-meetings": FakeResponse(NSE_BOARD),
                        "/api/corporates-corporateActions": FakeResponse({"data": NSE_ACTIONS}), "/api/block-deal": FakeResponse(NSE_BLOCK),
                        "https://www.nseindia.com": FakeResponse({})})


def test_nse_client_normalises_all_four_feeds_and_warms_session():
    session = nse_session()
    events = ecal.NseCalendarClient(session=session).fetch_all(TODAY, date(2027, 1, 9))
    assert session.calls[0]["url"] == "https://www.nseindia.com"  # cookie warm-up first
    by_title = {e["title"]: e for e in events}
    assert by_title["SBIN: Financial Results"]["category"] == "quarterly_result" and by_title["SBIN: Financial Results"]["verified"]
    tcs = next(e for e in events if e["symbol"] == "TCS")
    assert tcs["category"] == "quarterly_result" and "dividend" in tcs["tags"]
    infy = next(e for e in events if e["symbol"] == "INFY")
    assert infy["category"] == "dividend" and infy["date"] == "2026-10-20"
    wipro = next(e for e in events if e["symbol"] == "WIPRO")
    assert wipro["category"] == "buyback" and wipro["date"] == "2026-11-05"  # falls back to record date
    block = next(e for e in events if e["symbol"] == "RELIANCE" and e["category"] == "block_deal")
    assert "2 trades" in block["title"] and block["date"] == "2026-10-11" and block["status"] == "occurred"
    api_call = next(c for c in session.calls if c["url"].endswith("/api/event-calendar"))
    assert api_call["params"]["from_date"] == "11-10-2026" and api_call["params"]["to_date"] == "09-01-2027"


def test_nse_cookie_env_is_sent_and_skips_warmup(monkeypatch):
    monkeypatch.setenv("NSE_COOKIES", "nsit=abc123; nseappid=xyz789")
    session = nse_session()
    ecal.NseCalendarClient(session=session).event_calendar(TODAY, date(2026, 12, 1))
    assert all(c["url"] != "https://www.nseindia.com" for c in session.calls)
    assert session.calls[0]["headers"]["Cookie"] == "nsit=abc123; nseappid=xyz789"


def test_nse_block_gives_actionable_error_without_leaking_cookie(monkeypatch):
    monkeypatch.setenv("NSE_COOKIES", "nsit=SECRETCOOKIEVALUE")
    session = FakeSession(error=requests.ConnectionError("failed with Cookie nsit=SECRETCOOKIEVALUE"))
    with pytest.raises(ecal.CalendarSourceError) as exc:
        ecal.NseCalendarClient(session=session).fetch_all(TODAY, date(2026, 12, 1))
    assert "SECRETCOOKIEVALUE" not in str(exc.value)
    blocked = FakeSession({"/api/event-calendar": FakeResponse({}, 403), "/api/corporate-board-meetings": FakeResponse({}, 403),
                           "/api/corporates-corporateActions": FakeResponse({}, 403), "/api/block-deal": FakeResponse({}, 403)})
    with pytest.raises(ecal.CalendarSourceError, match="NSE_COOKIES"):
        ecal.NseCalendarClient(session=blocked).fetch_all(TODAY, date(2026, 12, 1))


def test_nse_partial_failure_still_returns_working_feeds():
    session = FakeSession({"/api/event-calendar": FakeResponse(NSE_EVENT_CAL), "/api/corporate-board-meetings": FakeResponse({}, 403),
                           "/api/corporates-corporateActions": FakeResponse(None), "/api/block-deal": FakeResponse({}, 500)})
    events = ecal.NseCalendarClient(session=session).fetch_all(TODAY, date(2026, 12, 1))
    assert [e["symbol"] for e in events] == ["SBIN"]


# ----------------------------------------------------------------- crypto providers
def test_coinmarketcal_parses_events_and_requires_key(monkeypatch):
    with pytest.raises(ecal.CalendarSourceError, match="COINMARKETCAL_API_KEY"):
        ecal.CoinMarketCalClient(session=FakeSession()).fetch(TODAY, date(2026, 12, 1))
    monkeypatch.setenv("COINMARKETCAL_API_KEY", "cmc-key-123456")
    body = {"body": [{"title": {"en": "Mainnet upgrade"}, "date_event": "2026-11-03T00:00:00+00:00", "coins": [{"symbol": "BTC"}],
                      "categories": [{"name": "Release"}], "source": "https://example.org/x", "percentage": 88, "is_hot": True},
                     {"title": {"en": "ARB token unlock"}, "date_event": "2026-11-16T00:00:00+00:00", "coins": [{"symbol": "ARB"}],
                      "categories": [{"name": "Token Unlock"}]}]}
    session = FakeSession({"/events": FakeResponse(body)})
    events = ecal.CoinMarketCalClient(session=session).fetch(TODAY, date(2026, 12, 1))
    assert session.calls[0]["headers"]["x-api-key"] == "cmc-key-123456"
    assert session.calls[0]["params"]["dateRangeStart"] == "10/11/2026"
    assert {e["category"] for e in events} == {"crypto_event", "crypto_unlock"}
    hot = next(e for e in events if e["category"] == "crypto_event")
    assert hot["importance"] == "high" and hot["assets"] == ["CRYPTO"] and hot["verified"] is False


def test_crypto_news_client_uses_documented_article_shape():
    articles = {"articles": [{"title": "SEC to decide on ETF on November 20", "description": "Decision due.", "link": "https://c.example/a",
                              "pubDate": "2026-10-10T12:00:00Z", "source": "CoinDesk"}]}
    items = ecal.CryptoNewsClient(session=FakeSession({"/api/news": FakeResponse(articles)})).fetch()
    assert items[0]["source_url"] == "https://c.example/a"
    events = ecal.detect_events_from_news(items, scope="CRYPTO", today=TODAY)
    assert events and events[0]["category"] == "crypto_event" and events[0]["date"] == "2026-11-20" and events[0]["assets"] == ["CRYPTO"]


# ----------------------------------------------------------------- news -> calendar detection
USER_FEED_SAMPLE = [
    {"title": "Ashish Kacholia, Mukul Agrawal, others invest Rs 120 crore in IPO-bound Fusion CX",
     "summary": "Sanshi Fund, along with Kotak Mahindra Life Insurance, acquired a combined 3.19% stake worth Rs 120 crore in Fusion CX ahead of its Rs 702 crore IPO opening on October 14.",
     "source_url": "https://economictimes.indiatimes.com/x", "source_name": "Economic Times", "published_at": "2026-10-11T09:03:08+00:00"},
    {"title": "Q2 earnings, inflation data, oil prices may drive stock markets this week",
     "summary": "Major quarterly earnings from companies like Wipro and HCL Tech will drive markets this week. Investors will track oil prices."},
    {"title": "Nifty lags global peers", "summary": "The Nifty 50 ranked second-last and the market 10 times more volatile."},
]


def test_detector_extracts_dated_ipo_from_user_feed_sample_and_ignores_vague_text():
    events = ecal.detect_events_from_news(USER_FEED_SAMPLE, scope="NSE", today=TODAY)
    assert [(e["category"], e["date"]) for e in events] == [("ipo", "2026-10-14")]
    assert events[0]["status"] == "detected_from_news" and events[0]["verified"] is False
    assert "verify" in events[0]["description"].lower()


def test_detector_year_rollover_and_lowercase_may_and_scope():
    item = [{"title": "HDFC Bank board meeting on January 9 to consider dividend", "published_at": "2026-12-20T00:00:00+00:00"}]
    events = ecal.detect_events_from_news(item, scope="NSE", today=date(2026, 12, 21))
    assert events and events[0]["date"] == "2027-01-09"
    assert ecal.detect_events_from_news([{"title": "Results may 5 times beat estimates, board meeting soon"}], today=TODAY) == []
    crypto_only = [{"title": "Bitcoin hard fork scheduled for November 2"}]
    assert ecal.detect_events_from_news(crypto_only, scope="NSE", today=TODAY) == []
    assert ecal.detect_events_from_news(crypto_only, scope="CRYPTO", today=TODAY)[0]["date"] == "2026-11-02"


def test_detector_ignores_dates_far_in_the_past():
    assert ecal.detect_events_from_news([{"title": "Dividend record date was March 3, 2024"}], today=TODAY) == []


# ----------------------------------------------------------------- IndianAPI news client
RAW_FEED = [
    {"title": "FPIs pull out Rs 44,166 crore from Indian equities in October so far", "summary": "", "url": "https://timesofindia.indiatimes.com/a.cms",
     "image_url": "https://img/x.jpg", "pub_date": "2026-10-11T15:21:54", "source": "Times of India", "topics": ["Business"]},
    {"title": "Broken item without url", "summary": "x", "url": "", "pub_date": "2026-10-11T15:00:00"},
]


def test_indianapi_requires_key_and_sends_header_only(monkeypatch):
    with pytest.raises(ecal.CalendarSourceError, match="INDIANAPI_API_KEY"):
        indianapi_news.IndianApiNewsClient(session=FakeSession()).fetch()
    monkeypatch.setenv("INDIANAPI_API_KEY", "sk-test-abcdef")
    session = FakeSession({"/news": FakeResponse(RAW_FEED)})
    items = indianapi_news.IndianApiNewsClient(session=session).fetch()
    assert session.calls[0]["url"] == "https://stock.indianapi.in/news"
    assert session.calls[0]["headers"]["x-api-key"] == "sk-test-abcdef"
    assert "sk-test-abcdef" not in session.calls[0]["url"] and session.calls[0]["params"] is None
    assert len(items) == 1  # item without a URL is dropped


def test_indianapi_naive_timestamps_are_ist_and_survive_quality_filter(monkeypatch):
    monkeypatch.setenv("INDIANAPI_API_KEY", "sk-test-abcdef")
    items = indianapi_news.IndianApiNewsClient(session=FakeSession({"/news": FakeResponse(RAW_FEED)})).fetch()
    assert items[0]["published_at"] == "2026-10-11T09:51:54+00:00"  # 15:21:54 IST
    # 'now' is 10:11 UTC: read as UTC the item would be future-dated and dropped.
    now = datetime(2026, 10, 11, 10, 11, tzinfo=timezone.utc)
    kept = filter_news_items(items, quality="all", days=7, now=now)
    assert len(kept) == 1


def test_indianapi_errors_are_clear_and_cache_protects_quota(monkeypatch):
    monkeypatch.setenv("INDIANAPI_API_KEY", "sk-test-abcdef")
    for status, text in ((401, "rejected"), (429, "rate limit")):
        indianapi_news.clear_cache()
        with pytest.raises(ecal.CalendarSourceError, match=text) as exc:
            indianapi_news.IndianApiNewsClient(session=FakeSession({"/news": FakeResponse({}, status)})).fetch()
        assert "sk-test-abcdef" not in str(exc.value)
    indianapi_news.clear_cache()
    session = FakeSession({"/news": FakeResponse(RAW_FEED)})
    client = indianapi_news.IndianApiNewsClient(session=session)
    client.fetch(); client.fetch()
    assert len(session.calls) == 1
    client.fetch(force=True)
    assert len(session.calls) == 2


# ----------------------------------------------------------------- storage / dedupe / refresh
def test_store_upserts_upgrades_unverified_and_queries_overlapping_ranges(store):
    news = ecal.make_event(day=date(2026, 10, 27), end_day=date(2026, 10, 28), category="fed_meeting", title="Fed: news lead", assets=["NSE"],
                           provider="news", source_name="News", status="detected_from_news")
    official = ecal.make_event(day=date(2026, 10, 27), end_day=date(2026, 10, 28), category="fed_meeting", title="Fed: news lead", assets=["NSE"],
                               provider="static", source_name="Federal Reserve", verified=True)
    assert store.upsert_calendar_event(news) is True
    assert store.upsert_calendar_event(official) is False  # same id: upgraded, not duplicated
    rows = store.list_calendar_events("2026-10-28", "2026-10-31")  # starts inside the 2-day range
    assert len(rows) == 1 and rows[0]["verified"] is True and rows[0]["source_name"] == "Federal Reserve"
    assert store.list_calendar_events("2026-10-29", "2026-10-31") == []
    assert store.hide_calendar_event(news["event_id"]) and store.list_calendar_events("2026-10-01", "2026-10-31") == []
    assert store.hide_calendar_event("missing") is False


def test_dedupe_prefers_verified_source_for_same_symbol_category_date():
    lead = ecal.make_event(day=TODAY, category="quarterly_result", symbol="sbin", title="SBIN results (news)", assets=["NSE"], provider="news", source_name="News")
    nse = ecal.make_event(day=TODAY, category="quarterly_result", symbol="SBIN", title="SBIN: Financial Results", assets=["NSE"], provider="nse", source_name="NSE", verified=True)
    merged = ecal.dedupe_events([lead, nse])
    assert len(merged) == 1 and merged[0]["verified"] is True


def test_refresh_isolates_failures_and_reports_each_source(store):
    class Boom:
        def fetch_all(self, *a): raise ecal.CalendarSourceError("NSE blocked the request (HTTP 403).")
    class NoKey:
        def configured(self): return False
    report = ecal.refresh_calendar(store, ["static", "nse", "coinmarketcal", "indianapi_news"], today=TODAY, nse=Boom(), coinmarketcal=NoKey(), indianapi=NoKey())
    assert report["static"]["status"] == "ok" and report["static"]["new"] > 20
    assert report["nse"]["status"] == "error" and "blocked" in report["nse"]["message"]
    assert report["coinmarketcal"]["status"] == "skipped" and report["indianapi_news"]["status"] == "skipped"
    again = ecal.refresh_calendar(store, ["static"], today=TODAY)
    assert again["static"]["new"] == 0  # idempotent
    assert store.get_calendar_meta("last_refresh")


def test_refresh_ingests_indianapi_news_into_calendar(store, monkeypatch):
    monkeypatch.setenv("INDIANAPI_API_KEY", "sk-test-abcdef")
    raw = [{"title": "Fusion CX IPO opening on October 14", "summary": "", "url": "https://economictimes.indiatimes.com/f", "pub_date": "2026-10-11T14:33:08", "source": "Economic Times"}]
    client = indianapi_news.IndianApiNewsClient(session=FakeSession({"/news": FakeResponse(raw)}))
    report = ecal.refresh_calendar(store, ["indianapi_news"], today=TODAY, indianapi=client)
    assert report["indianapi_news"] == {"status": "ok", "fetched": 1, "new": 1}
    events = ecal.query_events(store, date(2026, 10, 1), date(2026, 10, 31), asset="nse")
    assert [(e["category"], e["date"], e["status"]) for e in events] == [("ipo", "2026-10-14", "detected_from_news")]


# ----------------------------------------------------------------- HTTP API + UI
@pytest.fixture()
def client(store, monkeypatch):
    import tradingagents.web.app as web
    monkeypatch.setattr(web, "STORE", store)
    return TestClient(web.app)


def test_dashboard_has_events_page_and_nav(client):
    html = client.get("/").text
    assert 'data-page="calendar"' in html and 'id="page-calendar"' in html and 'id="calGrid"' in html and 'id="feedNews"' in html


def test_calendar_api_month_filters_and_marks_fed_for_both_assets(client):
    assert client.post("/api/calendar/refresh", params={"sources": "static"}).json()["report"]["static"]["status"] == "ok"
    data = client.get("/api/calendar/events", params={"month": "2026-10"}).json()
    fed = [e for e in data["events"] if e["category"] == "fed_meeting"]
    assert len(fed) == 1 and fed[0]["date"] == "2026-10-27" and data["categories"]["fed_meeting"]["color"].startswith("#")
    crypto = client.get("/api/calendar/events", params={"month": "2026-10", "asset": "crypto"}).json()["events"]
    assert [e["category"] for e in crypto] == ["fed_meeting"]  # Fed matters to Bitcoin; RBI/holidays do not
    nse_rbi = client.get("/api/calendar/events", params={"month": "2026-10", "asset": "nse", "category": "rbi_policy"}).json()["events"]
    assert [e["date"] for e in nse_rbi] == ["2026-10-05"]
    assert client.get("/api/calendar/events", params={"month": "2026-13"}).status_code == 422
    assert client.get("/api/calendar/events", params={"category": "nope"}).status_code == 422
    assert client.get("/api/calendar/upcoming", params={"days": 30}).status_code == 200


def test_calendar_manual_event_hide_and_validation(client):
    body = {"date": "2026-11-05", "end_date": "2026-11-06", "category": "board_meeting", "title": "SBIN board meeting", "symbol": "sbin", "assets": ["NSE"]}
    created = client.post("/api/calendar/events", json=body).json()
    assert created["created"] is True and created["event"]["symbol"] == "SBIN"
    listed = client.get("/api/calendar/events", params={"month": "2026-11", "symbol": "SBIN"}).json()["events"]
    assert len(listed) == 1
    assert client.post(f"/api/calendar/events/{created['event']['event_id']}/hide").json()["deleted"] is False
    assert client.get("/api/calendar/events", params={"month": "2026-11"}).json()["events"] == []
    assert client.post("/api/calendar/events/nope/hide").status_code == 404
    assert client.post("/api/calendar/events", json={**body, "category": "bogus"}).status_code == 422
    assert client.post("/api/calendar/events", json={**body, "end_date": "2026-11-01"}).status_code == 422
    assert client.post("/api/calendar/events", json={**body, "source_url": "javascript:alert(1)"}).status_code == 422
    assert client.post("/api/calendar/refresh", params={"sources": "bogus"}).status_code == 422


def test_news_feed_endpoint_needs_key_then_saves_scores_and_feeds_calendar(client, monkeypatch):
    assert client.get("/api/news/feed").status_code == 503
    assert "INDIANAPI_API_KEY" in client.get("/api/news/feed").json()["detail"]
    monkeypatch.setenv("INDIANAPI_API_KEY", "sk-test-abcdef")
    raw = [{"title": "Fusion CX IPO opening on October 14 as investors pour in Rs 120 crore", "summary": "", "url": "https://economictimes.indiatimes.com/f",
            "pub_date": "2026-10-11T14:33:08", "source": "Economic Times", "topics": ["Stocks"]}]
    monkeypatch.setattr(indianapi_news.IndianApiNewsClient, "fetch", lambda self, force=False: indianapi_news.IndianApiNewsClient.normalize(raw))
    data = client.get("/api/news/feed", params={"quality": "all"}).json()
    assert data["fetched"] == 1 and data["calendar_events_added"] == 1
    assert data["events"][0]["relevance_score"] >= 0 and "x-api-key" not in str(data).lower()
    cal = client.get("/api/calendar/events", params={"month": "2026-10"}).json()["events"]
    assert any(e["category"] == "ipo" and e["date"] == "2026-10-14" for e in cal)


def test_config_never_exposes_key(client, monkeypatch):
    monkeypatch.setenv("INDIANAPI_API_KEY", "sk-test-abcdef")
    cfg = client.get("/api/config").json()
    assert cfg["indianapi_news_configured"] is True and "sk-test-abcdef" not in str(cfg)
    assert "sk-test-abcdef" not in str(client.get("/api/calendar/sources").json())
