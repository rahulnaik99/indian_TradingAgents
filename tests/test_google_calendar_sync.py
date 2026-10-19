"""Google Calendar sync with a fake Google (OAuth token endpoint, calendars + events REST API)."""
from __future__ import annotations

import json
import stat
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from fastapi.testclient import TestClient

from tradingagents.india import event_calendar as ecal
from tradingagents.india import google_calendar as gcal
from tradingagents.india.storage import IndiaStore

TODAY = date(2026, 10, 11)


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload if payload is not None else {}

    def json(self):
        return self._p


class FakeGoogle:
    """Stateful fake: remembers events per calendar so idempotency can be asserted."""

    def __init__(self):
        self.calendars, self.events, self.calls, self.fail_next = {}, {}, [], []
        self.token_status = 200

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        if self.token_status != 200:
            return Resp(400, {"error": "invalid_grant"})
        if data["grant_type"] == "authorization_code":
            return Resp(200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
        return Resp(200, {"access_token": "AT2", "expires_in": 3600})

    def request(self, method, url, json=None, timeout=None, headers=None):
        assert headers["Authorization"].startswith("Bearer ")
        path = url.split("/calendar/v3")[1]
        self.calls.append((method, path, json))
        if self.fail_next and "/events" in path:
            return Resp(self.fail_next.pop(0), {"error": {"message": "boom", "errors": [{"reason": "backendError"}]}})
        if method == "POST" and path == "/calendars":
            cid = f"cal{len(self.calendars) + 1}@group.calendar.google.com"
            self.calendars[cid] = json
            self.events[cid] = {}
            return Resp(200, {"id": cid})
        parts = path.strip("/").split("/")
        cid = parts[1] if len(parts) > 1 else None
        if cid not in self.calendars:
            return Resp(404, {"error": {"message": "Not Found"}})
        if len(parts) == 2:
            return Resp(204) if method == "DELETE" and not self.calendars.pop(cid) is None else Resp(200, {"id": cid})
        events = self.events[cid]
        if method == "POST":
            if json["id"] in events:
                return Resp(409, {"error": {"message": "exists"}})
            events[json["id"]] = json
            return Resp(200, json)
        eid = parts[3]
        if method == "PATCH":
            events[eid] = {**events.get(eid, {}), **json}
            return Resp(200, events[eid])
        if method == "DELETE":
            return Resp(204) if events.pop(eid, None) is not None else Resp(404, {"error": {"message": "gone"}})
        return Resp(400)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-supersecret")
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "tok.json"))
    store = IndiaStore(tmp_path / "t.sqlite3")
    fake = FakeGoogle()
    client = gcal.GoogleCalendarClient(store, session=fake, sleep=lambda s: None)
    return store, fake, client


def connect(client):
    url = client.auth_url("http://127.0.0.1:8000/api/gcal/oauth/callback")
    state = parse_qs(urlparse(url).query)["state"][0]
    client.finish_auth("authcode", state)
    return url


def ev(day, category="quarterly_result", **kw):
    kw.setdefault("verified", True)
    kw.setdefault("symbol", "SBIN")
    return ecal.make_event(day=date.fromisoformat(day), category=category, title=kw.pop("title", "SBIN: Financial Results"),
                           assets=["NSE"], provider="t", source_name="NSE", source_url="https://nseindia.com/x", **kw)


# ---------------------------------------------------------------- mapping & filters
def test_event_mapping_is_allday_exclusive_end_yellow_for_fed_and_valid_id():
    fed = ecal.static_events()
    fomc = next(e for e in fed if e["category"] == "fed_meeting" and e["date"] == "2026-10-27")
    body = gcal.build_gcal_event(fomc)
    assert body["start"] == {"date": "2026-10-27"} and body["end"] == {"date": "2026-10-29"}  # 27-28 inclusive -> end exclusive
    assert body["colorId"] == "5" and body["transparency"] == "transparent"
    assert set(body["id"]) <= set("0123456789abcdefghijklmnopqrstuv") and 5 <= len(body["id"]) <= 1024
    assert body["source"]["url"].startswith("https://www.federalreserve.gov")
    lead = gcal.build_gcal_event(ev("2026-10-14", "ipo", status="detected_from_news", verified=False, title="IPO: Fusion CX"))
    assert lead["summary"].startswith("[unverified] ")


def test_select_events_filters_verified_categories_symbols_and_keeps_fed():
    s = gcal.validate_settings({"categories": ["quarterly_result", "fed_meeting"], "symbols": ["sbin"]})
    fed = next(e for e in ecal.static_events() if e["category"] == "fed_meeting")
    events = [ev("2026-10-30"), ev("2026-10-30", symbol="TCS", title="TCS results"), ev("2026-10-30", "dividend", title="div"),
              ev("2026-10-30", symbol="SBIN", verified=False, title="lead"), ev("2026-10-31", verified=False, status="manual", title="mine"), fed]
    titles = [e["title"] for e in gcal.select_events(events, s)]
    assert "SBIN: Financial Results" in titles and "mine" in titles and fed["title"] in titles
    assert "TCS results" not in titles and "div" not in titles and "lead" not in titles
    assert not gcal.validate_settings({"verified_only": False})["verified_only"]
    with pytest.raises(gcal.GoogleCalendarError):
        gcal.validate_settings({"categories": ["bogus"]})
    assert gcal.validate_settings({"symbols": ["tcs", "bad symbol!", " infy "]})["symbols"] == ["INFY", "TCS"]


# ---------------------------------------------------------------- OAuth
def test_oauth_uses_pkce_least_privilege_scope_and_stores_token_privately(env):
    store, fake, client = env
    url = connect(client)
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.app.created"] and q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"] and q["client_id"][0].startswith("cid")
    exchange = next(c for c in fake.calls if c[0] == "POST" and c[2].get("grant_type") == "authorization_code")
    assert exchange[2]["code_verifier"] and exchange[2]["code"] == "authcode"
    assert client.connected() and stat.S_IMODE(client.token_path.stat().st_mode) == 0o600
    assert "GOCSPX" not in client.token_path.read_text()


def test_oauth_rejects_unknown_state_and_missing_refresh_token(env):
    store, fake, client = env
    with pytest.raises(gcal.GoogleCalendarError, match="expired"):
        client.finish_auth("c", "forged-state")
    state = parse_qs(urlparse(client.auth_url("http://127.0.0.1:8000/cb")).query)["state"][0]
    fake.post = lambda url, data=None, timeout=None: Resp(200, {"access_token": "x", "expires_in": 10})
    with pytest.raises(gcal.GoogleCalendarError, match="refresh token"):
        client.finish_auth("c", state)
    assert not client.connected()


def test_revoked_grant_clears_token_with_clear_message(env):
    store, fake, client = env
    connect(client)
    client._access = None
    fake.token_status = 400
    with pytest.raises(gcal.GoogleCalendarError, match="Connect again"):
        client.sync([ev("2026-10-30")], gcal.validate_settings({}), today=TODAY)
    assert not client.connected()


# ---------------------------------------------------------------- sync behaviour
def test_sync_creates_dedicated_calendar_and_is_idempotent(env):
    store, fake, client = env
    connect(client)
    settings = gcal.validate_settings({})
    events = [ev("2026-10-30"), ev("2026-11-02", "dividend", title="INFY dividend", symbol="INFY")]
    first = client.sync(events, settings, today=TODAY)
    assert (first["created"], first["updated"], first["deleted"], first["failed"]) == (2, 0, 0, 0)
    (cal_id, cal), = fake.calendars.items()
    assert cal["summary"] == gcal.CALENDAR_NAME and cal["timeZone"] == "Asia/Kolkata"
    writes_before = len([c for c in fake.calls if c[0] in ("POST", "PATCH", "DELETE") and "/events" in str(c[1])])
    second = client.sync(events, settings, today=TODAY)
    assert second["unchanged"] == 2 and second["created"] == 0
    writes_after = len([c for c in fake.calls if c[0] in ("POST", "PATCH", "DELETE") and "/events" in str(c[1])])
    assert writes_after == writes_before  # nothing re-sent


def test_sync_patches_changed_events_and_deletes_removed_or_hidden(env):
    store, fake, client = env
    connect(client)
    settings = gcal.validate_settings({})
    a, b = ev("2026-10-30"), ev("2026-11-02", "dividend", title="INFY dividend", symbol="INFY")
    client.sync([a, b], settings, today=TODAY)
    changed = {**a, "description": "now with more detail"}
    report = client.sync([changed], settings, today=TODAY)  # a changed, b gone (hidden/superseded)
    assert report["updated"] + report["created"] == 1 and report["deleted"] == 1
    (cal_id,) = fake.calendars
    assert set(fake.events[cal_id]) == {a["event_id"]} and "more detail" in fake.events[cal_id][a["event_id"]]["description"]


def test_sync_leaves_out_of_window_events_alone_and_recovers_from_deleted_calendar(env):
    store, fake, client = env
    connect(client)
    settings = gcal.validate_settings({"days_ahead": 30})
    old = ev("2026-02-01", title="old one")
    client.sync([old], settings, today=date(2026, 2, 1))
    client.sync([], settings, today=TODAY)  # 'old' is outside the window now -> untouched
    assert len(fake.events[next(iter(fake.calendars))]) == 1
    fake.calendars.clear()  # user deleted the calendar in Google
    report = client.sync([ev("2026-10-30")], settings, today=TODAY)
    assert report["created"] == 1 and len(fake.calendars) == 1


def test_sync_reports_failures_and_retries_transient_errors(env):
    store, fake, client = env
    connect(client)
    client.ensure_calendar()
    fake.fail_next = [503]  # one transient failure on first write: retried, then succeeds
    report = client.sync([ev("2026-10-30")], gcal.validate_settings({}), today=TODAY)
    assert report["created"] == 1 and report["failed"] == 0
    fake.fail_next = [403, 403, 403, 403, 403, 403]
    report = client.sync([ev("2026-10-30"), ev("2026-10-31", title="second")], gcal.validate_settings({}), today=TODAY)
    assert report["status"] == "partial" and report["failed"] == 1 and "GOCSPX" not in json.dumps(report)


def test_delete_calendar_removes_it_and_clears_bookkeeping(env):
    store, fake, client = env
    connect(client)
    client.sync([ev("2026-10-30")], gcal.validate_settings({}), today=TODAY)
    client.delete_calendar()
    assert fake.calendars == {} and store.gcal_sync_all() == {}


def test_auto_sync_after_refresh_is_silent_when_unconfigured_and_never_raises(env, monkeypatch):
    store, fake, client = env
    assert gcal.auto_sync_after_refresh(store, client) is None  # not connected yet
    connect(client)
    ecal.refresh_calendar(store, ["static"], today=TODAY)
    result = gcal.auto_sync_after_refresh(store, client)
    assert result["status"] == "ok" and result["created"] > 0
    store.set_calendar_meta("gcal_settings", json.dumps({"auto_sync": False}))
    assert gcal.auto_sync_after_refresh(store, client)["status"] == "skipped"
    store.set_calendar_meta("gcal_settings", "{}")
    monkeypatch.setattr(client, "sync_from_store", lambda s=None: (_ for _ in ()).throw(RuntimeError("kaput GOCSPX-supersecret")))
    out = gcal.auto_sync_after_refresh(store, client)
    assert out["status"] == "error" and "GOCSPX" not in out["message"]


# ---------------------------------------------------------------- HTTP API
@pytest.fixture()
def api(env, monkeypatch):
    store, fake, _ = env
    import tradingagents.web.app as web
    monkeypatch.setattr(web, "STORE", store)
    real = gcal.GoogleCalendarClient
    monkeypatch.setattr(gcal, "GoogleCalendarClient", lambda st, **kw: real(st, session=fake, sleep=lambda s: None, **kw))
    return TestClient(web.app, base_url="http://127.0.0.1:8000"), store, fake


def test_api_connect_callback_sync_settings_disconnect_flow(api):
    http, store, fake = api
    status = http.get("/api/gcal/status").json()
    assert status["configured"] and not status["connected"] and status["on_this_computer"]
    assert status["redirect_uri"] == "http://127.0.0.1:8000/api/gcal/oauth/callback"
    assert "GOCSPX" not in json.dumps(status)
    assert http.post("/api/gcal/sync").status_code == 409  # not connected yet
    url = http.post("/api/gcal/connect").json()["auth_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    assert http.get("/api/gcal/oauth/callback", params={"code": "x", "state": "bad"}).status_code == 400
    assert http.get("/api/gcal/oauth/callback", params={"error": "access_denied"}).status_code == 400
    ok = http.get("/api/gcal/oauth/callback", params={"code": "authcode", "state": state})
    assert ok.status_code == 200 and "Connected" in ok.text
    assert http.get("/api/gcal/status").json()["connected"] is True
    assert http.put("/api/gcal/settings", json={"categories": ["bogus"]}).status_code == 422
    assert http.put("/api/gcal/settings", json={"verified_only": False, "symbols": ["sbin"]}).json()["settings"]["symbols"] == ["SBIN"]
    http.post("/api/calendar/refresh", params={"sources": "static"})  # auto-sync fires here
    cal_events = next(iter(fake.events.values()))
    assert any(e["colorId"] == "5" for e in cal_events.values())
    report = http.post("/api/gcal/sync").json()["report"]
    assert report["created"] == 0 and report["unchanged"] > 0
    assert http.post("/api/gcal/disconnect", params={"delete_calendar": True}).json() == {"connected": False, "calendar_deleted": True}
    assert fake.calendars == {} and http.get("/api/gcal/status").json()["connected"] is False


def test_api_refresh_includes_google_result_and_dashboard_has_card(api):
    http, store, fake = api
    assert "google_calendar" not in http.post("/api/calendar/refresh", params={"sources": "static"}).json()["report"]
    html = http.get("/").text
    assert 'id="gcBox"' in html and "api/gcal/sync" in html
