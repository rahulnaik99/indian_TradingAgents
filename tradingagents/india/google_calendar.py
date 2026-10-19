"""Push the Events calendar to a dedicated Google Calendar ("Market Events (TradingAgents)").

Design:
* OAuth 2.0 installed-app flow with PKCE against your own Google Cloud OAuth client
  (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env). No third-party Google SDK needed.
* Least-privilege scope ``calendar.app.created``: the app can only see and edit calendars it
  created itself, never your personal calendars or events.
* Events are all-day and use a stable id (our event_id), so syncing again updates in place,
  never duplicates. Changed events are patched, removed/hidden/superseded ones are deleted.
* The refresh token lives in a 0600 file under data/ and is never sent to the browser or logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from tradingagents.india import event_calendar as ecal

SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
CALENDAR_NAME = "Market Events (TradingAgents)"
ROOT = Path(__file__).resolve().parents[2]
MAX_WRITES_PER_SYNC = 500

# Google's fixed event colour palette (colorId): 5 = yellow ("banana").
COLOR_IDS = {"fed_meeting": "5", "rbi_policy": "5", "quarterly_result": "9", "dividend": "10", "buyback": "3",
             "bonus_split": "7", "block_deal": "6", "board_meeting": "2", "ipo": "1", "crypto_event": "4",
             "crypto_unlock": "4", "market_holiday": "8", "other": "8"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "verified_only": True, "auto_sync": True, "days_ahead": 120, "assets": ["NSE", "CRYPTO"], "symbols": [],
    "categories": ["fed_meeting", "rbi_policy", "quarterly_result", "dividend", "buyback", "bonus_split", "board_meeting", "ipo"],
}
_PENDING: dict[str, dict[str, Any]] = {}  # OAuth state -> {verifier, redirect_uri, at}
_PENDING_LOCK = threading.Lock()
SYNC_LOCK = threading.Lock()


class GoogleCalendarError(RuntimeError):
    pass


# ------------------------------------------------------------------ settings & mapping
def validate_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    out = dict(DEFAULT_SETTINGS)
    if "verified_only" in raw:
        out["verified_only"] = bool(raw["verified_only"])
    if "auto_sync" in raw:
        out["auto_sync"] = bool(raw["auto_sync"])
    if "days_ahead" in raw:
        try:
            out["days_ahead"] = min(365, max(7, int(raw["days_ahead"])))
        except (TypeError, ValueError):
            raise GoogleCalendarError("days_ahead must be a number between 7 and 365.") from None
    if "categories" in raw:
        cats = [c for c in raw["categories"] if c in ecal.CATEGORIES]
        if not cats:
            raise GoogleCalendarError("Select at least one event category to sync.")
        out["categories"] = cats
    if "assets" in raw:
        assets = [a for a in raw["assets"] if a in ecal.ASSET_SCOPES]
        if not assets:
            raise GoogleCalendarError("Select NSE and/or CRYPTO.")
        out["assets"] = assets
    if "symbols" in raw:
        symbols = []
        for s in raw["symbols"][:50]:
            s = str(s).strip().upper()
            if s and len(s) <= 30 and all(ch.isalnum() or ch in "-&." for ch in s):
                symbols.append(s)
        out["symbols"] = sorted(set(symbols))
    return out


def load_settings(store: Any) -> dict[str, Any]:
    try:
        return validate_settings(json.loads(store.get_calendar_meta("gcal_settings") or "{}"))
    except (ValueError, GoogleCalendarError):
        return dict(DEFAULT_SETTINGS)


def select_events(events: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the user's sync filters. Events you added yourself always count as trusted."""
    symbols = set(settings.get("symbols") or [])
    chosen = []
    for e in events:
        if e["category"] not in settings["categories"] or not set(e.get("assets", [])) & set(settings["assets"]):
            continue
        if settings["verified_only"] and not (e.get("verified") or e.get("status") == "manual"):
            continue
        if symbols and e.get("symbol") and e["symbol"] not in symbols:
            continue  # company events limited to your symbol list; Fed/RBI (no symbol) always pass
        chosen.append(e)
    return chosen


def build_gcal_event(e: dict[str, Any]) -> dict[str, Any]:
    end = e.get("end_date") or e["date"]
    prefix = {"detected_from_news": "[unverified] ", "tentative": "[tentative] ", "community_reported": "[unconfirmed] "}.get(e.get("status"), "")
    label = ecal.CATEGORIES.get(e["category"], ecal.CATEGORIES["other"])["label"]
    lines = [f"{label} · {', '.join(e.get('assets', []))}", "", e.get("description", "").strip(), "",
             f"Source: {e.get('source_name', '')} {e.get('source_url', '')}".strip(),
             "Status: " + ("verified from an official source" if e.get("verified") else (e.get("status") or "unverified").replace("_", " ")),
             "Added by TradingAgents India. Not investment advice."]
    body: dict[str, Any] = {
        "id": e["event_id"], "summary": (prefix + e["title"])[:300], "description": "\n".join(lines)[:8000],
        "start": {"date": e["date"]}, "end": {"date": (date.fromisoformat(end) + timedelta(days=1)).isoformat()},
        "colorId": COLOR_IDS.get(e["category"], "8"), "transparency": "transparent",
        "reminders": {"useDefault": True},
        "extendedProperties": {"private": {"tradingagents_event_id": e["event_id"], "category": e["category"]}},
    }
    if e.get("source_url", "").startswith(("http://", "https://")):
        body["source"] = {"title": (e.get("source_name") or "Source")[:100], "url": e["source_url"]}
    return body


def _hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ client
class GoogleCalendarClient:
    def __init__(self, store: Any, session: Any | None = None, timeout: float = 20.0, token_path: Path | None = None, sleep=time.sleep):
        self.store, self.session, self.timeout, self.sleep = store, session or requests.Session(), timeout, sleep
        self.token_path = Path(token_path or os.getenv("GOOGLE_TOKEN_PATH") or ROOT / "data" / "google_calendar_token.json")
        self.client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        self._access: tuple[str, float] | None = None

    # -- state
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _read_token(self) -> dict[str, Any]:
        try:
            return json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return {}

    def connected(self) -> bool:
        return bool(self._read_token().get("refresh_token"))

    def _write_token(self, data: dict[str, Any]) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.chmod(self.token_path, 0o600)

    def forget_token(self) -> None:
        self._access = None
        try:
            self.token_path.unlink()
        except OSError:
            pass

    # -- OAuth
    def auth_url(self, redirect_uri: str) -> str:
        if not self.configured():
            raise GoogleCalendarError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.")
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with _PENDING_LOCK:
            for key in [k for k, v in _PENDING.items() if time.time() - v["at"] > 600]:
                _PENDING.pop(key, None)
            _PENDING[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "at": time.time()}
        query = requests.Request("GET", AUTH_URL, params={
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256"}).prepare()
        return str(query.url)

    def finish_auth(self, code: str, state: str) -> None:
        with _PENDING_LOCK:
            pending = _PENDING.pop(state, None)
        if not pending or time.time() - pending["at"] > 600:
            raise GoogleCalendarError("The sign-in link expired or was not started here. Click Connect again.")
        data = self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
                                    "code_verifier": pending["verifier"]})
        if not data.get("refresh_token"):
            raise GoogleCalendarError("Google did not return a refresh token. Remove this app at "
                                      "myaccount.google.com/permissions, then connect again.")
        self._write_token({"refresh_token": data["refresh_token"]})
        self._access = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.session.post(TOKEN_URL, data={**form, "client_id": self.client_id, "client_secret": self.client_secret}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GoogleCalendarError(ecal.scrub(f"Could not reach Google ({type(exc).__name__}).")) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200:
            if payload.get("error") == "invalid_grant" and form.get("grant_type") == "refresh_token":
                self.forget_token()
                raise GoogleCalendarError("Google access was revoked or expired (Testing-mode OAuth apps expire after 7 days). Connect again.")
            raise GoogleCalendarError(f"Google sign-in failed: {payload.get('error_description') or payload.get('error') or response.status_code}")
        return payload

    def _access_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        refresh = self._read_token().get("refresh_token")
        if not refresh:
            raise GoogleCalendarError("Google Calendar is not connected. Click Connect first.")
        data = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        self._access = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)
        return self._access[0]

    # -- API
    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None, ok: tuple[int, ...] = (200, 204)) -> Any:
        for attempt in range(4):
            try:
                response = self.session.request(method, f"{API}{path}", json=json_body, timeout=self.timeout,
                                                headers={"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"})
            except requests.RequestException as exc:
                raise GoogleCalendarError(ecal.scrub(f"Google Calendar request failed ({type(exc).__name__}).")) from exc
            if response.status_code in ok:
                return response
            try:
                err = response.json().get("error", {})
            except ValueError:
                err = {}
            reason = (err.get("errors") or [{}])[0].get("reason", "") if isinstance(err, dict) else ""
            retryable = response.status_code in (429, 500, 502, 503, 504) or reason in ("rateLimitExceeded", "userRateLimitExceeded")
            if retryable and attempt < 3:
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 401:
                self._access = None
            message = err.get("message") if isinstance(err, dict) else None
            raise GoogleCalendarError(f"Google Calendar API error {response.status_code}: {message or 'request failed'}", )
        raise GoogleCalendarError("Google Calendar API retries exhausted.")

    def ensure_calendar(self) -> str:
        cal_id = self.store.get_calendar_meta("gcal_calendar_id")
        if cal_id:
            try:
                self._request("GET", f"/calendars/{cal_id}")
                return cal_id
            except GoogleCalendarError as exc:
                if " 404" not in str(exc) and " 410" not in str(exc):
                    raise
                self.store.gcal_sync_clear()  # calendar was deleted in Google: start fresh
        created = self._request("POST", "/calendars", json_body={
            "summary": CALENDAR_NAME, "timeZone": "Asia/Kolkata",
            "description": "Fed, RBI, NSE corporate events and crypto events from TradingAgents India. Safe to delete."}).json()
        self.store.set_calendar_meta("gcal_calendar_id", created["id"])
        return created["id"]

    def sync(self, events: list[dict[str, Any]], settings: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
        """Make the Google calendar match the selected events (idempotent, resumable)."""
        today = today or ecal._today()
        window_start, window_end = (today - timedelta(days=7)).isoformat(), (today + timedelta(days=settings["days_ahead"])).isoformat()
        cal = self.ensure_calendar()
        desired = {e["event_id"]: e for e in select_events(events, settings)}
        synced = self.store.gcal_sync_all()
        report = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "failed": 0, "pending": 0, "errors": []}
        writes = 0
        for event_id, event in desired.items():
            body = build_gcal_event(event)
            digest = _hash(body)
            if synced.get(event_id, {}).get("hash") == digest:
                report["unchanged"] += 1
                continue
            if writes >= MAX_WRITES_PER_SYNC:
                report["pending"] += 1
                continue
            writes += 1
            try:
                response = self._request("POST", f"/calendars/{cal}/events", json_body=body, ok=(200, 201, 409))
                if response.status_code == 409:  # id exists (or was deleted earlier): patch and re-confirm
                    patch = {k: v for k, v in body.items() if k != "id"} | {"status": "confirmed"}
                    self._request("PATCH", f"/calendars/{cal}/events/{event_id}", json_body=patch)
                    report["updated"] += 1
                else:
                    report["created"] += 1
                self.store.gcal_sync_set(event_id, event["date"], digest)
            except GoogleCalendarError as exc:
                report["failed"] += 1
                if len(report["errors"]) < 3:
                    report["errors"].append(str(exc)[:200])
        for event_id, row in synced.items():
            if event_id in desired or not (window_start <= row["date"] <= window_end):
                continue
            try:
                self._request("DELETE", f"/calendars/{cal}/events/{event_id}", ok=(200, 204, 404, 410))
                self.store.gcal_sync_delete(event_id)
                report["deleted"] += 1
            except GoogleCalendarError as exc:
                report["failed"] += 1
                if len(report["errors"]) < 3:
                    report["errors"].append(str(exc)[:200])
        report["status"] = "ok" if not report["failed"] else "partial"
        report["at"] = datetime.now(timezone.utc).isoformat()
        self.store.set_calendar_meta("gcal_last_sync", json.dumps(report))
        return report

    def sync_from_store(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = settings or load_settings(self.store)
        today = ecal._today()
        events = ecal.query_events(self.store, today - timedelta(days=7), today + timedelta(days=settings["days_ahead"]))
        return self.sync(events, settings, today=today)

    def delete_calendar(self) -> None:
        cal = self.store.get_calendar_meta("gcal_calendar_id")
        if cal:
            self._request("DELETE", f"/calendars/{cal}", ok=(200, 204, 404, 410))
        self.store.gcal_sync_clear()
        self.store.set_calendar_meta("gcal_calendar_id", "")


def auto_sync_after_refresh(store: Any, client: GoogleCalendarClient | None = None) -> dict[str, Any] | None:
    """Called after a calendar refresh. Returns None when Google sync isn't set up; never raises."""
    try:
        client = client or GoogleCalendarClient(store)
        if not (client.configured() and client.connected()):
            return None
        settings = load_settings(store)
        if not settings["auto_sync"]:
            return {"status": "skipped", "message": "Auto-sync is off."}
        return client.sync_from_store(settings)
    except Exception as exc:  # noqa: BLE001 - Google problems must not break the refresh
        return {"status": "error", "message": ecal.scrub(f"{type(exc).__name__}: {exc}")[:300]}
