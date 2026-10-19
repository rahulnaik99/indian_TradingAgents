"""FastAPI backend for the local TradingAgents India swing-research dashboard.

The app is local-first. It exposes deterministic technical analysis, sourced web
research, point-in-time fundamental storage and a paper-only order ledger. It
never places live broker orders.
"""
from __future__ import annotations

import os
import json
import re
import csv
import io
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)

from tradingagents.india.agent_runner import AgentRunDisabled, run_existing_graph  # noqa: E402
from tradingagents.india.events import WebResearchError, WebResearchService  # noqa: E402
from tradingagents.india.news_quality import CONFIG as NEWS_QUALITY_CONFIG, build_recent_news_query, filter_news_items, score_news_item  # noqa: E402
from tradingagents.india.fundamentals import FundamentalDataError, import_facts_csv, parse_xbrl_facts, source_guidance, summarize_facts, refresh_yahoo_snapshot  # noqa: E402
from tradingagents.india.moneycontrol import MoneycontrolFetchError, MoneycontrolPermissionError, fetch_statement_tables  # noqa: E402
from tradingagents.india.paper_trading import PaperBroker, PaperTradingError  # noqa: E402
from tradingagents.india.providers.fyers import (  # noqa: E402
    FyersAPIError, FyersAuthenticationError, FyersConfigurationError, FyersDataError,
    FyersMarketDataClient, FyersRateLimitError, aggregate_hourly_to_four_hour,
)
from tradingagents.india.providers.fyers_orders import FyersOrderClient, FyersOrderError, LiveTradingDisabled  # noqa: E402
from tradingagents.india.contracts import OHLCVCandle, SwingTimeframe  # noqa: E402
from tradingagents.india.storage import IndiaStore  # noqa: E402
from tradingagents.india.technical import analyze_multi_timeframe  # noqa: E402
from tradingagents.india.tracing import traced, tracing_status  # noqa: E402
from tradingagents.india.execution_audit import create_run, mark_component, add_event, finish_run, get_run, list_runs, COMPONENTS  # noqa: E402
from tradingagents.india.workflow import run_integrated_research  # noqa: E402
from tradingagents.india.evidence_package import build_evidence_package, serialize_agent_context  # noqa: E402
from tradingagents.india.universe import DEFAULT_NIFTY50_REGISTRY, StockUniverseRegistry  # noqa: E402
from tradingagents.india.bulk_fundamentals import build_coverage, progress_summary, run_bulk  # noqa: E402
from tradingagents.india.fundamentals_view import last_refreshed, statement_view, build_statement_view  # noqa: E402
from tradingagents.india.financial_flags import evaluate_financial_flags  # noqa: E402
from tradingagents.india.valuation import build_valuation_summary  # noqa: E402
from tradingagents.india import event_calendar as ecal  # noqa: E402
from tradingagents.india.indianapi_news import IndianApiNewsClient  # noqa: E402
from tradingagents.india import google_calendar as gcal  # noqa: E402
from tradingagents.india import research_memory  # noqa: E402

INDIA_TZ = ZoneInfo("Asia/Kolkata")
STORE = IndiaStore(os.getenv("TRADINGAGENTS_DB_PATH", str(ROOT / "data" / "tradingagents_india.sqlite3")))
WEB_RESEARCH = WebResearchService()
PAPER_BROKER = PaperBroker(STORE)
_SCAN_LOCK = threading.Lock()
_FYERS_CLIENT: FyersMarketDataClient | None = None
_FYERS_CLIENT_KEY: tuple[str, str, str] | None = None
_NSE_CATALOG_CACHE: list[dict[str, Any]] = []
_NSE_CATALOG_FETCHED_AT = 0.0
_NSE_CATALOG_LOCK = threading.Lock()
_NSE_CATALOG_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"

app = FastAPI(
    title="TradingAgents India",
    description="Local-first NIFTY 50 swing research with sourced events, fundamentals, tracing and paper trading. Live orders are disabled.",
    version="0.2.0",
)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class XbrlParseRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    source_url: str = Field(min_length=8, max_length=2000)
    published_at: str = Field(min_length=10, max_length=50)
    xml_text: str = Field(min_length=1, max_length=8_000_000)
    scope: str = Field(default="consolidated", pattern="^(standalone|consolidated|unspecified)$")


class IntegratedResearchRequest(BaseModel):
    lookback_days: int = Field(default=365, ge=210, le=2000)
    include_15m: bool = False
    refresh_news: bool = True
    use_llm: bool = False
    # Retained for API compatibility with earlier clients. Full Analysis now always
    # includes the original TradingAgents analyst/research/risk/portfolio graph.
    run_agent_debate: bool = True
    as_of: datetime | None = None


class ResearchMemoryCompareRequest(BaseModel):
    analysis: dict[str, Any]


class ResearchMemoryPairRequest(BaseModel):
    previous_run_id: str = Field(min_length=1, max_length=200)
    current_run_id: str = Field(min_length=1, max_length=200)


class ResearchMemoryQuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    previous_run_id: str = Field(min_length=1, max_length=200)
    current_run_id: str = Field(min_length=1, max_length=200)


class ResearchQARequest(BaseModel):
    analysis: dict[str, Any]
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    # Optional explicit point-in-time cutoff for historical questions.
    as_of: datetime | None = None


class ScanRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=50)
    lookback_days: int = Field(default=365, ge=210, le=2000)
    include_15m: bool = False


class LiveOrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    side: str = Field(pattern="^(buy|sell)$")
    quantity: int = Field(gt=0, le=1_000_000)
    limit_price: float = Field(gt=0)
    confirm_live_order: str = Field(min_length=1, max_length=40)


class LiveModifyRequest(BaseModel):
    quantity: int = Field(gt=0, le=1_000_000)
    limit_price: float = Field(gt=0)
    confirm_live_order: str = Field(min_length=1, max_length=40)


class LiveCancelRequest(BaseModel):
    confirm_live_order: str = Field(min_length=1, max_length=40)


class PaperOrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    side: str = Field(pattern="^(buy|sell)$")
    quantity: int = Field(gt=0, le=1_000_000)
    reference_price: float = Field(gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=500)


class CalendarEventRequest(BaseModel):
    date: date
    end_date: date | None = None
    category: str = Field(default="other", max_length=40)
    title: str = Field(min_length=3, max_length=240)
    symbol: str | None = Field(default=None, max_length=30)
    assets: list[str] = Field(default_factory=lambda: ["NSE"], max_length=2)
    description: str = Field(default="", max_length=1200)
    source_url: str = Field(default="", max_length=2000)


class GcalSettingsRequest(BaseModel):
    verified_only: bool | None = None
    auto_sync: bool | None = None
    days_ahead: int | None = Field(default=None, ge=7, le=365)
    assets: list[str] | None = None
    categories: list[str] | None = None
    symbols: list[str] | None = Field(default=None, max_length=50)


_CALENDAR_REFRESH_LOCK = threading.Lock()


def _calendar_ingest(items: list[dict[str, Any]], *, scope: str = "NSE", provider: str = "news", symbol: str | None = None) -> int:
    """Best-effort: turn dated events found in news into calendar rows. Never breaks the news call."""
    try:
        return ecal.ingest_news_items(STORE, items, scope=scope, provider=provider, symbol=symbol)
    except Exception:  # noqa: BLE001
        return 0


def _nse_equity_catalog(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return the cached FYERS NSE cash-market EQ symbol master.

    The symbol master is public and contains tradable vendor symbols. A dated
    NIFTY 50 + CANARABANK fallback keeps the picker useful when the provider is
    temporarily unavailable; the response explicitly labels that fallback.
    """
    global _NSE_CATALOG_CACHE, _NSE_CATALOG_FETCHED_AT
    now = time.monotonic()
    with _NSE_CATALOG_LOCK:
        if _NSE_CATALOG_CACHE and not force_refresh and now - _NSE_CATALOG_FETCHED_AT < 6 * 3600:
            return list(_NSE_CATALOG_CACHE)
        try:
            response = requests.get(_NSE_CATALOG_URL, timeout=12, headers={"User-Agent": "TradingAgents-India/1.0"})
            response.raise_for_status()
            parsed: dict[str, dict[str, Any]] = {}
            reader = csv.reader(io.StringIO(response.text))
            for row in reader:
                if len(row) < 14:
                    continue
                vendor_symbol = (row[9] or "").strip().upper()
                if not vendor_symbol.startswith("NSE:") or not vendor_symbol.endswith("-EQ"):
                    continue
                symbol = vendor_symbol[4:-3].strip()
                if not re.fullmatch(r"[A-Z0-9&_-]{1,30}", symbol):
                    continue
                company = (row[1] or "").strip() or symbol
                parsed[symbol] = {"symbol": symbol, "company_name": company, "exchange": "NSE", "sector": None, "industry": None}
            if len(parsed) < 100:
                raise ValueError(f"NSE symbol master returned only {len(parsed)} EQ instruments")
            _NSE_CATALOG_CACHE = sorted(parsed.values(), key=lambda item: item["symbol"])
            _NSE_CATALOG_FETCHED_AT = now
            return list(_NSE_CATALOG_CACHE)
        except Exception:
            if _NSE_CATALOG_CACHE and not force_refresh:
                return list(_NSE_CATALOG_CACHE)
            fallback = [{"symbol": item.symbol, "company_name": item.company_name or item.symbol,
                         "exchange": item.exchange, "sector": item.sector, "industry": item.industry}
                        for item in DEFAULT_NIFTY50_REGISTRY.members("NIFTY 50")]
            if not any(item["symbol"] == "CANARABANK" for item in fallback):
                fallback.append({"symbol": "CANARABANK", "company_name": "Canara Bank", "exchange": "NSE", "sector": "Financial Services", "industry": "Banking"})
            return sorted(fallback, key=lambda item: item["symbol"])


def _known_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9&_-]{1,30}", normalized):
        raise HTTPException(status_code=422, detail="Invalid NSE equity symbol")
    allowed = {item["symbol"] for item in _nse_equity_catalog()}
    # A valid exchange symbol is allowed through to FYERS so the provider can
    # return its precise availability/error response when the master is stale.
    if normalized not in allowed and normalized not in set(DEFAULT_NIFTY50_REGISTRY.symbols("NIFTY 50")):
        if not normalized:
            raise HTTPException(status_code=422, detail="Symbol is required")
    return normalized


def _fyers_client() -> FyersMarketDataClient:
    global _FYERS_CLIENT, _FYERS_CLIENT_KEY
    app_id, token = os.getenv("FYERS_APP_ID", ""), os.getenv("FYERS_ACCESS_TOKEN", "")
    base_url = os.getenv("FYERS_DATA_BASE_URL", "https://api-t1.fyers.in/data/history")
    if not (app_id and token):
        raise HTTPException(status_code=503, detail="FYERS credentials are not configured. Add FYERS_APP_ID and FYERS_ACCESS_TOKEN to .env.")
    key = (app_id, token, base_url)
    if _FYERS_CLIENT is None or _FYERS_CLIENT_KEY != key:
        try:
            _FYERS_CLIENT = FyersMarketDataClient.from_env()
            _FYERS_CLIENT_KEY = key
        except FyersConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _FYERS_CLIENT


def _serialize_candle(c: Any) -> dict[str, Any]:
    return {"open_time": c.open_time.isoformat(), "close_time": c.close_time.isoformat(),
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
            "volume": c.volume, "is_complete": c.is_complete, "quality_flags": list(c.quality_flags)}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _live_trading_server_gate_open() -> bool:
    return (os.getenv("ENABLE_LIVE_TRADING", "false").lower() in {"1", "true", "yes"}
            and os.getenv("LIVE_TRADING_CONFIRMATION", "") == FyersOrderClient.SERVER_CONFIRMATION)

def _live_trading_ready() -> bool:
    return _live_trading_server_gate_open() and bool(os.getenv("FYERS_APP_ID") and os.getenv("FYERS_ACCESS_TOKEN"))


def _fyers_order_client(confirmation: str) -> FyersOrderClient:
    try:
        return FyersOrderClient.from_env(confirmation_phrase=confirmation)
    except LiveTradingDisabled as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FyersOrderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "tradingagents-india", "frontend": "served-by-fastapi",
            "live_trading_enabled": _live_trading_ready(), "paper_trading_enabled": True,
            "fyers_credentials_configured": bool(os.getenv("FYERS_APP_ID") and os.getenv("FYERS_ACCESS_TOKEN")),
            "langsmith": tracing_status()}


@app.get("/api/config")
def config_status() -> dict[str, Any]:
    return {"market_data_provider": "fyers", "fyers_configured": bool(os.getenv("FYERS_APP_ID") and os.getenv("FYERS_ACCESS_TOKEN")),
            "core_timeframes": ["1d", "4h", "1h"], "optional_timeframe": "15m", "primary_strategy": "swing_trading",
            "live_order_placement": _live_trading_ready(), "paper_trading": True,
            "news_provider": "Wigolo REST search + multi-step research when available, Google News RSS fallback",
            "wigolo": WEB_RESEARCH.health(),
            "indianapi_news_configured": bool(os.getenv("INDIANAPI_API_KEY")),
            "moneycontrol_automated_collection": source_guidance()["moneycontrol_automated_collection_enabled"],
            "langsmith": tracing_status(),
            "note": ("Technical analysis, web research, sourced financial-fact import and paper orders are available. Live limit-order controls are enabled by explicit server configuration; protective stop/target orders are not automated." if _live_trading_ready() else "Technical analysis, web research, sourced financial-fact import and paper orders are available. Live order placement is disabled by default.")}


@app.get("/api/universe")
def universe(index: str = Query(default="NIFTY 50", description="NIFTY 50 or ALL NSE")) -> dict[str, Any]:
    try:
        if index.strip().casefold() in {"all nse", "nse all", "nse eligible", "all"}:
            members = _nse_equity_catalog()
            live_catalog = len(members) > 100
            return {"index": "ALL NSE EQ" if live_catalog else "NSE fallback snapshot",
                    "snapshot_date": datetime.now(INDIA_TZ).date().isoformat() if live_catalog else "fallback",
                    "source_name": "FYERS NSE Capital Market symbol master" if live_catalog else "NIFTY 50 snapshot + CANARABANK fallback",
                    "source_url": _NSE_CATALOG_URL if live_catalog else "https://www.nseindia.com/static/market-data/securities-available-for-trading",
                    "count": len(members), "members": members, "is_fallback": not live_catalog}
        snapshots = DEFAULT_NIFTY50_REGISTRY.snapshots(index)
        if not snapshots:
            raise HTTPException(status_code=404, detail=f"No configured snapshot for {index!r}")
        snapshot = snapshots[-1]
        members = [{"symbol": item.symbol, "company_name": item.company_name or item.symbol,
                    "exchange": item.exchange, "sector": item.sector, "industry": item.industry}
                   for item in snapshot.instruments]
        return {"index": snapshot.index_name, "snapshot_date": snapshot.snapshot_date.isoformat(),
                "source_name": snapshot.source_name, "source_url": snapshot.source_url,
                "count": len(members), "members": members}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Unable to load configured stock universe") from exc


@app.get("/api/fyers/history")
@traced("india.api.fyers_history")
def fyers_history(symbol: str = Query(min_length=1, max_length=30), start_date: date = Query(),
                  end_date: date = Query(), timeframe: str = Query(default="1d", pattern="^(1d|4h|1h|15m)$")) -> dict[str, Any]:
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")

    requested_symbol = symbol.strip().upper()
    try:
        universe_symbols = {
            str(item.get("symbol", "")).strip().upper()
            for item in _nse_equity_catalog()
            if isinstance(item, dict) and item.get("symbol")
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Unable to validate the configured NSE equity universe",
        ) from exc
    if requested_symbol not in universe_symbols:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported NSE equity symbol: {requested_symbol}",
        )

    normalized = _known_symbol(symbol)
    client = _fyers_client()
    try:
        candles = client.get_history(normalized, start_date, end_date, timeframe)
    except FyersAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except FyersRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except FyersConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (FyersAPIError, FyersDataError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"symbol": normalized, "timeframe": timeframe, "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(), "count": len(candles), "source": "fyers",
            "candles": [_serialize_candle(c) for c in candles]}


@app.get("/api/analysis/{symbol}")
@traced("india.api.swing_analysis")
def swing_analysis(symbol: str, lookback_days: int = Query(default=365, ge=210, le=2000), include_15m: bool = False) -> dict[str, Any]:
    """Fetch provider candles and compute deterministic Daily/4H/1H swing analysis."""
    normalized = _known_symbol(symbol)
    run_id = create_run("technical_analysis", normalized, {"lookback_days": lookback_days, "include_15m": include_15m})
    try:
        client = _fyers_client()
        end_date = datetime.now(INDIA_TZ).date()
        daily_start = end_date - timedelta(days=lookback_days)
        intraday_start = end_date - timedelta(days=min(95, lookback_days))
        mark_component(run_id, "technical_analysis", "running", event_type="agent_started", message="Starting technical analysis")
        mark_component(run_id, "fyers_daily_history", "running", event_type="tool_started", message="Fetching daily candles")
        daily = client.get_history(normalized, daily_start, end_date, "1d")
        mark_component(run_id, "fyers_daily_history", "completed", details={"timeframe":"1d", "candles":len(daily)}, event_type="tool_completed")
        mark_component(run_id, "fyers_hourly_history", "running", event_type="tool_started", message="Fetching hourly candles")
        hourly = client.get_history(normalized, intraday_start, end_date, "1h")
        mark_component(run_id, "fyers_hourly_history", "completed", details={"timeframe":"1h", "candles":len(hourly)}, event_type="tool_completed")
        four_hour = aggregate_hourly_to_four_hour(hourly)
        series: dict[str, Any] = {"1d": daily, "4h": four_hour, "1h": hourly}
        if include_15m:
            mark_component(run_id, "fyers_15m_history", "running", event_type="tool_started", message="Fetching optional 15-minute candles")
            series["15m"] = client.get_history(normalized, intraday_start, end_date, "15m")
            mark_component(run_id, "fyers_15m_history", "completed", details={"timeframe":"15m", "candles":len(series["15m"])}, event_type="tool_completed")
        else:
            mark_component(run_id, "fyers_15m_history", "skipped", reason="Optional 15-minute refinement was not requested.", event_type="tool_skipped")
        mark_component(run_id, "indicator_calculation", "running", event_type="tool_started", message="Calculating indicators")
        result = analyze_multi_timeframe(series, symbol=normalized)
        mark_component(run_id, "indicator_calculation", "completed", details={"timeframes":list(series), "result_keys":sorted(result)}, event_type="tool_completed")
        mark_component(run_id, "technical_analysis", "completed", details={"bias":result.get("multi_timeframe_bias"), "timeframes":list(series)}, event_type="agent_completed")
        trace = tracing_status()
        mark_component(run_id, "langsmith_tracing", "completed" if trace.get("active") else "unavailable",
                       reason=None if trace.get("active") else "LangSmith tracing is not fully configured.", details=trace, event_type="tracing_status")
        result["as_of"] = datetime.now(INDIA_TZ).isoformat()
        result["source"] = "fyers"
        result["data_window"] = {"daily_start": daily_start.isoformat(), "intraday_start": intraday_start.isoformat(), "end_date": end_date.isoformat()}
        result["run_id"] = run_id
        result["execution_flow_url"] = f"/#execution?run_id={run_id}"
        # Persist a compact, source-aware summary for cross-run research memory.
        # Memory failures must never turn a completed analysis into a failed run.
        try:
            research_memory.save_snapshot(normalized, result)
            result["research_memory"] = {"persisted": True, "run_id": run_id}
        except Exception as memory_exc:  # noqa: BLE001
            result["research_memory"] = {"persisted": False, "error_type": type(memory_exc).__name__}
        finish_run(run_id, "completed", {"technical_bias": result.get("multi_timeframe_bias")})
        return result
    except HTTPException as exc:
        finish_run(run_id, "failed", {"error_type":"HTTPException", "status_code":exc.status_code, "detail":str(exc.detail)[:300]})
        raise
    except (FyersAuthenticationError, FyersRateLimitError, FyersConfigurationError, FyersAPIError, FyersDataError, ValueError) as exc:
        finish_run(run_id, "failed", {"error_type":type(exc).__name__, "message":str(exc)[:300]})
        if isinstance(exc, FyersAuthenticationError): raise HTTPException(status_code=401, detail=str(exc)) from exc
        if isinstance(exc, FyersRateLimitError): raise HTTPException(status_code=429, detail=str(exc)) from exc
        if isinstance(exc, FyersConfigurationError): raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        finish_run(run_id, "failed", {"error_type":type(exc).__name__, "message":str(exc)[:300]})
        raise


ASSET_TICKERS: dict[str, dict[str, str]] = {
    "crypto": {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana", "XRP-USD": "XRP", "BNB-USD": "BNB", "ADA-USD": "Cardano", "DOGE-USD": "Dogecoin", "LTC-USD": "Litecoin", "LINK-USD": "Chainlink"},
    "commodities": {"GC=F": "Gold futures (COMEX)", "SI=F": "Silver futures (COMEX)", "CL=F": "Crude oil futures (WTI)", "NG=F": "Natural gas futures", "HG=F": "Copper futures"},
    "forex": {"EURUSD=X": "EUR/USD", "GBPUSD=X": "GBP/USD", "USDJPY=X": "USD/JPY", "AUDUSD=X": "AUD/USD", "USDINR=X": "USD/INR", "EURINR=X": "EUR/INR", "GBPINR=X": "GBP/INR"},
}


@app.get("/api/asset-analysis/{asset_type}/{symbol:path}")
@traced("india.api.non_equity_asset_analysis")
def asset_analysis(asset_type: str, symbol: str, lookback_days: int = Query(default=365, ge=90, le=2000)) -> dict[str, Any]:
    """Technical analysis for allowlisted crypto, commodity futures and FX via Yahoo Finance."""
    asset_type = asset_type.strip().lower()
    ticker = symbol.strip().upper()
    if asset_type not in ASSET_TICKERS or ticker not in ASSET_TICKERS[asset_type]:
        raise HTTPException(status_code=422, detail="Unsupported asset. Choose an instrument from the market picker.")
    run_id = create_run("asset_technical_analysis", ticker, {"asset_type": asset_type, "lookback_days": lookback_days, "provider": "yfinance"})
    try:
        from datetime import timezone as dt_timezone
        from tradingagents.dataflows.vendors.yahoo.ohlcv import fetch_yahoo_history
        now = datetime.now(dt_timezone.utc)
        mark_component(run_id, "yahoo_finance_history", "running", event_type="tool_started", message=f"Fetching {asset_type} history from Yahoo Finance", details={"ticker": ticker})
        history = fetch_yahoo_history(
            ticker,
            period="5y",
            interval="1d",
        )
        hourly = fetch_yahoo_history(
            ticker,
            period="2y",
            interval="1h",
        )
        if history is None or history.empty or hourly is None or hourly.empty:
            raise HTTPException(status_code=502, detail=f"No usable Yahoo Finance candles returned for {ticker}")
        def make_candles(frame: Any, timeframe: SwingTimeframe, interval: timedelta) -> tuple[OHLCVCandle, ...]:
            result: list[OHLCVCandle] = []
            cutoff = now - timedelta(days=lookback_days if timeframe == SwingTimeframe.DAILY else min(95, lookback_days))
            for idx, row in frame.iterrows():
                stamp = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=dt_timezone.utc)
                stamp = stamp.astimezone(dt_timezone.utc)
                close_time = stamp + interval
                if stamp < cutoff or close_time > now:
                    continue
                vals = [float(row.get(col, 0) or 0) for col in ("Open", "High", "Low", "Close", "Volume")]
                o, h, lo, c, v = vals
                if not all(map(lambda x: x == x and abs(x) != float("inf"), vals)) or min(o, h, lo, c) <= 0:
                    continue
                # Reject broken provider rows rather than silently altering price data.
                if h < max(o, c, lo) or lo > min(o, c, h):
                    continue
                result.append(OHLCVCandle(symbol=ticker, timeframe=timeframe, open_time=stamp, close_time=close_time,
                    open=o, high=h, low=lo, close=c, volume=max(v, 0.0), source_provider="yahoo_finance",
                    retrieved_at=now, is_complete=True, is_adjusted=False))
            return tuple(sorted(result, key=lambda candle: candle.open_time))
        daily = make_candles(history, SwingTimeframe.DAILY, timedelta(days=1))
        hourly_candles = make_candles(hourly, SwingTimeframe.HOURLY, timedelta(hours=1))
        if len(daily) < 30 or len(hourly_candles) < 30:
            raise HTTPException(status_code=502, detail=f"Insufficient complete candles for {ticker}; daily={len(daily)}, hourly={len(hourly_candles)}")
        # Non-equity markets trade outside NSE hours, so use UTC-aligned 4H
        # buckets rather than the NSE-session-only FYERS aggregation helper.
        four_hour_groups: dict[tuple[Any, int], list[OHLCVCandle]] = {}
        for candle in hourly_candles:
            four_hour_groups.setdefault((candle.open_time.date(), candle.open_time.hour // 4), []).append(candle)
        four_hour: list[OHLCVCandle] = []
        for (_, _), group in sorted(four_hour_groups.items(), key=lambda item: item[1][0].open_time):
            group.sort(key=lambda candle: candle.open_time)
            four_hour.append(OHLCVCandle(symbol=ticker, timeframe=SwingTimeframe.FOUR_HOUR,
                open_time=group[0].open_time, close_time=group[-1].close_time,
                open=group[0].open, high=max(c.high for c in group), low=min(c.low for c in group),
                close=group[-1].close, volume=sum(c.volume for c in group), source_provider="yahoo_finance",
                retrieved_at=now, is_complete=all(c.is_complete for c in group), is_adjusted=False,
                quality_flags=() if len(group) == 4 else ("partial_four_hour_bucket",)))
        series = {"1d": daily, "4h": tuple(four_hour), "1h": hourly_candles}
        result = analyze_multi_timeframe(series, symbol=ticker)
        result.update({"symbol": ticker, "asset_type": asset_type, "asset_name": ASSET_TICKERS[asset_type][ticker],
                       "source": "yahoo_finance", "as_of": now.isoformat(), "run_id": run_id,
                       "data_note": "Public Yahoo Finance market data; symbol coverage, delay and history depend on Yahoo availability. Gold is COMEX futures, not MCX spot."})
        mark_component(run_id, "yahoo_finance_history", "completed", details={"daily_candles": len(daily), "hourly_candles": len(hourly_candles)}, event_type="tool_completed")
        mark_component(run_id, "technical_analysis", "completed", details={"bias": result.get("multi_timeframe_bias")}, event_type="agent_completed")
        finish_run(run_id, "completed", {"bias": result.get("multi_timeframe_bias"), "source": "yahoo_finance"})
        return result
    except HTTPException as exc:
        mark_component(run_id, "yahoo_finance_history", "failed", reason=str(exc.detail), event_type="tool_failed")
        finish_run(run_id, "failed", {"error": str(exc.detail)[:300]})
        raise
    except Exception as exc:
        mark_component(run_id, "yahoo_finance_history", "failed", reason=f"{type(exc).__name__}: {str(exc)[:200]}", event_type="tool_failed")
        finish_run(run_id, "failed", {"error_type": type(exc).__name__, "message": str(exc)[:300]})
        raise HTTPException(status_code=502, detail=f"{asset_type.title()} technical analysis failed for {ticker}: {type(exc).__name__}. Check provider availability.") from exc


@app.post("/api/research/{symbol}")
@traced("india.api.integrated_research")
def integrated_research(symbol: str, request: IntegratedResearchRequest) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    cutoff = request.as_of or datetime.now(INDIA_TZ)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise HTTPException(status_code=422, detail="as_of must include a timezone offset")
    if request.use_llm and os.getenv("ENABLE_INDIA_LLM_SYNTHESIS", "false").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=403, detail="LLM synthesis is disabled by default. Set ENABLE_INDIA_LLM_SYNTHESIS=true after configuring a supported provider.")

    # The main integrated research path must include the original TradingAgents
    # graph; otherwise the India-specific evidence workflow can silently bypass
    # the original analyst, Bull/Bear, Trader, risk and Portfolio Manager stages.
    # Keep the request field for backward compatibility, but do not let older UI
    # clients disable the original agent workflow.
    request.run_agent_debate = True

    run_id = create_run("integrated_research", normalized, {
        "lookback_days": request.lookback_days, "include_15m": request.include_15m,
        "refresh_news": request.refresh_news, "use_llm": request.use_llm,
        "run_agent_debate": request.run_agent_debate, "as_of": cutoff.isoformat()
    })
    try:
        client = _fyers_client()
        end_date = cutoff.astimezone(INDIA_TZ).date()
        daily_start = end_date - timedelta(days=request.lookback_days)
        intraday_start = end_date - timedelta(days=min(95, request.lookback_days))

        mark_component(run_id, "fyers_daily_history", "running", event_type="tool_started", message="Fetching FYERS daily history", details={"symbol": normalized, "start": daily_start.isoformat(), "end": end_date.isoformat()})
        daily = client.get_history(normalized, daily_start, end_date, "1d")
        daily = tuple(c for c in daily if c.close_time <= cutoff and c.is_complete)
        mark_component(run_id, "fyers_daily_history", "completed", details={"timeframe": "1d", "complete_candles": len(daily)}, event_type="tool_completed", message=f"FYERS daily history returned {len(daily)} complete candles")

        mark_component(run_id, "fyers_hourly_history", "running", event_type="tool_started", message="Fetching FYERS hourly history", details={"symbol": normalized, "start": intraday_start.isoformat(), "end": end_date.isoformat()})
        hourly = client.get_history(normalized, intraday_start, end_date, "1h")
        hourly = tuple(c for c in hourly if c.close_time <= cutoff and c.is_complete)
        mark_component(run_id, "fyers_hourly_history", "completed", details={"timeframe": "1h", "complete_candles": len(hourly)}, event_type="tool_completed", message=f"FYERS hourly history returned {len(hourly)} complete candles")
        series: dict[str, Any] = {"1d": daily, "4h": aggregate_hourly_to_four_hour(hourly), "1h": hourly}
        if request.include_15m:
            mark_component(run_id, "fyers_15m_history", "running", event_type="tool_started", message="Fetching optional FYERS 15-minute history")
            fifteen = client.get_history(normalized, intraday_start, end_date, "15m")
            series["15m"] = tuple(c for c in fifteen if c.close_time <= cutoff and c.is_complete)
            mark_component(run_id, "fyers_15m_history", "completed", details={"timeframe": "15m", "complete_candles": len(series["15m"])}, event_type="tool_completed", message="Optional 15-minute refinement retrieved")
        else:
            mark_component(run_id, "fyers_15m_history", "skipped", reason="Optional 15-minute refinement was not requested.", event_type="tool_skipped")

        mark_component(run_id, "technical_analysis", "running", event_type="agent_started", message="Calculating multi-timeframe technical analysis")
        mark_component(run_id, "indicator_calculation", "running", event_type="tool_started", message="Calculating technical indicators")
        technical = analyze_multi_timeframe(series, symbol=normalized)
        technical["as_of"] = cutoff.isoformat()
        mark_component(run_id, "indicator_calculation", "completed", details={"timeframes": list(series.keys()), "indicator_keys": sorted(technical.keys())}, event_type="tool_completed", message="Indicator calculation completed")
        mark_component(run_id, "technical_analysis", "completed", details={"bias": technical.get("multi_timeframe_bias"), "timeframes": list(series.keys())}, event_type="agent_completed", message="Technical analysis completed")

        fresh_events = 0
        wigolo_research_result: dict[str, Any] | None = None
        if request.refresh_news:
            mark_component(run_id, "news_research", "running", event_type="agent_started", message="Searching for stock news and policy events")
            instrument = next((m for m in _nse_equity_catalog() if m.get("symbol") == normalized), None)
            company = (instrument.get("company_name") if instrument else None) or normalized
            try:
                found_events = WEB_RESEARCH.search(f'"{company}" OR {normalized} company news results RBI policy', max_results=12)
                fresh_events = len(found_events)
                providers = {str(e.get("provider", "")).lower() for e in found_events}
                if "wigolo" in providers:
                    mark_component(run_id, "wigolo_search", "completed", details={"results": sum(1 for e in found_events if str(e.get("provider", "")).lower() == "wigolo")}, event_type="tool_completed", message="Wigolo search returned results")
                    mark_component(run_id, "google_news_rss", "skipped", reason="Wigolo returned results; RSS fallback was not used.", event_type="tool_skipped")
                elif "google_news_rss" in providers:
                    mark_component(run_id, "wigolo_search", "skipped", reason="Wigolo returned no usable results or was unavailable; RSS fallback was used.", event_type="tool_skipped")
                    mark_component(run_id, "google_news_rss", "completed", details={"results": len(found_events)}, event_type="tool_completed", message="Google News RSS fallback returned results")
                else:
                    mark_component(run_id, "wigolo_search", "unknown", reason="Search returned no provider metadata; exact provider path could not be established.", event_type="tool_unknown")
                    mark_component(run_id, "google_news_rss", "unknown", reason="Search returned no provider metadata; exact provider path could not be established.", event_type="tool_unknown")
                for event in found_events:
                    event["symbol"] = normalized
                    STORE.save_event(event)
                if found_events:
                    mark_component(run_id, "event_store_write", "completed", details={"events_saved_or_seen": len(found_events)}, event_type="tool_completed", message="Retrieved events persisted")
                else:
                    mark_component(run_id, "event_store_write", "skipped", reason="No fresh events were returned to persist.", event_type="tool_skipped")
                # Wigolo's multi-step research tool cross-checks claims and returns
                # a structured brief. Search remains responsible for event rows;
                # the brief is passed to the LangGraph analyst context separately.
                wigolo_health = WEB_RESEARCH.health()
                if wigolo_health.get("available"):
                    mark_component(run_id, "wigolo_research", "running", event_type="tool_started",
                                   message="Running multi-step Wigolo research",
                                   details={"symbol": normalized, "company": company, "depth": "quick"})
                    question = (
                        f"As of {cutoff.isoformat()}, research recent material developments for "
                        f"{company} ({normalized}) listed on NSE India. Find company disclosures, "
                        "quarterly results, RBI/SEBI/NSE/BSE notices, banking or sector policy changes, "
                        "index inclusion/exclusion, and credible financial-news coverage. Prioritize "
                        "official filings and regulator/company sources, preserve publication dates and "
                        "source URLs, distinguish confirmed facts from interpretation, report positive, "
                        "negative and uncertain implications, and state evidence gaps. Do not make up "
                        "financial figures or infer that a news headline proves a trade direction."
                    )
                    try:
                        wigolo_research_result = WEB_RESEARCH.research(question, depth="quick", max_sources=10)
                        if wigolo_research_result.get("source") == "wigolo":
                            raw = wigolo_research_result.get("result", {})
                            mark_component(run_id, "wigolo_research", "completed",
                                           details={"source_count": len(raw.get("sources", [])) if isinstance(raw, dict) else None,
                                                    "has_brief": bool(raw.get("brief")) if isinstance(raw, dict) else False},
                                           event_type="tool_completed", message="Wigolo research brief returned")
                        else:
                            mark_component(run_id, "wigolo_research", "skipped",
                                           reason="Wigolo research was unavailable; only fallback search leads were returned.",
                                           details={"source": wigolo_research_result.get("source")},
                                           event_type="tool_skipped")
                    except WebResearchError as research_exc:
                        mark_component(run_id, "wigolo_research", "failed",
                                       reason=f"Wigolo deep research failed ({type(research_exc).__name__}).",
                                       details={"error_type": type(research_exc).__name__}, event_type="tool_failed")
                else:
                    mark_component(run_id, "wigolo_research", "unavailable",
                                   reason="Wigolo REST service is not reachable; start it with `npx wigolo serve`.",
                                   details=wigolo_health, event_type="tool_unavailable")
                mark_component(run_id, "news_research", "completed", details={"results": fresh_events, "providers": sorted(providers),
                                  "wigolo_deep_research": bool(wigolo_research_result and wigolo_research_result.get("source") == "wigolo")},
                               event_type="agent_completed", message="News research stage completed")
            except WebResearchError as exc:
                mark_component(run_id, "news_research", "failed", reason=f"Research failed ({type(exc).__name__}).", details={"error_type": type(exc).__name__}, event_type="agent_failed")
                mark_component(run_id, "wigolo_search", "unknown", reason="Provider-specific result was unavailable after search failure.", event_type="tool_unknown")
                mark_component(run_id, "google_news_rss", "unknown", reason="Provider-specific result was unavailable after search failure.", event_type="tool_unknown")
                mark_component(run_id, "wigolo_research", "not_invoked", reason="News search failed before the multi-step research call could start.", details={"error_type": type(exc).__name__}, event_type="tool_not_invoked")
        else:
            mark_component(run_id, "news_research", "skipped", reason="Refresh news was unchecked; saved events are still used.", event_type="agent_skipped")
            mark_component(run_id, "wigolo_search", "skipped", reason="Refresh news was not requested.", event_type="tool_skipped")
            mark_component(run_id, "google_news_rss", "skipped", reason="Refresh news was not requested.", event_type="tool_skipped")
            mark_component(run_id, "wigolo_research", "skipped", reason="Refresh news and deep research were not requested.", event_type="tool_skipped")
            mark_component(run_id, "event_store_write", "skipped", reason="No refresh was requested; no new events were written by this run.", event_type="tool_skipped")

        mark_component(run_id, "event_store_read", "running", event_type="tool_started", message="Loading saved sourced events")
        events = STORE.list_events(normalized, 100)
        mark_component(run_id, "event_store_read", "completed", details={"event_count_loaded": len(events)}, event_type="tool_completed", message=f"Loaded {len(events)} saved events")
        mark_component(run_id, "calendar_store_read", "running", event_type="tool_started", message="Loading relevant scheduled NSE calendar events")
        calendar_start = (cutoff.astimezone(INDIA_TZ).date() - timedelta(days=30)).isoformat()
        calendar_end = (cutoff.astimezone(INDIA_TZ).date() + timedelta(days=90)).isoformat()
        calendar_events = STORE.list_calendar_events(calendar_start, calendar_end)
        mark_component(run_id, "calendar_store_read", "completed", details={
            "calendar_events_loaded": len(calendar_events), "window_start": calendar_start, "window_end": calendar_end
        }, event_type="tool_completed", message=f"Loaded {len(calendar_events)} calendar events before point-in-time filtering")
        mark_component(run_id, "fundamentals_store_read", "running", event_type="tool_started", message="Loading stored financial facts")
        raw_facts = STORE.list_facts(normalized, 2000)
        try:
            fact_summary = summarize_facts(raw_facts, as_of=cutoff)
            facts = fact_summary["facts"]
        except ValueError as exc:
            mark_component(run_id, "fundamentals_store_read", "failed", reason="Financial fact summary rejected invalid as_of.", details={"error_type": type(exc).__name__}, event_type="tool_failed")
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        mark_component(run_id, "fundamentals_store_read", "completed", details={"stored_facts": len(raw_facts), "point_in_time_facts": len(facts)}, event_type="tool_completed", message="Loaded point-in-time financial facts")
        mark_component(run_id, "fundamentals_review", "completed", details={"point_in_time_facts": len(facts), "empty_evidence": not bool(facts)}, event_type="agent_completed", message="Fundamentals evidence reviewed")

        # Build one shared, point-in-time evidence package for both the India
        # workflow and the original TradingAgents graph. In particular, do not
        # send undated/future news into the upstream analysts just because it was
        # retrieved during this run.
        mark_component(run_id, "market_movers_context", "running", event_type="tool_started",
                       message="Loading the latest eligible saved market-movers and scanner snapshot")
        market_context = _market_context_for_analysis(normalized, cutoff)
        mark_component(run_id, "market_movers_context", "completed" if market_context.get("status") == "available" else "unavailable",
                       reason=None if market_context.get("status") == "available" else market_context.get("reason"),
                       details={"status": market_context.get("status"), "job_id": market_context.get("job_id"),
                                "snapshot_age_hours": market_context.get("snapshot_age_hours"),
                                "coverage_count": market_context.get("market_breadth", {}).get("coverage_count", 0),
                                "selected_symbol_found": bool(market_context.get("selected_stock"))},
                       event_type="tool_completed" if market_context.get("status") == "available" else "tool_unavailable",
                       message="Market context prepared from saved scanner data")

        evidence_package = build_evidence_package(
            symbol=normalized, as_of=cutoff.isoformat(), technical=technical,
            events=events, facts=facts, research=wigolo_research_result, calendar_events=calendar_events,
            market_context=market_context,
        )
        point_in_time_events = evidence_package["news_and_events"]
        point_in_time_facts = evidence_package["financial_facts"]
        point_in_time_calendar_events = evidence_package.get("calendar_events", [])
        external_evidence_context = serialize_agent_context(evidence_package)
        mark_component(run_id, "evidence_package", "completed", details={
            "schema_version": evidence_package["schema_version"],
            "eligible_events": len(point_in_time_events),
            "eligible_calendar_events": len(evidence_package.get("calendar_events", [])),
            "eligible_facts": len(point_in_time_facts),
            "warnings": evidence_package["data_quality"]["warnings"],
            "serialized_context_chars": len(external_evidence_context),
        }, event_type="evidence_package_built", message="Built shared point-in-time evidence package")

        # Run the repository's actual multi-agent TradingAgents graph with this
        # exact package. Its native analyst tools still run, but analysts are told
        # to cite and reconcile the shared India evidence rather than silently
        # replacing it. The same filtered rows feed the India workflow below.
        agent_workflow: dict[str, Any] | None = None
        if request.run_agent_debate:
            graph_components = (
                "upstream_market_analyst", "upstream_fundamentals_analyst", "upstream_news_analyst",
                "upstream_social_analyst", "bull_researcher", "bear_researcher", "research_manager",
                "trader_agent", "aggressive_analyst", "conservative_analyst", "neutral_analyst",
                "portfolio_manager",
            )
            # Keep the local audit honest if the upstream graph fails: we cannot infer
            # the exact last node reached from a failed final state.
            try:
                agent_workflow = run_existing_graph(
                    normalized, cutoff.astimezone(INDIA_TZ).date().isoformat(),
                    analysts=("market", "social", "news", "fundamentals"), run_id=run_id,
                    explicit_user_request=True, external_context=external_evidence_context,
                )
                audit_after_graph = get_run(run_id) or {}
                callback_status = {item.get("component_id"): item.get("status") for item in audit_after_graph.get("components", [])}
                for component_id in graph_components:
                    # Prefer live LangChain callback events when available. The
                    # final-state reconciliation is a fallback, not a reason to
                    # overwrite a more precise observed status.
                    if callback_status.get(component_id) in {"completed", "failed"}:
                        continue
                    stage = next((item for item in agent_workflow.get("stages", []) if item.get("component_id") == component_id), None)
                    if stage is None:
                        mark_component(run_id, component_id, "unknown",
                                       reason="The graph returned no evidence to establish this component's status.",
                                       event_type="agent_status_unknown")
                    else:
                        mark_component(run_id, component_id, stage.get("status", "unknown"),
                                       reason=stage.get("reason"), details=stage.get("details", {}),
                                       event_type="agent_state_observed",
                                       message=f"Reconciled {stage['name']} from completed graph state")
                has_final_decision = bool(agent_workflow.get("summaries", {}).get("final_trade_decision"))
                agent_workflow["status"] = "completed" if has_final_decision else "completed_with_warnings"
                result = run_integrated_research(symbol=normalized, as_of=cutoff.isoformat(), technical=technical,
                                                 events=point_in_time_events, facts=point_in_time_facts,
                                                 calendar_events=point_in_time_calendar_events, market_context=market_context,
                                                 financial_health_flags=evidence_package.get("financial_health_flags"),
                                                 fundamental_analysis=evidence_package.get("fundamental_analysis"),
                                                 use_llm=request.use_llm, run_id=run_id)
                result["agent_workflow"] = agent_workflow
                result["wigolo_research"] = wigolo_research_result
                result["final_agent_rating"] = agent_workflow.get("decision", "REVIEW")
                result["final_conclusion"] = agent_workflow.get("summaries", {}).get("final_trade_decision") or "Portfolio Manager did not produce a final decision in this run. Review the agent reports and execution audit; no actionable conclusion is available."
                result["final_agent_report_path"] = agent_workflow.get("report_path")
            except Exception as graph_exc:
                agent_workflow = {"status": "failed", "error_type": type(graph_exc).__name__,
                                  "message": str(graph_exc)[:500], "stages": []}
                audit_before_reconcile = get_run(run_id) or {}
                prior_status = {item.get("component_id"): item.get("status") for item in audit_before_reconcile.get("components", [])}
                for component_id in graph_components:
                    if prior_status.get(component_id) in {"completed", "failed"}:
                        # Preserve real callback evidence rather than replacing it
                        # with a less precise inferred status after graph failure.
                        continue
                    mark_component(run_id, component_id, "unknown",
                                   reason="The graph failed before a final state established this node's reachability; no live callback result was recorded.",
                                   details={"error_type": type(graph_exc).__name__,
                                            "selected_analysts": ["market", "social", "news", "fundamentals"]},
                                   event_type="agent_status_unknown")
                audit_snapshot = get_run(run_id) or {}
                for component in audit_snapshot.get("components", []):
                    component_id = component.get("component_id", "")
                    if component_id.startswith("upstream_tool_") or component_id == "upstream_other_tool":
                        if component.get("status") in {"not_invoked", "running"}:
                            mark_component(run_id, component_id, "unknown",
                                           reason="The full graph failed before a tool result could establish whether this tool would be invoked later.",
                                           details={"error_type": type(graph_exc).__name__}, event_type="tool_status_unknown")
                result = run_integrated_research(symbol=normalized, as_of=cutoff.isoformat(), technical=technical,
                                                 events=point_in_time_events, facts=point_in_time_facts,
                                                 calendar_events=point_in_time_calendar_events, market_context=market_context,
                                                 financial_health_flags=evidence_package.get("financial_health_flags"),
                                                 fundamental_analysis=evidence_package.get("fundamental_analysis"),
                                                 use_llm=request.use_llm, run_id=run_id)
                result["agent_workflow"] = agent_workflow
                result["wigolo_research"] = wigolo_research_result
                result["final_agent_rating"] = "REVIEW"
                result["final_conclusion"] = "The full Bull/Bear and Portfolio Manager workflow failed. No final agent conclusion is available. See agent_workflow.message and server logs."
        else:
            result = run_integrated_research(symbol=normalized, as_of=cutoff.isoformat(), technical=technical,
                                             events=point_in_time_events, facts=point_in_time_facts,
                                             calendar_events=point_in_time_calendar_events, market_context=market_context,
                                                 financial_health_flags=evidence_package.get("financial_health_flags"),
                                                 fundamental_analysis=evidence_package.get("fundamental_analysis"),
                                                 use_llm=request.use_llm, run_id=run_id)
            result["agent_workflow"] = {"status": "not_invoked", "reason": "Full agent debate was unchecked for this run."}
            result["wigolo_research"] = wigolo_research_result
            result["final_agent_rating"] = "REVIEW"
            result["final_conclusion"] = "Full agent debate was not run. Enable it and run analysis again for a Portfolio Manager conclusion."
        result["evidence_package"] = evidence_package
        result["market_context"] = market_context
        result["data_quality_warnings"] = evidence_package["data_quality"]["warnings"]
        result["evidence_package_version"] = evidence_package["schema_version"]
        result["wigolo_research_context_status"] = {
            "source": evidence_package["web_research"].get("provider"),
            "brief_included_in_agent_context": bool(evidence_package["web_research"].get("brief")),
            "eligible_citation_count": len(evidence_package["web_research"].get("sources", [])),
            "excluded_unknown_timestamp_count": evidence_package["web_research"].get("excluded_unknown_timestamp_count", 0),
            "excluded_future_timestamp_count": evidence_package["web_research"].get("excluded_future_timestamp_count", 0),
            "note": "Raw research output may be returned for inspection; only the evidence_package.web_research fields are passed to agents.",
        }
        if request.use_llm and result.get("llm_used"):
            mark_component(run_id, "llm_synthesis", "completed", details={"llm_used": True}, event_type="tool_completed", message="Configured LLM synthesis completed")
        elif request.use_llm:
            mark_component(run_id, "llm_synthesis", "failed", reason="LLM synthesis was requested but deterministic fallback was returned.", details={"llm_error_type": result.get("llm_error_type")}, event_type="tool_failed")
        else:
            mark_component(run_id, "llm_synthesis", "skipped", reason="LLM synthesis was not requested; deterministic summary was used.", event_type="tool_skipped")
        trace = tracing_status()
        if trace.get("active"):
            mark_component(run_id, "langsmith_tracing", "completed", details={"active": True, "project": trace.get("project")}, event_type="tool_available", message="LangSmith tracing is configured as active; this run's remote trace availability depends on SDK callbacks.")
        else:
            mark_component(run_id, "langsmith_tracing", "unavailable", reason="LangSmith tracing is not fully active (enabled + SDK + API key required).", details=trace, event_type="tool_unavailable")
        result["stored_financial_fact_count"] = len(raw_facts)
        result["point_in_time_financial_fact_count"] = len(facts)
        result["fresh_news_results_saved"] = fresh_events
        result["data_sources"] = {"market_data": "fyers", "market_movers_and_scanner": market_context.get("source", "local saved scanner snapshot"),
                                  "events": "local sourced-event store plus Wigolo search (Google News RSS fallback)",
                                  "web_research": "Wigolo multi-step research when reachable; only publication-dated citations at/before as_of enter shared context, and unsafe mixed-date briefs are withheld",
                                  "fundamentals": "local sourced financial-fact store", "llm_synthesis_requested": request.use_llm}
        result["run_id"] = run_id
        result["execution_flow_url"] = f"/#execution?run_id={run_id}"
        # Persist a compact, source-aware summary for cross-run research memory.
        # Memory failures must never turn a completed analysis into a failed run.
        try:
            research_memory.save_snapshot(normalized, result)
            result["research_memory"] = {"persisted": True, "run_id": run_id}
        except Exception as memory_exc:  # noqa: BLE001
            result["research_memory"] = {"persisted": False, "error_type": type(memory_exc).__name__}
        final_status = "completed_with_errors" if result.get("agent_workflow", {}).get("status") in {"failed", "completed_with_warnings"} else "completed"
        finish_run(run_id, final_status, {"summary": result.get("summary", "")[:1000],
                    "technical_bias": technical.get("multi_timeframe_bias"),
                    "risk_flags": result.get("assessment", {}).get("risk_flag_count", 0),
                    "final_agent_rating": result.get("final_agent_rating"),
                    "agent_workflow_status": result.get("agent_workflow", {}).get("status")})
        return result
    except HTTPException as exc:
        finish_run(run_id, "failed", {"error_type": "HTTPException", "status_code": exc.status_code, "detail": str(exc.detail)[:300]})
        raise
    except (FyersAuthenticationError, FyersRateLimitError, FyersConfigurationError, FyersAPIError, FyersDataError, ValueError) as exc:
        finish_run(run_id, "failed", {"error_type": type(exc).__name__, "message": str(exc)[:300]})
        if isinstance(exc, FyersAuthenticationError):
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if isinstance(exc, FyersRateLimitError):
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if isinstance(exc, FyersConfigurationError):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(exc, (FyersAPIError, FyersDataError, ValueError)):
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        raise
    except Exception as exc:
        finish_run(run_id, "failed", {"error_type": type(exc).__name__, "message": str(exc)[:300]})
        raise


def _classify_research_qa_intent(question: str) -> dict[str, Any]:
    """Build a transparent, bounded retrieval plan; never permit arbitrary tools or URLs."""
    q = question.casefold().strip()
    wants_price = bool(re.search(r"\b(price|quote|trading at|current value|latest value|latest market|market price|volume today|today's volume|current price|latest price|price today)\b", q))
    wants_history = bool(re.search(r"\b(historical|history|ohlcv|candles?|chart|ema\b|vwap|technical indicators?|technical signals?|moving average|support|resistance|trend today|rsi|macd)\b", q))
    wants_news = bool(re.search(r"\b(news|headline|headlines|what happened|latest events?|today's update|recent update|breaking|announcement|announcements|press release)\b", q))
    wants_fundamentals = bool(re.search(r"\b(fundamentals?|financial statements?|revenue|earnings|profit|debt|valuation|pe ratio|p/e|balance sheet|cash flow|latest results|quarterly results)\b", q))
    wants_calendar = bool(re.search(r"\b(dividend|earnings date|board meeting|corporate action|upcoming event|upcoming events|ex-date|record date|results date|catalyst|split|bonus issue)\b", q))
    wants_compare = bool(re.search(r"\b(compare|changed|change since|since the analysis|invalidate|still valid|still a good|thesis|what changed|update the outlook|different now)\b", q))
    explicitly_fresh = bool(re.search(r"\b(latest|current|right now|today|this morning|recent|new information|live|up to date)\b", q))
    if wants_compare:
        wants_price = True
        wants_news = True
    requested = []
    if wants_price or wants_history: requested.append("market_price_history")
    if wants_news: requested.append("search_latest_news")
    if wants_fundamentals: requested.append("get_company_fundamentals")
    if wants_calendar: requested.append("get_corporate_events")
    intent = "thesis_comparison" if wants_compare else ("live_retrieval" if requested else "completed_analysis")
    rationale = ("Thesis comparison checks new market context and news against the original analysis." if wants_compare else
                 "The question requests market, news, fundamental, or event data that may not exist in the completed report." if requested else
                 "No live-retrieval trigger matched; answer from completed evidence and disclose gaps.")
    return {"intent": intent, "requested_tools": requested, "rationale": rationale,
            "explicit_freshness_requested": explicitly_fresh, "uses_original_analysis": True, "read_only": True}


def _research_qa_cutoff(question: str, requested_as_of: datetime | None = None) -> datetime | None:
    """Resolve an explicit point-in-time cutoff; do not guess one for ordinary questions."""
    if requested_as_of is not None:
        return requested_as_of if requested_as_of.tzinfo else requested_as_of.replace(tzinfo=INDIA_TZ)
    match = re.search(r"\b(?:as of|on|before|by)\s+(20\d{2}-\d{2}-\d{2})\b", question, re.I)
    if not match:
        return None
    try:
        # A date-only "as of YYYY-MM-DD" means end of that local calendar day.
        return datetime.fromisoformat(match.group(1)).replace(hour=23, minute=59, second=59, tzinfo=INDIA_TZ)
    except ValueError:
        return None


def _research_qa_parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=INDIA_TZ)
    except (TypeError, ValueError):
        return None


def _research_qa_quality(evidence: dict[str, Any], tools: list[dict[str, Any]], cutoff: datetime | None) -> dict[str, Any]:
    """Summarize provenance/freshness and flag data that cannot support point-in-time claims."""
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    market = evidence.get("market_data")
    if isinstance(market, dict):
        market["source_id"] = "M1"
        sources.append({"id": "M1", "kind": "market_data", "source": market.get("source"),
                        "retrieved_at": market.get("generated_at"), "latest_bar_date": (market.get("latest") or {}).get("date"),
                        "freshness": "daily_bar_delayed_or_coverage_dependent"})
        if cutoff:
            rows = market.get("rows") or []
            filtered = [row for row in rows if str(row.get("date", "")) <= cutoff.date().isoformat()]
            market["rows"] = filtered
            market["latest"] = filtered[-1] if filtered else None
            market["rows_cutoff_applied"] = cutoff.isoformat()
            market["latest_bar_date"] = filtered[-1].get("date") if filtered else None
            market["daily_rows_returned"] = len(filtered)
            market["previous_close"] = filtered[-2].get("close") if len(filtered) > 1 else None
            market["change_pct"] = (((filtered[-1].get("close") / filtered[-2].get("close")) - 1) * 100
                                     if len(filtered) > 1 and filtered[-2].get("close") else None)
            closes = [float(row["close"]) for row in filtered if isinstance(row.get("close"), (int, float))]
            historical_technical = {}
            for window in (20, 50, 100, 200):
                if len(closes) >= window:
                    alpha = 2 / (window + 1)
                    ema = closes[0]
                    for value in closes[1:]:
                        ema = alpha * value + (1 - alpha) * ema
                    historical_technical[f"ema{window}"] = round(ema, 4)
            market["technical_snapshot"] = historical_technical
            if not filtered:
                warnings.append("No market bars were available on or before the requested historical cutoff.")
    news = evidence.get("latest_news")
    if isinstance(news, dict):
        retained = []
        excluded_undated = 0
        excluded_future = 0
        for item in news.get("events", []):
            stamp = _research_qa_parse_timestamp(item.get("published_at") or item.get("published"))
            if cutoff:
                if stamp is None:
                    excluded_undated += 1
                    continue
                if stamp > cutoff:
                    excluded_future += 1
                    continue
            item["source_id"] = "N1"
            retained.append(item)
        news["events"] = retained
        news["count"] = len(retained)
        sources.append({"id": "N1", "kind": "news_search", "source": news.get("source_note") or "stock news search",
                        "retrieved_at": next((x.get("retrieved_at") for x in tools if x.get("tool") == "search_latest_news"), None),
                        "dated_items": len(retained), "freshness": "publication timestamps checked" if cutoff else "recent-search results; timestamps may vary"})
        if cutoff and (excluded_undated or excluded_future):
            warnings.append(f"Historical cutoff excluded {excluded_future} post-cutoff and {excluded_undated} undated news items.")
    fundamentals = evidence.get("fundamentals")
    if isinstance(fundamentals, dict):
        sources.append({"id": "F1", "kind": "fundamentals", "source": "stored sourced financial facts",
                        "last_refreshed": fundamentals.get("last_refreshed"),
                        "freshness": "refresh attempted" if fundamentals.get("refreshed_this_request") else "stored; may be stale"})
        if cutoff:
            warnings.append("Fundamental facts are not fully point-in-time filtered; do not treat them as historical-known facts without period/publication dates.")
    events = evidence.get("corporate_events")
    if isinstance(events, dict):
        sources.append({"id": "C1", "kind": "corporate_events", "source": "stored corporate-event calendar",
                        "retrieved_at": next((x.get("retrieved_at") for x in tools if x.get("tool") == "get_corporate_events"), None),
                        "freshness": "stored; verification status varies"})
        if cutoff:
            warnings.append("Corporate-event records are not fully point-in-time filtered by publication timestamp; verify before using for historical reconstruction.")
    failed = [x.get("tool") for x in tools if x.get("status") == "failed"]
    if failed:
        warnings.append("Retrieval failed for: " + ", ".join(str(x) for x in failed) + ".")
    return {"as_of_cutoff": cutoff.isoformat() if cutoff else None, "sources": sources, "warnings": warnings,
            "failed_tools": failed, "point_in_time_mode": bool(cutoff)}



def _research_qa_thesis_comparison(analysis: dict[str, Any], evidence: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Create cautious, inspectable thesis-change signals from original and retrieved evidence.

    This is a heuristic evidence organizer, not a trade signal or a statistical forecast.
    It never upgrades missing or ambiguous evidence into a bullish/bearish conclusion.
    """
    def text_of(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value or "")

    original_text = " ".join([
        text_of(analysis.get("final_agent_rating")),
        text_of(analysis.get("final_conclusion")),
        text_of(analysis.get("decision")),
        text_of((analysis.get("assessment") or {}).get("decision") if isinstance(analysis.get("assessment"), dict) else ""),
        text_of((analysis.get("technical") or {}).get("multi_timeframe_bias") if isinstance(analysis.get("technical"), dict) else ""),
        text_of(analysis.get("summary")),
    ]).casefold()
    bull_terms = bool(re.search(r"\b(bullish|buy|overweight|positive|uptrend|strong buy)\b", original_text))
    bear_terms = bool(re.search(r"\b(bearish|sell|underweight|negative|downtrend|strong sell)\b", original_text))
    original_direction = "bullish" if bull_terms and not bear_terms else "bearish" if bear_terms and not bull_terms else "mixed_or_unclear"
    findings: list[dict[str, Any]] = []
    market = evidence.get("market_data") if isinstance(evidence.get("market_data"), dict) else None
    if market:
        latest = market.get("latest") if isinstance(market.get("latest"), dict) else {}
        change = market.get("change_pct")
        if not isinstance(change, (int, float)):
            change = None
        direction = "bullish" if change is not None and change >= 1.5 else "bearish" if change is not None and change <= -1.5 else "neutral_or_small_move"
        if original_direction == "mixed_or_unclear" or direction == "neutral_or_small_move":
            classification = "insufficient_to_evaluate" if original_direction == "mixed_or_unclear" else "insufficient_or_neutral"
        elif direction == original_direction:
            classification = "supports"
        else:
            classification = "weakens"
        findings.append({"evidence": "recent_daily_price_change", "classification": classification,
                         "observed_direction": direction, "change_pct": round(change, 3) if change is not None else None,
                         "latest_bar_date": latest.get("date") or market.get("latest_bar_date"),
                         "source_id": "M1", "caveat": "A single daily move does not confirm or invalidate a thesis."})
        snapshot = market.get("technical_snapshot") if isinstance(market.get("technical_snapshot"), dict) else {}
        close = latest.get("close")
        ema20, ema50 = snapshot.get("ema20"), snapshot.get("ema50")
        if isinstance(close, (int, float)) and isinstance(ema20, (int, float)) and isinstance(ema50, (int, float)):
            if close > ema20 > ema50:
                trend = "bullish"
            elif close < ema20 < ema50:
                trend = "bearish"
            else:
                trend = "mixed"
            if original_direction == "mixed_or_unclear" or trend == "mixed":
                classification = "insufficient_to_evaluate" if original_direction == "mixed_or_unclear" else "mixed_evidence"
            elif trend == original_direction:
                classification = "supports"
            else:
                classification = "weakens"
            findings.append({"evidence": "daily_close_vs_ema20_ema50", "classification": classification,
                             "observed_direction": trend, "close": close, "ema20": ema20, "ema50": ema50,
                             "source_id": "M1", "caveat": "Daily-close trend heuristic; not an intraday signal."})
    news = evidence.get("latest_news") if isinstance(evidence.get("latest_news"), dict) else None
    if news:
        items = news.get("events") or []
        positive = sum(1 for item in items if str(item.get("direction", "")).casefold() in {"positive", "bullish", "positive_event"})
        negative = sum(1 for item in items if str(item.get("direction", "")).casefold() in {"negative", "bearish", "negative_event"})
        if positive == negative:
            news_direction = "mixed_or_unclear"
        else:
            news_direction = "positive" if positive > negative else "negative"
        if not items or news_direction == "mixed_or_unclear" or original_direction == "mixed_or_unclear":
            classification = "insufficient_to_evaluate"
        elif (news_direction == "positive" and original_direction == "bullish") or (news_direction == "negative" and original_direction == "bearish"):
            classification = "supports"
        else:
            classification = "weakens"
        findings.append({"evidence": "retrieved_news_direction_labels", "classification": classification,
                         "observed_direction": news_direction, "positive_labeled_items": positive,
                         "negative_labeled_items": negative, "items_considered": len(items), "source_id": "N1",
                         "caveat": "Provider labels and headline summaries are not independently verified; neutral or unlabeled news is not scored."})
    failures = [x.get("tool") for x in tools if x.get("status") == "failed"]
    if not findings:
        findings.append({"evidence": "available_new_evidence", "classification": "insufficient_to_evaluate",
                         "observed_direction": "unknown", "caveat": "No usable market/news evidence was retrieved for comparison."})
    material = [f for f in findings if f["classification"] in {"supports", "weakens"}]
    if failures:
        overall = "incomplete_retrieval"
    elif not material:
        overall = "insufficient_evidence"
    elif any(f["classification"] == "supports" for f in material) and any(f["classification"] == "weakens" for f in material):
        overall = "mixed_evidence"
    elif any(f["classification"] == "weakens" for f in material):
        overall = "thesis_weakened_by_available_signals"
    else:
        overall = "available_signals_support_thesis"
    return {"original_direction": original_direction, "overall_assessment": overall,
            "findings": findings, "failed_tools": failures,
            "confidence": "low" if failures or original_direction == "mixed_or_unclear" or not material else "low_to_moderate",
            "interpretation_limits": ["Heuristic comparison, not a forecast or trade instruction.",
                                      "Daily price and provider-labeled news are partial evidence; they cannot alone prove a thesis valid or invalid.",
                                      "A conclusion requires checking evidence timestamps, source quality, and the original analysis horizon."]}


def _research_qa_live_tools(symbol: str, question: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run only bounded, read-only retrieval tools selected from the user's question."""
    q = question.casefold()
    intent = _classify_research_qa_intent(question)
    requested = set(intent["requested_tools"])
    compare_terms = r"\b(compare|changed|change since|since the analysis|invalidate|still valid|still a good|thesis|what changed|update the outlook|different now)\b"
    wants_compare = intent["intent"] == "thesis_comparison"
    wants_price = "market_price_history" in requested and (wants_compare or bool(re.search(r"\b(price|quote|trading at|current value|latest value|latest market|market price|volume today|today's volume|current price|latest price|price today)\b", q)))
    wants_history = "market_price_history" in requested and not wants_price
    wants_news = "search_latest_news" in requested
    wants_fundamentals = "get_company_fundamentals" in requested
    wants_calendar = "get_corporate_events" in requested
    retrieved: dict[str, Any] = {}
    used: list[dict[str, Any]] = []

    if wants_price or wants_history:
        try:
            period = "1y" if re.search(r"\b(1 year|one year|12 months|long[- ]term|historical|history|year)\b", q) else ("6mo" if wants_history else "1mo")
            price_data = market_price_history(symbol, period=period)
            rows = price_data.get("rows", [])
            # Provide a compact technical snapshot derived from returned daily closes.
            closes = [float(r["close"]) for r in rows if isinstance(r, dict) and isinstance(r.get("close"), (int, float))]
            technical = {}
            for window in (20, 50, 100, 200):
                if len(closes) >= window:
                    technical[f"ema{window}"] = round(sum(closes[-window:]) / window, 4) if window == 1 else None
                    # True EMA calculation (rather than a simple moving average).
                    alpha = 2 / (window + 1)
                    ema = closes[0]
                    for value in closes[1:]:
                        ema = alpha * value + (1 - alpha) * ema
                    technical[f"ema{window}"] = round(ema, 4)
            retrieved["market_data"] = {"source": price_data.get("source"), "generated_at": price_data.get("generated_at"),
                "period": period, "latest": price_data.get("latest"), "previous_close": price_data.get("previous_close"),
                "change_pct": price_data.get("change_pct"), "daily_rows_returned": len(rows), "rows": rows[-260:], "technical_snapshot": technical,
                "note": "Daily bars may be delayed. These are daily-close EMAs, not intraday indicators; VWAP is not inferred from daily candles."}
            used.append({"tool": "market_price_history", "status": "completed", "source": price_data.get("source"), "retrieved_at": price_data.get("generated_at")})
        except Exception as exc:
            retrieved["market_data_error"] = f"Market data retrieval failed ({type(exc).__name__})."
            used.append({"tool": "market_price_history", "status": "failed", "error_type": type(exc).__name__})

    if wants_news:
        try:
            news_data = stock_news(symbol, max_results=8, persist=False)
            events = news_data.get("events", [])
            retrieved["latest_news"] = {"source_note": news_data.get("note"), "count": len(events), "events": [
                {k: item.get(k) for k in ("title", "summary", "source", "source_name", "source_url", "published_at", "published", "direction", "quality", "symbol") if item.get(k) is not None}
                for item in events[:8] if isinstance(item, dict)]}
            used.append({"tool": "search_latest_news", "status": "completed", "results": len(events), "retrieved_at": datetime.now(INDIA_TZ).isoformat()})
        except Exception as exc:
            retrieved["latest_news_error"] = f"News retrieval failed ({type(exc).__name__})."
            used.append({"tool": "search_latest_news", "status": "failed", "error_type": type(exc).__name__})

    if wants_fundamentals:
        try:
            refreshed = False
            refresh_requested = bool(re.search(r"\b(refresh|update|fetch|retrieve)\b", q)) and bool(re.search(r"\b(fundamentals?|financial statements?|latest results|financials)\b", q))
            refresh_note = None
            if refresh_requested:
                # Refresh is only triggered when the user explicitly asks to update/fetch financial data.
                try:
                    refresh_yahoo_snapshot(symbol, STORE)
                    refreshed = True
                except Exception as refresh_exc:  # statement refresh is separate and may still succeed
                    refresh_note = f"Snapshot refresh unavailable ({type(refresh_exc).__name__})."
                try:
                    from tradingagents.india.statements import refresh_yahoo_statements
                    refresh_yahoo_statements(symbol, STORE, years=5)
                    refreshed = True
                except Exception as refresh_exc:
                    refresh_note = (refresh_note + " " if refresh_note else "") + f"Statement refresh unavailable ({type(refresh_exc).__name__})."
            facts = STORE.list_facts(symbol, 30)
            retrieved["fundamentals"] = {"facts": facts[:30], "last_refreshed": last_refreshed(STORE, symbol),
                "source_guidance": source_guidance(), "refresh_requested": refresh_requested, "refreshed_this_request": refreshed,
                "refresh_note": refresh_note, "note": "Stored sourced facts; freshness is reported separately and no missing values are inferred."}
            used.append({"tool": "get_company_fundamentals", "status": "completed", "facts": len(facts),
                         "freshness": "refreshed-this-request" if refreshed else "stored-data", "refresh_requested": refresh_requested})
        except Exception as exc:
            retrieved["fundamentals_error"] = f"Fundamentals retrieval failed ({type(exc).__name__})."
            used.append({"tool": "get_company_fundamentals", "status": "failed", "error_type": type(exc).__name__})

    if wants_calendar:
        try:
            today = datetime.now(INDIA_TZ).date()
            rows = STORE.list_calendar_events((today - timedelta(days=7)).isoformat(), (today + timedelta(days=90)).isoformat())
            relevant = [row for row in rows if not row.get("symbol") or str(row.get("symbol", "")).upper() == symbol]
            retrieved["corporate_events"] = {"from": (today - timedelta(days=7)).isoformat(), "through": (today + timedelta(days=90)).isoformat(), "events": relevant[:20],
                "note": "Stored calendar entries can be unverified; confirm material events against official company or exchange filings."}
            used.append({"tool": "get_corporate_events", "status": "completed", "events": len(relevant), "retrieved_at": datetime.now(INDIA_TZ).isoformat()})
        except Exception as exc:
            retrieved["corporate_events_error"] = f"Corporate-event retrieval failed ({type(exc).__name__})."
            used.append({"tool": "get_corporate_events", "status": "failed", "error_type": type(exc).__name__})

    return retrieved, used


@app.get("/api/research-memory/{symbol}")
@traced("india.api.research_memory")
def get_research_memory(symbol: str, limit: int = Query(default=20, ge=1, le=50)) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    try:
        runs = research_memory.list_snapshots(normalized, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Research memory is unavailable ({type(exc).__name__}).") from exc
    return {"symbol": normalized, "count": len(runs), "runs": runs}


@app.get("/api/research-memory/{symbol}/{run_id}")
@traced("india.api.research_memory_detail")
def get_research_memory_detail(symbol: str, run_id: str) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    try:
        snapshot = research_memory.get_snapshot(normalized, run_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Research memory is unavailable ({type(exc).__name__}).") from exc
    if not snapshot:
        raise HTTPException(status_code=404, detail="Saved analysis run was not found for this symbol.")
    return {"symbol": normalized, "snapshot": snapshot}


@app.post("/api/research-memory/{symbol}/compare-runs")
@traced("india.api.research_memory_compare_runs")
def compare_research_memory_runs(symbol: str, request: ResearchMemoryPairRequest) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    try:
        previous = research_memory.get_snapshot(normalized, request.previous_run_id)
        current = research_memory.get_snapshot(normalized, request.current_run_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Research memory is unavailable ({type(exc).__name__}).") from exc
    if not previous or not current:
        raise HTTPException(status_code=404, detail="One or both selected analysis runs were not found for this symbol.")
    if previous.get("run_id") == current.get("run_id"):
        raise HTTPException(status_code=422, detail="Choose two different analysis runs.")
    comparison = research_memory.compare_snapshots(previous, current)
    return {"symbol": normalized, "comparison_available": True, "comparison": comparison,
            "previous": {k: previous.get(k) for k in ("run_id", "as_of", "rating", "technical_bias", "conclusion", "summary", "technical_metrics", "news_evidence", "catalyst_evidence", "source_summary", "data_quality_warnings")},
            "current": {k: current.get(k) for k in ("run_id", "as_of", "rating", "technical_bias", "conclusion", "summary", "technical_metrics", "news_evidence", "catalyst_evidence", "source_summary", "data_quality_warnings")}}


@app.post("/api/research-memory/{symbol}/ask")
@traced("india.api.research_memory_historical_qa")
def ask_research_memory(symbol: str, request: ResearchMemoryQuestionRequest) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    previous = research_memory.get_snapshot(normalized, request.previous_run_id)
    current = research_memory.get_snapshot(normalized, request.current_run_id)
    if not previous or not current:
        raise HTTPException(status_code=404, detail="One or both selected analysis runs were not found for this symbol.")
    comparison = research_memory.compare_snapshots(previous, current)
    context = {"symbol": normalized, "previous_snapshot": previous, "current_snapshot": current, "deterministic_comparison": comparison}
    context_text = json.dumps(context, ensure_ascii=False, default=str)
    if len(context_text) > 18000:
        context["previous_snapshot"] = {k: previous.get(k) for k in ("run_id", "as_of", "rating", "technical_bias", "conclusion", "summary", "technical_metrics", "data_quality_warnings")}
        context["current_snapshot"] = {k: current.get(k) for k in ("run_id", "as_of", "rating", "technical_bias", "conclusion", "summary", "technical_metrics", "data_quality_warnings")}
        context_text = json.dumps(context, ensure_ascii=False, default=str)
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.llm_clients import create_tier_client
        llm = create_tier_client(DEFAULT_CONFIG.copy(), "quick").get_llm()
        prompt = (
            "You are a historical stock-research assistant. Answer only from the saved snapshots and deterministic comparison provided. "
            "Do not use or imply live data. Distinguish recorded facts from interpretation. Missing data is unavailable, not zero. "
            "A source absent from a later snapshot is not proven retracted; a past-due catalyst is not proof it occurred. "
            "Cite snapshot run IDs and as-of timestamps where relevant. This is research, not a trade instruction.\n\n"
            f"SAVED HISTORICAL EVIDENCE (JSON):\n{context_text}\n\nQUESTION: {request.question.strip()}\n\n"
            "Return a direct answer, then evidence used and limitations."
        )
        response = llm.invoke(prompt)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
        answer = str(content).strip()
        if not answer:
            raise ValueError("The configured model returned an empty answer.")
        return {"symbol": normalized, "answer": answer[:10000], "previous_run_id": previous.get("run_id"),
                "current_run_id": current.get("run_id"), "previous_as_of": previous.get("as_of"),
                "current_as_of": current.get("as_of"), "comparison": comparison, "mode": "saved_historical_evidence_only"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Historical research Q&A could not reach the configured LLM provider ({type(exc).__name__}). Check provider configuration and server logs.") from exc


@app.post("/api/research-memory/{symbol}/compare")
@traced("india.api.research_memory_compare")
def compare_research_memory(symbol: str, request: ResearchMemoryCompareRequest) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    current = request.analysis
    current_symbol = str(current.get("symbol") or current.get("ticker") or normalized).upper()
    if current_symbol != normalized:
        raise HTTPException(status_code=422, detail="The supplied analysis does not match the requested symbol.")
    current_snapshot = research_memory.build_snapshot(normalized, current)
    try:
        history = research_memory.list_snapshots(normalized, limit=50)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Research memory is unavailable ({type(exc).__name__}).") from exc
    current_run_id = current_snapshot.get("run_id")
    previous = next((item for item in history if item.get("run_id") and item.get("run_id") != current_run_id), None)
    if previous is None:
        return {"symbol": normalized, "comparison_available": False,
                "message": "No earlier persisted analysis was found for this symbol. Run Full Analysis again after at least one saved run to compare changes.",
                "current_run_id": current_run_id, "current_as_of": current_snapshot.get("as_of")}
    return {"symbol": normalized, "comparison_available": True,
            "comparison": research_memory.compare_snapshots(previous, current_snapshot)}


def _research_qa_compact_value(value: Any, *, depth: int = 0, max_string: int = 500) -> Any:
    """Bound nested analysis values before serializing them into the Q&A prompt."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "…[truncated]"
    if depth >= 4:
        return str(value)[:160] + ("…[truncated]" if len(str(value)) > 160 else "")
    if isinstance(value, dict):
        # Prefer concise, decision-relevant keys; large raw payload fields are excluded.
        preferred = ("symbol", "as_of", "run_id", "rating", "bias", "assessment", "conclusion", "summary",
                     "trend", "close", "price", "change_pct", "ema20", "ema50", "ema100", "ema200",
                     "rsi", "macd", "signal", "status", "source", "source_url", "url", "published_at",
                     "date", "title", "headline", "category", "severity", "message", "name", "value", "unit")
        blocked = {"raw_report", "report", "details", "candles", "history", "ohlcv", "full_text", "html", "content_html", "debug", "traceback"}
        keys = list(value.keys())
        keys.sort(key=lambda k: (str(k).lower() not in preferred, str(k).lower()))
        out = {}
        for key in keys:
            if str(key).lower() in blocked or len(out) >= 24:
                continue
            out[str(key)[:80]] = _research_qa_compact_value(value[key], depth=depth + 1, max_string=max_string)
        return out
    if isinstance(value, (list, tuple)):
        return [_research_qa_compact_value(item, depth=depth + 1, max_string=max_string) for item in list(value)[:8]]
    return str(value)[:160]


def _research_qa_compact_analysis(analysis: dict[str, Any], symbol: str, max_chars: int = 16000) -> tuple[dict[str, Any], str]:
    """Build a bounded evidence summary without rejecting oversized full-analysis payloads."""
    assessment = analysis.get("assessment") if isinstance(analysis.get("assessment"), dict) else {}
    evidence = analysis.get("evidence_package") if isinstance(analysis.get("evidence_package"), dict) else {}
    technical = analysis.get("technical") or evidence.get("technical")
    web_research = evidence.get("web_research") if isinstance(evidence.get("web_research"), dict) else {}
    compact = {
        "symbol": symbol,
        "as_of": analysis.get("as_of") or evidence.get("as_of"),
        "run_id": analysis.get("run_id"),
        "final_agent_rating": analysis.get("final_agent_rating"),
        "final_conclusion": analysis.get("final_conclusion"),
        "summary": analysis.get("summary"),
        "assessment": assessment,
        "technical": technical,
        "market_context": analysis.get("market_context") or evidence.get("market_context"),
        "events": evidence.get("events") or analysis.get("events") or [],
        "calendar_events": evidence.get("calendar_events") or analysis.get("calendar_events") or [],
        "financial_facts": evidence.get("facts") or analysis.get("financial_facts") or [],
        "financial_health_flags": evidence.get("financial_health_flags") or analysis.get("financial_health_flags"),
        "fundamental_analysis": evidence.get("fundamental_analysis") or analysis.get("fundamental_analysis"),
        "web_research": {"sources": web_research.get("sources", []) if isinstance(web_research, dict) else []},
        "data_quality_warnings": analysis.get("data_quality_warnings") or (evidence.get("data_quality", {}).get("warnings", []) if isinstance(evidence.get("data_quality"), dict) else []),
        "data_sources": analysis.get("data_sources", {}),
    }
    # First pass limits nested depth, strings and list sizes. Then progressively drop
    # lower-priority sections if the original analysis contains unusually large reports.
    compact = _research_qa_compact_value(compact, max_string=700)
    context_text = json.dumps(compact, ensure_ascii=False, default=str)
    for key in ("fundamental_analysis", "data_sources", "financial_facts", "events", "calendar_events", "web_research", "market_context", "technical", "assessment"):
        if len(context_text) <= max_chars:
            break
        if key in {"financial_facts", "events", "calendar_events"} and isinstance(compact.get(key), list):
            compact[key] = compact[key][:3]
        else:
            compact.pop(key, None)
        context_text = json.dumps(compact, ensure_ascii=False, default=str)
    # Last-resort safe fallback retains identity, conclusion and warnings, never raises 413.
    if len(context_text) > max_chars:
        compact = {k: compact.get(k) for k in ("symbol", "as_of", "run_id", "final_agent_rating", "final_conclusion", "summary", "data_quality_warnings") if compact.get(k) is not None}
        compact = _research_qa_compact_value(compact, max_string=180)
        context_text = json.dumps(compact, ensure_ascii=False, default=str)
    return compact, context_text


@app.post("/api/research-qa/{symbol}")
@traced("india.api.research_qa")
def research_qa(symbol: str, request: ResearchQARequest) -> dict[str, Any]:
    """Answer from completed evidence first, adding bounded read-only live retrieval when the question needs it."""
    normalized = _known_symbol(symbol)
    analysis = request.analysis
    if str(analysis.get("symbol", normalized)).strip().upper() != normalized:
        raise HTTPException(status_code=422, detail="The supplied analysis does not match the requested symbol.")
    if not analysis or not any(key in analysis for key in ("assessment", "technical", "evidence_package", "final_conclusion", "summary")):
        raise HTTPException(status_code=422, detail="Run Full Analysis first so the assistant has completed-analysis evidence.")

    compact, context_text = _research_qa_compact_analysis(analysis, normalized)

    retrieval_plan = _classify_research_qa_intent(request.question)
    live_evidence, live_tools_used = _research_qa_live_tools(normalized, request.question)
    point_in_time_cutoff = _research_qa_cutoff(request.question, request.as_of)
    evidence_quality = _research_qa_quality(live_evidence, live_tools_used, point_in_time_cutoff)
    thesis_comparison = (_research_qa_thesis_comparison(analysis, live_evidence, live_tools_used)
                         if retrieval_plan["intent"] == "thesis_comparison" else None)
    live_text = json.dumps(live_evidence, ensure_ascii=False, default=str)
    if len(live_text) > 10000:
        live_text = live_text[:10000] + "…[live evidence truncated to remain within the Q&A context budget]"

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.llm_clients import create_tier_client
        llm = create_tier_client(DEFAULT_CONFIG.copy(), "quick").get_llm()
        history = []
        for item in request.history[-6:]:
            role, content = item.get("role", ""), item.get("content", "")
            if role in {"user", "assistant"} and content:
                history.append(f"{role.upper()}: {content[:1200]}")
        prompt = (
            "You are TradingAgents India's conversational research assistant. Use completed-analysis evidence first, then use any live-retrieval evidence supplied below. "
            "Never claim a tool ran unless it appears in LIVE RETRIEVAL EVIDENCE. Never invent prices, indicators, dates, metrics, sources or results. "
            "Clearly separate original-analysis facts, newly retrieved facts, and your interpretation. Include timestamps and source/provenance where provided. "
            "If a tool failed or data is absent, say so. Stored fundamentals/calendar records are not necessarily fresh; disclose this. Daily-close EMAs are not intraday indicators and do not infer VWAP from daily bars. "
            "When evidence source IDs are provided, attach them to material factual claims using [M1], [N1], [F1], or [C1] only when that source supports the claim. Do not invent source IDs or links. "
            "If a historical cutoff is requested, never use post-cutoff or undated news as historical evidence. Explicitly disclose that stored fundamentals/corporate events may not be fully point-in-time filtered. "
            "If the user asks whether new evidence changes the old thesis, compare the old report cutoff with the retrieved timestamps and explain what did or did not change. This is research, not a guaranteed outcome or an instruction to trade.\n\n"
            f"COMPLETED ANALYSIS (bounded JSON):\n{context_text}\n\n"
            f"LIVE RETRIEVAL EVIDENCE (bounded JSON; may be empty):\n{live_text or '(no live retrieval selected for this question)'}\n\n"
            f"TOOLS ATTEMPTED: {json.dumps(live_tools_used, ensure_ascii=False)}\n\n"
            f"EVIDENCE QUALITY / PROVENANCE: {json.dumps(evidence_quality, ensure_ascii=False)}\n\n"
            f"THESIS COMPARISON SIGNALS (heuristic; do not treat as a trade signal): {json.dumps(thesis_comparison, ensure_ascii=False) if thesis_comparison else '(not requested)'}\n\n"
            f"RECENT CONVERSATION:\n{chr(10).join(history) or '(none)'}\n\nUSER QUESTION: {request.question.strip()}\n\n"
            "Return a direct answer, then 'Evidence used', 'Freshness / limitations', and sources when available."
        )
        response = llm.invoke(prompt)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
        answer = str(content).strip()
        if not answer:
            raise ValueError("The configured model returned an empty answer.")
        mode = "combined" if live_tools_used else "completed_analysis"
        return {"symbol": normalized, "answer": answer[:12000], "mode": mode, "live_tools_used": live_tools_used,
                "live_retrieval": live_evidence, "analysis_run_id": analysis.get("run_id"), "analysis_as_of": compact.get("as_of"),
                "context_characters": len(context_text), "live_context_characters": len(live_text), "live_retrieval_selected": bool(live_tools_used),
                "retrieval_plan": retrieval_plan, "evidence_quality": evidence_quality,
                "thesis_comparison": thesis_comparison}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Analysis Q&A could not reach the configured LLM provider ({type(exc).__name__}). Check provider configuration and server logs.") from exc


@app.post("/api/agent-analysis/{symbol}")
@traced("india.api.agent_analysis")
def agent_analysis(symbol: str, trade_date: date | None = None, analysts: str = Query(default="market,social,news,fundamentals", max_length=100)) -> dict[str, Any]:
    if os.getenv("ENABLE_LANGGRAPH_AGENT_ANALYSIS", "false").strip().lower() not in {"1", "true", "yes"}:
        raise HTTPException(
            status_code=403,
            detail="LangGraph agent analysis is disabled by default. Set ENABLE_LANGGRAPH_AGENT_ANALYSIS=true to enable it.",
        )
    normalized = _known_symbol(symbol)
    as_of = trade_date or datetime.now(INDIA_TZ).date()
    allowed = {"market", "news", "fundamentals", "social"}
    selected = tuple(dict.fromkeys(part.strip().lower() for part in analysts.split(",") if part.strip()))
    if not selected or any(item not in allowed for item in selected):
        raise HTTPException(status_code=422, detail="analysts must be a comma-separated subset of market,news,fundamentals,social")
    run_id = create_run("upstream_agent_graph", normalized, {"trade_date": as_of.isoformat(), "analysts": list(selected)})
    try:
        result = run_existing_graph(normalized, as_of.isoformat(), selected, run_id=run_id, explicit_user_request=True)
        for stage in result.get("stages", []):
            component_id = stage.get("component_id")
            if component_id:
                mark_component(run_id, component_id, stage.get("status", "unknown"),
                               reason=stage.get("reason"), details=stage.get("details", {}),
                               event_type="agent_state_observed", message=f"Reconciled {stage.get('name', component_id)} from completed graph state")
        result["run_id"] = run_id
        result["execution_flow_url"] = f"/#execution?run_id={run_id}"
        # Persist a compact, source-aware summary for cross-run research memory.
        # Memory failures must never turn a completed analysis into a failed run.
        try:
            research_memory.save_snapshot(normalized, result)
            result["research_memory"] = {"persisted": True, "run_id": run_id}
        except Exception as memory_exc:  # noqa: BLE001
            result["research_memory"] = {"persisted": False, "error_type": type(memory_exc).__name__}
        finish_run(run_id, "completed", {"decision": result.get("decision")})
        return result
    except AgentRunDisabled as exc:
        finish_run(run_id, "failed", {"error_type": type(exc).__name__, "message": str(exc)[:300]})
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Do not expose provider responses, prompts, or credentials to the client.
        import logging
        logging.getLogger(__name__).exception("Existing LangGraph analysis failed")
        finish_run(run_id, "failed", {"error_type": type(exc).__name__, "message": str(exc)[:300]})
        raise HTTPException(status_code=502, detail=f"LangGraph analysis failed ({type(exc).__name__}). Check server logs and LLM provider configuration.") from exc


def _daily_market_snapshot(symbol: str, candles: tuple[Any, ...], as_of: datetime) -> dict[str, Any]:
    """Summarize only completed daily candles for market-mover context."""
    completed = [c for c in candles if getattr(c, "is_complete", False) and c.close_time <= as_of]
    completed.sort(key=lambda c: c.close_time)
    if not completed:
        return {"market_snapshot_status": "insufficient_data", "market_snapshot_symbol": symbol}
    latest = completed[-1]
    previous = completed[-2] if len(completed) > 1 else None
    # Relative volume compares the latest session against prior sessions, excluding
    # the latest session itself to avoid diluting a genuine volume spike.
    volumes = [float(c.volume) for c in completed[-21:-1] if isinstance(c.volume, (int, float)) and c.volume >= 0]
    avg_volume = sum(volumes) / len(volumes) if volumes else None
    change_pct = ((latest.close - previous.close) / previous.close * 100.0) if previous and previous.close else None
    relative_volume = (latest.volume / avg_volume) if avg_volume and avg_volume > 0 else None
    return {
        "market_snapshot_status": "ok",
        "market_snapshot_symbol": symbol.upper(),
        "session_date": latest.open_time.date().isoformat(),
        "previous_session_date": previous.open_time.date().isoformat() if previous else None,
        "close": float(latest.close),
        "previous_close": float(previous.close) if previous else None,
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "volume": int(latest.volume),
        "average_volume_20_sessions": round(avg_volume, 2) if avg_volume is not None else None,
        "relative_volume_20_sessions": round(relative_volume, 4) if relative_volume is not None else None,
        "traded_value_estimate": round(float(latest.close) * float(latest.volume), 2),
        "market_snapshot_close_time": latest.close_time.isoformat(),
        "market_snapshot_source": "FYERS completed daily candles",
    }


def _market_context_for_analysis(symbol: str, cutoff: datetime) -> dict[str, Any]:
    """Use the latest completed scanner snapshot available by cutoff; never fetch on-demand."""
    jobs = STORE.list_scan_jobs(limit=50, completed_only=True)
    chosen = None
    cutoff_utc = cutoff.astimezone(timezone.utc) if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
    for job in jobs:
        result = job.get("result") or {}
        timestamp = result.get("as_of") or job.get("updated_at")
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                continue
            parsed = parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if parsed <= cutoff_utc:
            age_hours = max(0.0, (cutoff_utc - parsed).total_seconds() / 3600.0)
            if age_hours <= 48:
                chosen = (job, result, parsed, age_hours)
                break
    if chosen is None:
        return {"status": "unavailable", "source": "latest completed local scanner job",
                "reason": "No completed scanner snapshot within 48 hours was available at the requested as_of timestamp.",
                "selected_symbol": symbol.upper(), "warnings": ["Market-wide context was not included; run the scanner to populate it."]}
    job, result, parsed, age_hours = chosen
    rows = [row for row in result.get("results", []) if isinstance(row, dict) and row.get("market_snapshot_status") == "ok"]
    # A completed scan can still be partially populated; do not represent it as a full-market census.
    gainers = sorted((r for r in rows if isinstance(r.get("change_pct"), (int, float))), key=lambda r: r["change_pct"], reverse=True)
    losers = list(reversed(gainers))
    volume = sorted((r for r in rows if isinstance(r.get("volume"), (int, float))), key=lambda r: r["volume"], reverse=True)
    advancers = sum(1 for r in gainers if r["change_pct"] > 0)
    decliners = sum(1 for r in gainers if r["change_pct"] < 0)
    unchanged = sum(1 for r in gainers if r["change_pct"] == 0)
    selected = next((r for r in rows if str(r.get("symbol", "")).upper() == symbol.upper()), None)
    compact = lambda r: {k: r.get(k) for k in ("symbol", "session_date", "close", "change_pct", "volume", "relative_volume_20_sessions", "traded_value_estimate", "multi_timeframe_bias", "aggregate_trend_score")}
    return {"status": "available", "source": "FYERS completed daily candles via saved scanner job",
            "as_of": parsed.isoformat(), "snapshot_age_hours": round(age_hours, 2),
            "job_id": job.get("job_id"), "universe": {"requested": result.get("count_requested"), "completed": len(rows), "errors": result.get("count_errors", 0), "scope": "user-triggered configured NIFTY 50 scanner; not all NSE stocks"},
            "market_breadth": {"advancers": advancers, "decliners": decliners, "unchanged": unchanged, "coverage_count": len(gainers)},
            "selected_symbol": symbol.upper(), "selected_stock": compact(selected) if selected else None,
            "top_gainers": [compact(r) for r in gainers[:10]], "top_losers": [compact(r) for r in losers[:10]],
            "highest_volume": [compact(r) for r in volume[:10]],
            "warnings": (["Scanner output is a configured NIFTY 50 subset, not an all-NSE ranking."] + (["Some requested symbols failed to fetch."] if result.get("count_errors") else []))}


def _run_scan_job(job_id: str, symbols: list[str], lookback_days: int, include_15m: bool) -> None:
    started = datetime.now(timezone.utc).isoformat()
    STORE.update_scan_job(job_id, status="running", now=started)
    try:
        with _SCAN_LOCK:
            client = _fyers_client()
            end_date = datetime.now(INDIA_TZ).date()
            daily_start = end_date - timedelta(days=lookback_days)
            intraday_start = end_date - timedelta(days=min(95, lookback_days))
            results = []
            errors = []
            for symbol in symbols:
                try:
                    daily = client.get_history(symbol, daily_start, end_date, "1d")
                    hourly = client.get_history(symbol, intraday_start, end_date, "1h")
                    scan_cutoff = datetime.now(INDIA_TZ)
                    market_snapshot = _daily_market_snapshot(symbol, daily, scan_cutoff)
                    daily = tuple(c for c in daily if c.close_time <= scan_cutoff and c.is_complete)
                    hourly = tuple(c for c in hourly if c.close_time <= scan_cutoff and c.is_complete)
                    series: dict[str, Any] = {"1d": daily, "4h": aggregate_hourly_to_four_hour(hourly), "1h": hourly}
                    if include_15m:
                        series["15m"] = client.get_history(symbol, intraday_start, end_date, "15m")
                    report = analyze_multi_timeframe(series, symbol=symbol)
                    report.update(market_snapshot)
                    report["source"] = "fyers"
                    results.append(report)
                except Exception as exc:
                    errors.append({"symbol": symbol, "error_type": type(exc).__name__, "message": str(exc)[:300]})
            results.sort(key=lambda r: (r.get("multi_timeframe_bias") == "bullish_alignment", r.get("aggregate_trend_score", 0)), reverse=True)
            payload = {"as_of": datetime.now(INDIA_TZ).isoformat(), "count_requested": len(symbols),
                       "count_completed": len(results), "count_errors": len(errors), "results": results, "errors": errors,
                       "note": "Candidate research only; not trade instructions. Full-universe scans consume provider request quotas."}
            final_status = "completed" if not errors else "completed_with_errors"
            STORE.update_scan_job(job_id, status=final_status, now=datetime.now(timezone.utc).isoformat(), result=payload)
    except Exception as exc:
        STORE.update_scan_job(job_id, status="failed", now=datetime.now(timezone.utc).isoformat(), error=f"{type(exc).__name__}: {str(exc)[:300]}")


@app.post("/api/scan")
def create_scan(request: ScanRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    # This is a user-triggered batch job, not a five-minute auto-loop. The default NIFTY 50 run
    # can consume ~100 historical requests and should not be scheduled every five minutes on Standard limits.
    _fyers_client()  # fail fast when credentials are absent
    universe_symbols = list(DEFAULT_NIFTY50_REGISTRY.symbols("NIFTY 50"))
    symbols = list(dict.fromkeys((item.strip().upper() for item in request.symbols if item.strip()))) if request.symbols else universe_symbols
    unknown = sorted(set(symbols) - set(universe_symbols))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Symbols outside configured NIFTY 50 snapshot: {', '.join(unknown)}")
    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    params = {"symbols": symbols, "lookback_days": request.lookback_days, "include_15m": request.include_15m}
    STORE.create_scan_job(job_id, params, now)
    background_tasks.add_task(_run_scan_job, job_id, symbols, request.lookback_days, request.include_15m)
    return {"job_id": job_id, "status": "queued", "count_requested": len(symbols), "poll_url": f"/api/scan/{job_id}",
            "note": "Full-universe scans are quota-limited; do not trigger every five minutes without a streaming/incremental data design."}


@app.get("/api/scan/{job_id}")
def get_scan(job_id: str) -> dict[str, Any]:
    job = STORE.get_scan_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scan job not found")
    return job


@app.get("/api/market/movers-snapshot")
def market_movers_snapshot() -> dict[str, Any]:
    """Return daily gainers, losers and volume rankings from the latest completed scan.

    This endpoint intentionally does not trigger provider requests. Run the scanner
    explicitly to refresh the snapshot and control FYERS request usage.
    """
    context = _market_context_for_analysis("", datetime.now(INDIA_TZ))
    if context.get("status") != "available":
        return {**context, "note": "Run the Market Scanner to generate a fresh snapshot; no provider request was triggered."}
    return {**context, "note": "Rankings are based on completed daily candles from the latest saved NIFTY 50 scanner snapshot; not a full NSE universe or real-time feed."}


@app.get("/api/executions")
def executions(limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
    runs = list_runs(limit)
    return {"count": len(runs), "runs": runs}


@app.get("/api/executions/{run_id}")
def execution_detail(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Execution run not found")
    return run


@app.get("/api/execution-registry")
def execution_registry() -> dict[str, Any]:
    return {"count": len(COMPONENTS), "components": COMPONENTS,
            "status_definitions": {
                "completed": "Invocation was observed and completed.", "running": "Invocation started and has not completed.",
                "failed": "Invocation failed.", "skipped": "Workflow logic or user settings deliberately bypassed the component.",
                "not_invoked": "Component belongs to this workflow but no invocation was observed.",
                "not_reached": "Run ended before the component could be confirmed complete.",
                "unavailable": "Component was not configured or not available at runtime.",
                "not_applicable": "Component belongs to a different workflow path.",
                "unknown": "Available evidence cannot establish whether or how it ran."
            }}


@app.get("/api/tracing")
def tracing() -> dict[str, Any]:
    return tracing_status()


@app.get("/api/news/search")
@traced("india.api.news_search")
def news_search(query: str = Query(min_length=2, max_length=300), max_results: int = Query(default=10, ge=1, le=25), persist: bool = True) -> dict[str, Any]:
    search_query = build_recent_news_query(query, days=30)
    try:
        events = [score_news_item(event) for event in WEB_RESEARCH.search(search_query, max_results=max_results)]
    except WebResearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Preserve provider results for review, but expose quality metadata immediately.
    if persist:
        for event in events:
            STORE.save_event(event)
    calendar_added = _calendar_ingest(events, provider="news:search") if persist else 0
    return {"query": query, "search_query": search_query, "count": len(events), "events": events, "calendar_events_added": calendar_added,
            "note": "Search results are leads. Verify material claims against official filings or primary sources before using them in a decision."}


@app.get("/api/web/status")
def web_research_status() -> dict[str, Any]:
    return WEB_RESEARCH.health()


@app.get("/api/web/fetch")
def web_fetch(url: str = Query(min_length=12, max_length=2000), max_content_chars: int = Query(default=12000, ge=1000, le=30000)) -> dict[str, Any]:
    try:
        return WEB_RESEARCH.fetch_url(url, max_content_chars=max_content_chars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WebResearchError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/news/research")
def news_research(question: str = Query(min_length=5, max_length=1000), depth: str = Query(default="quick", pattern="^(quick|standard|comprehensive)$"), max_sources: int = Query(default=12, ge=1, le=50)) -> dict[str, Any]:
    try:
        return WEB_RESEARCH.research(question, depth=depth, max_sources=max_sources)
    except WebResearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/news/stock/{symbol}")
def stock_news(symbol: str, max_results: int = Query(default=10, ge=1, le=25), persist: bool = True) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    instrument = next((m for m in DEFAULT_NIFTY50_REGISTRY.members("NIFTY 50") if m.symbol == normalized), None)
    company = instrument.company_name if instrument else normalized
    query = f'"{company}" OR {normalized} stock news'
    search_query = build_recent_news_query(query, days=30)
    try:
        events = [score_news_item(event) for event in WEB_RESEARCH.search(search_query, max_results=max_results)]
    except WebResearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Add matching IndianAPI headlines when a key is configured (best-effort; same scoring/storage path).
    try:
        feed = IndianApiNewsClient()
        if feed.configured():
            seen = {e.get("source_url") for e in events}
            needles = {company.casefold(), normalized.casefold()}
            for item in feed.fetch():
                text = f"{item['title']} {item['summary']}".casefold()
                if item["source_url"] not in seen and any(n in text for n in needles):
                    events.append(score_news_item(item))
                    seen.add(item["source_url"])
    except Exception:  # noqa: BLE001 - optional provider must not break stock news
        pass
    for event in events:
        event["symbol"] = normalized
        if persist:
            STORE.save_event(event)
    calendar_added = _calendar_ingest(events, provider="news:stock", symbol=normalized) if persist else 0
    return {"symbol": normalized, "company": company, "query": query, "search_query": search_query, "count": len(events), "events": events, "calendar_events_added": calendar_added,
            "note": "Search leads must be verified against official filings and primary sources before use."}


@app.get("/api/news/events")
def news_events(
    symbol: str | None = None, limit: int = Query(default=100, ge=1, le=500),
    source_type: str = Query(default="all", pattern="^(all|news|official|other)$"),
    quality: str = Query(default="good", pattern="^(good|all)$"),
    days: int = Query(default=30, ge=7, le=90), include_undated: bool = False,
    event_type: str = "all", direction: str = Query(default="all", pattern="^(all|positive|negative|uncertain)$"),
    text: str = Query(default="", max_length=200), include_hidden: bool = False,
) -> dict[str, Any]:
    if days not in (7, 30, 90):
        raise HTTPException(status_code=422, detail="days must be one of 7, 30, or 90")
    normalized = _known_symbol(symbol) if symbol else None
    # Read hidden rows too so the UI can report a true saved count without deleting history.
    stored = STORE.list_events(normalized, 500, include_hidden=True)
    visible = filter_news_items(stored, source_type=source_type, quality=quality, days=days,
        include_undated=include_undated, event_type=event_type, direction=direction,
        text=text, include_hidden=include_hidden)
    return {"count": len(visible[:limit]), "saved_count": len(stored),
            "hidden_count": sum(1 for row in stored if row.get("hidden")),
            "shown_count": min(len(visible), limit), "filters": {"source_type": source_type,
            "quality": quality, "days": days, "include_undated": include_undated,
            "event_type": event_type, "direction": direction, "text": text,
            "include_hidden": include_hidden}, "events": visible[:limit],
            "quality_config": {"good_score_min": NEWS_QUALITY_CONFIG["good_score_min"]}}


@app.post("/api/news/events/{event_id}/hide")
def hide_news_event(event_id: str, hidden: bool = True) -> dict[str, Any]:
    if not STORE.hide_event(event_id, hidden=hidden):
        raise HTTPException(status_code=404, detail="Saved event not found")
    return {"event_id": event_id, "hidden": hidden, "deleted": False}


@app.post("/api/news/events/cleanup")
def cleanup_news_events(min_score: int = Query(default=NEWS_QUALITY_CONFIG["good_score_min"], ge=0, le=100)) -> dict[str, Any]:
    hidden_count = STORE.hide_events_below_score(min_score)
    return {"hidden_count": hidden_count, "minimum_score": min_score,
            "deleted": False, "message": "Low-quality events were hidden; source records were retained."}


@app.get("/api/news/feed")
@traced("india.api.news_feed")
def news_feed(refresh: bool = False, quality: str = Query(default="good", pattern="^(good|all)$"),
              limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """Latest IndianAPI headlines (key from .env). Saved as events and scanned for dated calendar events."""
    client = IndianApiNewsClient()
    if not client.configured():
        raise HTTPException(status_code=503, detail="IndianAPI is not configured. Add INDIANAPI_API_KEY to .env.")
    try:
        items = client.fetch(force=refresh)
    except ecal.CalendarSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    scored = [score_news_item(item) for item in items]
    for item in scored:
        STORE.save_event(item)
    calendar_added = _calendar_ingest(scored, provider="news:indianapi")
    shown = [i for i in scored if quality == "all" or i["quality"] == "good"]
    shown.sort(key=lambda i: i.get("published_at") or "", reverse=True)
    return {"count": len(shown[:limit]), "fetched": len(scored), "calendar_events_added": calendar_added,
            "events": shown[:limit], "note": "Headlines are leads; dated events found in them are shown on the calendar as unverified."}


@app.get("/api/calendar/events")
def calendar_events(month: str = Query(default="", pattern=r"^(\d{4}-\d{2})?$"),
                    asset: str = Query(default="all", pattern="^(all|nse|crypto|NSE|CRYPTO)$"),
                    category: str = Query(default="all", max_length=40), symbol: str | None = Query(default=None, max_length=30)) -> dict[str, Any]:
    month = month or datetime.now(INDIA_TZ).strftime("%Y-%m")
    try:
        start, end = ecal.month_bounds(month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc
    if category != "all" and category not in ecal.CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown category")
    events = ecal.query_events(STORE, start, end, asset=asset, category=category, symbol=symbol)
    return {"month": month, "start": start.isoformat(), "end": end.isoformat(), "count": len(events), "events": events,
            "categories": ecal.CATEGORIES, "last_refresh": STORE.get_calendar_meta("last_refresh"),
            "today": datetime.now(INDIA_TZ).date().isoformat()}


@app.get("/api/calendar/upcoming")
def calendar_upcoming(days: int = Query(default=14, ge=1, le=120), asset: str = Query(default="all", pattern="^(all|nse|crypto|NSE|CRYPTO)$")) -> dict[str, Any]:
    today = datetime.now(INDIA_TZ).date()
    events = ecal.query_events(STORE, today, today + timedelta(days=days), asset=asset)
    return {"from": today.isoformat(), "days": days, "count": len(events), "events": events, "categories": ecal.CATEGORIES}


@app.get("/api/calendar/sources")
def calendar_sources() -> dict[str, Any]:
    return {"sources": ecal.source_status(STORE), "last_refresh": STORE.get_calendar_meta("last_refresh")}


@app.post("/api/calendar/refresh")
def calendar_refresh(sources: list[str] | None = Query(default=None)) -> dict[str, Any]:
    if sources and any(s not in ecal.DEFAULT_SOURCES for s in sources):
        raise HTTPException(status_code=422, detail=f"sources must be a subset of {list(ecal.DEFAULT_SOURCES)}")
    if not _CALENDAR_REFRESH_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A calendar refresh is already running.")
    try:
        report = ecal.refresh_calendar(STORE, sources)
        google = gcal.auto_sync_after_refresh(STORE)
        if google is not None:
            report["google_calendar"] = google
        return {"report": report, "last_refresh": STORE.get_calendar_meta("last_refresh")}
    finally:
        _CALENDAR_REFRESH_LOCK.release()


@app.post("/api/calendar/events")
def calendar_add_event(body: CalendarEventRequest) -> dict[str, Any]:
    if body.category not in ecal.CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of {list(ecal.CATEGORIES)}")
    if any(a not in ecal.ASSET_SCOPES for a in body.assets):
        raise HTTPException(status_code=422, detail="assets must be NSE and/or CRYPTO")
    if body.end_date and body.end_date < body.date:
        raise HTTPException(status_code=422, detail="end_date must not be before date")
    if body.source_url and not body.source_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="source_url must be an http(s) URL")
    event = ecal.make_event(day=body.date, end_day=body.end_date, category=body.category, title=body.title, assets=body.assets,
                            symbol=body.symbol, description=body.description, source_url=body.source_url, provider="manual",
                            source_name="Added by you", verified=False, status="manual", importance="medium")
    created = STORE.upsert_calendar_event(event)
    return {"created": created, "event": event}


def _gcal_redirect_uri(request: Request) -> str:
    return f"http://127.0.0.1:{request.url.port or int(os.getenv('PORT', '8000'))}/api/gcal/oauth/callback"


@app.get("/api/gcal/status")
def gcal_status(request: Request) -> dict[str, Any]:
    client = gcal.GoogleCalendarClient(STORE)
    last = STORE.get_calendar_meta("gcal_last_sync")
    return {"configured": client.configured(), "connected": client.configured() and client.connected(),
            "calendar_id_set": bool(STORE.get_calendar_meta("gcal_calendar_id")), "settings": gcal.load_settings(STORE),
            "last_sync": json.loads(last) if last else None, "redirect_uri": _gcal_redirect_uri(request),
            "categories": ecal.CATEGORIES, "calendar_name": gcal.CALENDAR_NAME,
            "on_this_computer": (request.url.hostname or "") in ("127.0.0.1", "localhost", "::1")}


@app.post("/api/gcal/connect")
def gcal_connect(request: Request) -> dict[str, Any]:
    try:
        return {"auth_url": gcal.GoogleCalendarClient(STORE).auth_url(_gcal_redirect_uri(request))}
    except gcal.GoogleCalendarError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/gcal/oauth/callback", response_class=HTMLResponse)
def gcal_oauth_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    import html as _html
    if error or not code or not state:
        message, ok = f"Google did not grant access ({_html.escape(error or 'missing code')}).", False
    else:
        try:
            gcal.GoogleCalendarClient(STORE).finish_auth(code, state)
            message, ok = "Google Calendar connected. You can close this tab and return to the dashboard.", True
        except gcal.GoogleCalendarError as exc:
            message, ok = _html.escape(str(exc)), False
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Google Calendar</title><body style='font:16px system-ui;max-width:520px;margin:12vh auto;padding:0 20px'>"
                        f"<h2>{'✅ Connected' if ok else '⚠️ Not connected'}</h2><p>{message}</p></body>", status_code=200 if ok else 400)


@app.put("/api/gcal/settings")
def gcal_save_settings(body: GcalSettingsRequest) -> dict[str, Any]:
    try:
        merged = gcal.validate_settings({**gcal.load_settings(STORE), **body.model_dump(exclude_none=True)})
    except gcal.GoogleCalendarError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    STORE.set_calendar_meta("gcal_settings", json.dumps(merged))
    return {"settings": merged}


@app.post("/api/gcal/sync")
def gcal_sync_now() -> dict[str, Any]:
    client = gcal.GoogleCalendarClient(STORE)
    if not (client.configured() and client.connected()):
        raise HTTPException(status_code=409, detail="Google Calendar is not connected.")
    if not gcal.SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A Google Calendar sync is already running.")
    try:
        return {"report": client.sync_from_store()}
    except gcal.GoogleCalendarError as exc:
        raise HTTPException(status_code=502, detail=ecal.scrub(str(exc))) from exc
    finally:
        gcal.SYNC_LOCK.release()


@app.post("/api/gcal/disconnect")
def gcal_disconnect(delete_calendar: bool = False) -> dict[str, Any]:
    client = gcal.GoogleCalendarClient(STORE)
    deleted = False
    if delete_calendar and client.connected():
        try:
            client.delete_calendar()
            deleted = True
        except gcal.GoogleCalendarError as exc:
            raise HTTPException(status_code=502, detail=ecal.scrub(str(exc))) from exc
    client.forget_token()
    STORE.gcal_sync_clear()
    return {"connected": False, "calendar_deleted": deleted}


@app.post("/api/calendar/events/{event_id}/hide")
def calendar_hide_event(event_id: str, hidden: bool = True) -> dict[str, Any]:
    if not STORE.hide_calendar_event(event_id, hidden=hidden):
        raise HTTPException(status_code=404, detail="Calendar event not found")
    return {"event_id": event_id, "hidden": hidden, "deleted": False}


@app.get("/api/fundamentals/source-guidance")
def fundamentals_sources() -> dict[str, Any]:
    return source_guidance()


# Cached research endpoints for the Fundamentals and Market Movers workspaces.
_SCREEN_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_SCREEN_LOCK = threading.Lock()
_MOVERS_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_MOVERS_LOCK = threading.Lock()


def _nifty500_members() -> list[dict[str, str]]:
    path = ROOT / "tradingagents" / "india" / "data" / "nifty_500_constituents.csv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(stream)]


@app.get("/api/fundamental-screen")
def fundamental_screen(universe: str = Query(default="NIFTY 500"), refresh: bool = Query(default=False)) -> dict[str, Any]:
    """Explainable screen over available Nifty 500 financial evidence; missing data never scores as healthy."""
    now = time.time()
    with _SCREEN_LOCK:
        cached = _SCREEN_CACHE.get("payload")
        if cached and not refresh and now - float(_SCREEN_CACHE.get("at", 0)) < 600:
            return {**cached, "cached": True}
    members = _nifty500_members()
    if not members:
        members = [{"symbol": s, "company_name": s, "sector": "", "industry": ""} for s in _fundamentals_universe()[1]]
    try:
        all_facts = STORE.list_facts(limit=200000)
    except Exception:
        all_facts = []
    facts_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for fact in all_facts:
        sym = str(fact.get("symbol") or "").upper()
        if sym:
            facts_by_symbol.setdefault(sym, []).append(fact)
    rows: list[dict[str, Any]] = []
    for member in members:
        symbol = (member.get("symbol") or "").upper()
        if not symbol:
            continue
        facts = facts_by_symbol.get(symbol, [])
        annual = build_statement_view(facts, "annual")
        quarterly = build_statement_view(facts, "quarterly")
        is_financial = any(t in f"{member.get('sector','')} {member.get('industry','')}".lower()
                           for t in ("financial", "bank", "insurance", "capital market", "lending"))
        flags = evaluate_financial_flags({**annual, "symbol": symbol}, {**quarterly, "symbol": symbol}, is_financial=is_financial)
        reasons = [f.get("title", "Positive financial trend") for f in (flags.get("green_flags") or [])]
        reasons.extend("Check: " + f.get("title", "Financial risk") for f in (flags.get("red_flags") or []))
        green_count, red_count = len(flags.get("green_flags") or []), len(flags.get("red_flags") or [])
        score = min(100, max(0, 50 + 10 * green_count - 15 * red_count))
        snapshots = [f for f in facts if f.get("period_type") == "snapshot"]
        snapshots.sort(key=lambda f: str(f.get("retrieved_at") or f.get("published_at") or ""), reverse=True)
        snap = snapshots[0] if snapshots else {}
        snapshot_at = snap.get("retrieved_at") or snap.get("published_at")
        try:
            stamp = datetime.fromisoformat(str(snapshot_at).replace("Z", "+00:00")) if snapshot_at else None
            fresh = bool(stamp and stamp.tzinfo and datetime.now(timezone.utc) - stamp.astimezone(timezone.utc) <= timedelta(days=30))
        except ValueError:
            fresh = False
        metrics = {}
        if fresh:
            for code in ("PE_RATIO", "PB_RATIO"):
                found = next((f for f in snapshots if f.get("metric_code") == code), None)
                try: metrics[code] = float(found["value"]) if found else None
                except (TypeError, ValueError, KeyError): metrics[code] = None
        pe, pb = metrics.get("PE_RATIO"), metrics.get("PB_RATIO")
        if pe is not None and 0 < pe <= 25:
            score += 5; reasons.append("P/E at or below 25× (context only)")
        if pb is not None and 0 < pb <= 3:
            score += 3; reasons.append("P/B at or below 3× (context only)")
        has_evidence = bool(facts)
        if not has_evidence:
            status_label, shown_score = "Insufficient data", None
            reasons = ["No financial evidence stored; refresh fundamentals"]
        elif red_count and red_count >= green_count:
            status_label, shown_score = "Needs review", min(score, 49)
        elif score >= 65 and red_count == 0 and green_count > 0:
            status_label, shown_score = "Strong candidate", min(score, 100)
        else:
            status_label, shown_score = "Needs review", min(score, 64)
        as_of = snapshot_at or annual.get("retrieved_at") or quarterly.get("retrieved_at")
        rows.append({"symbol": symbol, "company_name": member.get("company_name") or symbol,
                     "sector": member.get("sector") or member.get("industry") or "", "industry": member.get("industry") or "",
                     "status": status_label, "score": shown_score, "pe": pe, "pb": pb,
                     "reasons": reasons[:5], "as_of": as_of, "green_flags": green_count, "red_flags": red_count})
    rows.sort(key=lambda r: (r["status"] != "Strong candidate", -(r["score"] if r["score"] is not None else -1), r["symbol"]))
    payload = {"universe": "NIFTY 500", "source": "Nifty 500 constituent CSV + stored Yahoo/provider statements and valuation snapshots",
               "generated_at": datetime.now(INDIA_TZ).isoformat(), "rows": rows,
               "coverage": {"total": len(rows), "screened": sum(r["status"] != "Insufficient data" for r in rows),
                            "strong": sum(r["status"] == "Strong candidate" for r in rows),
                            "missing": sum(r["status"] == "Insufficient data" for r in rows)}}
    with _SCREEN_LOCK:
        _SCREEN_CACHE.update(at=time.time(), payload=payload)
    return {**payload, "cached": False}

@app.get("/api/market/price-history/{symbol}")
def market_price_history(symbol: str, period: str = Query(default="6mo", pattern="^(1mo|3mo|6mo|1y)$")) -> dict[str, Any]:
    """Return provider-sourced daily OHLCV for the selected NSE equity chart."""
    normalized = _known_symbol(symbol)
    try:
        import yfinance as yf
        frame = yf.download(tickers=f"{normalized}.NS", period=period, interval="1d", auto_adjust=False,
                            progress=False, threads=False, timeout=15)
        if frame is None or frame.empty:
            raise ValueError("provider returned no daily candles")
        # yfinance may return a MultiIndex even for one ticker, depending on version.
        if getattr(frame.columns, "nlevels", 1) > 1:
            frame.columns = frame.columns.get_level_values(0)
        rows: list[dict[str, Any]] = []
        for stamp, row in frame.iterrows():
            try:
                values = {key: float(row[key]) for key in ("Open", "High", "Low", "Close", "Volume")}
                if not all(__import__("math").isfinite(v) for v in values.values()) or values["Close"] <= 0:
                    continue
                rows.append({"date": stamp.date().isoformat() if hasattr(stamp, "date") else str(stamp)[:10],
                             "open": values["Open"], "high": values["High"], "low": values["Low"],
                             "close": values["Close"], "volume": max(0, values["Volume"])})
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if not rows:
            raise ValueError("provider returned no usable daily candles")
        latest = rows[-1]
        previous = rows[-2] if len(rows) > 1 else None
        change = ((latest["close"] / previous["close"]) - 1) * 100 if previous and previous["close"] else None
        return {"symbol": normalized, "period": period, "rows": rows, "latest": latest,
                "previous_close": previous["close"] if previous else None, "change_pct": change,
                "source": "Yahoo Finance daily bars; delayed/coverage-dependent",
                "generated_at": datetime.now(INDIA_TZ).isoformat()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Price history unavailable for {normalized}: {type(exc).__name__}: {str(exc)[:160]}") from exc


@app.get("/api/market/movers")
def market_movers(universe: str = Query(default="NIFTY 500"), refresh: bool = Query(default=False)) -> dict[str, Any]:
    """Daily Nifty 500 movers from Yahoo Finance daily bars; not a real-time quote feed."""
    now = time.time()
    with _MOVERS_LOCK:
        cached = _MOVERS_CACHE.get("payload")
        if cached and not refresh and now - float(_MOVERS_CACHE.get("at", 0)) < 300:
            return {**cached, "cached": True}
    members = _nifty500_members()
    if not members:
        raise HTTPException(status_code=503, detail="Nifty 500 constituent snapshot is unavailable")
    try:
        import yfinance as yf
        tickers = [f"{m['symbol']}.NS" for m in members if m.get("symbol")]
        frame = yf.download(tickers=tickers, period="1mo", interval="1d", group_by="ticker", auto_adjust=False,
                            progress=False, threads=True, timeout=20)
        if frame is None or frame.empty:
            raise ValueError("provider returned no daily bars")
        rows = []
        for member in members:
            symbol = (member.get("symbol") or "").upper()
            ticker = f"{symbol}.NS"
            try:
                part = frame[ticker] if isinstance(frame.columns, __import__('pandas').MultiIndex) else frame
                part = part.dropna(subset=["Close"])
                if len(part) < 1:
                    continue
                part = part.dropna(subset=["Close", "High", "Low"]).tail(22)
                if part.empty:
                    continue
                latest = part.iloc[-1]
                previous = part.iloc[-2] if len(part) > 1 else None
                close = float(latest["Close"])
                prev_close = float(previous["Close"]) if previous is not None else None
                change = ((close / prev_close) - 1) * 100 if prev_close else 0.0
                volume = int(latest.get("Volume", 0) or 0)
                traded_value = close * volume
                row_date = part.index[-1].date().isoformat()
                closes = [float(v) for v in part["Close"].tolist()]
                highs = [float(v) for v in part["High"].tolist()]
                lows = [float(v) for v in part["Low"].tolist()]
                volumes = [max(0, int(v or 0)) for v in part["Volume"].tolist()]
                def ret_over_sessions(n: int) -> float | None:
                    if len(closes) <= n or not closes[-n-1]:
                        return None
                    return (closes[-1] / closes[-n-1] - 1) * 100
                consecutive_down = 0
                for idx in range(len(closes)-1, 0, -1):
                    if closes[idx] < closes[idx-1]: consecutive_down += 1
                    else: break
                def broke_high(n: int) -> bool:
                    return len(highs) > n and closes[-1] > max(highs[-n-1:-1])
                def broke_low(n: int) -> bool:
                    return len(lows) > n and closes[-1] < min(lows[-n-1:-1])
                prior_volumes = volumes[-6:-1] if len(volumes) > 1 else []
                avg_prior_volume = (sum(prior_volumes) / len(prior_volumes)) if prior_volumes else None
                rows.append({"symbol": symbol, "company_name": member.get("company_name") or symbol,
                             "sector": member.get("sector") or None, "industry": member.get("industry") or None,
                             "market_cap": member.get("market_cap") if isinstance(member.get("market_cap"), (int, float)) else None,
                             "close": close, "previous_close": prev_close, "change_pct": change,
                             "return_2d_pct": ret_over_sessions(2), "return_3d_pct": ret_over_sessions(3),
                             "consecutive_down_days": consecutive_down,
                             "avg_volume_5d": avg_prior_volume,
                             "volume_ratio_5d": (volume / avg_prior_volume) if avg_prior_volume and avg_prior_volume > 0 else None,
                             "break_1d_high": broke_high(1), "break_1d_low": broke_low(1),
                             "break_2d_high": broke_high(2), "break_2d_low": broke_low(2),
                             "break_3d_high": broke_high(3), "break_3d_low": broke_low(3),
                             "break_weekly_high": broke_high(5), "break_weekly_low": broke_low(5),
                             "week_high": max(highs[-5:]) if highs else None, "week_low": min(lows[-5:]) if lows else None,
                             "volume": volume, "traded_value": traded_value, "date": row_date})
            except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                continue
        if not rows:
            raise ValueError("no usable ticker bars returned")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Daily market data could not be loaded: {type(exc).__name__}: {str(exc)[:180]}") from exc
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    dates = [r["date"] for r in rows if r.get("date")]
    market_date = max(set(dates), key=dates.count) if dates else None
    payload = {"universe": "NIFTY 500", "market_date": market_date, "source": "Yahoo Finance daily bars; delayed/coverage-dependent; traded value estimated as close × volume",
               "rows": rows, "gainers": rows[:10], "losers": sorted(rows, key=lambda r: r["change_pct"])[:10],
               "highest_volume": sorted(rows, key=lambda r: r["volume"], reverse=True)[:10],
               "consistent_losers": sorted([r for r in rows if r.get("consecutive_down_days", 0) >= 2], key=lambda r: (r.get("consecutive_down_days", 0), -(r.get("return_3d_pct") or 0)), reverse=True)[:25],
               "three_day_winners": sorted([r for r in rows if r.get("return_3d_pct") is not None and r["return_3d_pct"] > 0], key=lambda r: r["return_3d_pct"], reverse=True)[:25],
               "high_volume_3d": sorted([r for r in rows if r.get("volume_ratio_5d") is not None], key=lambda r: r["volume_ratio_5d"], reverse=True)[:25],
               "breakouts": {key: sorted([r for r in rows if r.get(key)], key=lambda r: abs(r.get("change_pct", 0)), reverse=True)[:50] for key in ("break_1d_high", "break_1d_low", "break_2d_high", "break_2d_low", "break_3d_high", "break_3d_low", "break_weekly_high", "break_weekly_low")},
               "breadth": {"advancers": sum(r["change_pct"] > 0 for r in rows),
                           "decliners": sum(r["change_pct"] < 0 for r in rows),
                           "unchanged": sum(r["change_pct"] == 0 for r in rows)},
               "generated_at": datetime.now(INDIA_TZ).isoformat()}
    with _MOVERS_LOCK:
        _MOVERS_CACHE.update(at=time.time(), payload=payload)
    return {**payload, "cached": False}



@app.get("/api/fundamentals/{symbol}")
def fundamentals(symbol: str, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    return {"symbol": normalized, "facts": STORE.list_facts(normalized, limit), "source_guidance": source_guidance(),
            "last_refreshed": last_refreshed(STORE, normalized),
            "note": "Numeric facts appear only after they are imported from a sourced CSV. No values are fabricated or inferred."}


@app.post("/api/fundamentals/{symbol}/refresh-yahoo")
def fundamentals_refresh_yahoo(symbol: str) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    try:
        return refresh_yahoo_snapshot(normalized, STORE)
    except FundamentalDataError as exc:
        message = str(exc)
        status_code = 503 if "not installed" in message else (502 if "request failed" in message or "returned no snapshot" in message else 422)
        raise HTTPException(status_code=status_code, detail=message) from exc


@app.post("/api/fundamentals/{symbol}/refresh-statements")
def fundamentals_refresh_statements(symbol: str, years: int = Query(default=5, ge=1, le=10)) -> dict[str, Any]:
    from tradingagents.india.statements import StatementDataError, refresh_yahoo_statements

    normalized = _known_symbol(symbol)
    try:
        return refresh_yahoo_statements(normalized, STORE, years=years)
    except StatementDataError as exc:
        message = str(exc)
        raise HTTPException(status_code=503 if "not installed" in message else 502, detail=message) from exc


@app.post("/api/fundamentals/{symbol}/refresh")
def fundamentals_refresh(symbol: str) -> dict[str, Any]:
    """Refresh Yahoo statements (required) and the P/E-style snapshot (best effort) for one symbol."""
    from tradingagents.india.statements import StatementDataError, refresh_yahoo_statements

    normalized = _known_symbol(symbol)
    try:
        statements = refresh_yahoo_statements(normalized, STORE)
    except StatementDataError as exc:
        message = str(exc)
        raise HTTPException(status_code=503 if "not installed" in message else 502, detail=message) from exc
    snapshot: dict[str, Any] | None = None
    snapshot_error: str | None = None
    try:
        snapshot = refresh_yahoo_snapshot(normalized, STORE)
    except FundamentalDataError as exc:
        snapshot_error = str(exc)
    periods = {k: v["periods"] for k, v in statements["coverage"].items() if k.startswith("income_stmt")}
    return {"symbol": normalized, "refreshed_at": statements["retrieved_at"], "facts_stored": statements["facts_stored"],
            "income_statement_periods": periods, "snapshot_metrics": snapshot["inserted"] if snapshot else 0,
            "snapshot_error": snapshot_error, "last_refreshed": last_refreshed(STORE, normalized)}


@app.get("/api/fundamentals/{symbol}/statements")
def fundamentals_statements(symbol: str, period_type: str = Query(default="annual", pattern="^(annual|quarterly)$")) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    return {**statement_view(STORE, normalized, period_type), "last_refreshed": last_refreshed(STORE, normalized)}


@app.get("/api/fundamentals/{symbol}/valuation")
def fundamentals_valuation(symbol: str) -> dict[str, Any]:
    """Current stock P/E and peer medians from stored Nifty 500 snapshots."""
    normalized = _known_symbol(symbol)
    # The universe needs several snapshot metrics per symbol; load beyond the UI's normal fact limit.
    rows = STORE.list_facts(limit=20000)
    return build_valuation_summary(normalized, rows)


@app.get("/api/fundamentals/{symbol}/flags")
def fundamentals_flags(symbol: str) -> dict[str, Any]:
    """Return transparent rule-based health flags from stored annual and quarterly statements."""
    normalized = _known_symbol(symbol)
    annual = statement_view(STORE, normalized, "annual")
    quarterly = statement_view(STORE, normalized, "quarterly")
    # Treat financial-services constituents as a separate rule family; never apply
    # industrial debt heuristics to banks/lenders.
    is_financial = False
    try:
        import csv
        universe_path = ROOT / "data" / "nifty_500_constituents.csv"
        if universe_path.is_file():
            with universe_path.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    if (row.get("symbol") or "").strip().upper() == normalized:
                        label = " ".join((row.get("industry") or "", row.get("sector") or "")).lower()
                        is_financial = any(term in label for term in ("financial", "bank", "insurance", "capital market", "lending"))
                        break
    except (OSError, ValueError):
        is_financial = False
    result = evaluate_financial_flags({**annual, "symbol": normalized}, {**quarterly, "symbol": normalized}, is_financial=is_financial)
    # Valuation context is deliberately neutral: a low P/E can indicate either value or distress.
    valuation = build_valuation_summary(normalized, STORE.list_facts(limit=20000))
    if valuation.get("percent_vs_peers") is not None:
        pct = valuation["percent_vs_peers"]
        direction = "below" if pct < 0 else "above"
        result.setdefault("red_flags", []).append({
            "severity": "warning", "title": "Valuation relative to peers",
            "detail": f"Trailing P/E is {abs(pct):.1f}% {direction} the {valuation.get('peer_group')} median "
                      f"({valuation.get('stock', {}).get('trailing_pe')} vs {valuation.get('peer_median')}; "
                      f"{valuation.get('peer_count')} peers). Relative valuation alone does not establish mispricing.",
            "periods": [str(valuation.get("stock", {}).get("fetched_at") or "snapshot date unavailable")],
        })
    result["valuation"] = valuation
    return result


_FUND_PROGRESS_PATH = ROOT / "data" / "fundamentals_bulk_progress.jsonl"
_FUND_COVERAGE_PATH = ROOT / "data" / "fundamentals_coverage.csv"
_FUND_JOB_LOCK = threading.Lock()
_FUND_JOB: dict[str, Any] = {"status": "idle"}


def _fundamentals_universe() -> tuple[str, list[str]]:
    csv_path = ROOT / "tradingagents" / "india" / "data" / "nifty_500_constituents.csv"
    if csv_path.is_file():
        registry = StockUniverseRegistry.from_csv(csv_path)
        return "NIFTY 500", list(registry.symbols("NIFTY 500"))
    return "NIFTY 50", list(DEFAULT_NIFTY50_REGISTRY.symbols("NIFTY 50"))


def _run_fundamentals_job(index_name: str, symbols: list[str], force: bool) -> None:
    def progress(i: int, total: int, symbol: str, outcome: str) -> None:
        with _FUND_JOB_LOCK:
            _FUND_JOB.update(processed=i, total=total, current=symbol)
            _FUND_JOB[{"ok": "done", "failed": "failed", "skipped": "skipped"}[outcome]] += 1

    try:
        summary = run_bulk(symbols, STORE, progress_path=_FUND_PROGRESS_PATH, force=force,
                           refresh_after_days=7.0, with_snapshot=True, log=lambda _m: None, on_progress=progress)
        try:
            build_coverage(_FUND_PROGRESS_PATH, out_csv=_FUND_COVERAGE_PATH)
        except OSError:
            pass
        with _FUND_JOB_LOCK:
            _FUND_JOB.update(status="halted" if summary["halted"] else "finished",
                             finished_at=datetime.now(timezone.utc).isoformat(), current=None)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the dashboard
        with _FUND_JOB_LOCK:
            _FUND_JOB.update(status="failed", finished_at=datetime.now(timezone.utc).isoformat(), current=None,
                             error=f"{type(exc).__name__}: {str(exc)[:200]}")


@app.post("/api/fundamentals/refresh-all")
def fundamentals_refresh_all(force: bool = Query(default=False)) -> dict[str, Any]:
    index_name, symbols = _fundamentals_universe()
    with _FUND_JOB_LOCK:
        if _FUND_JOB.get("status") == "running":
            raise HTTPException(status_code=409, detail="A fundamentals refresh is already running")
        _FUND_JOB.clear()
        _FUND_JOB.update(status="running", index=index_name, total=len(symbols), processed=0, done=0, failed=0,
                         skipped=0, current=None, force=force, started_at=datetime.now(timezone.utc).isoformat())
        snapshot = dict(_FUND_JOB)
    threading.Thread(target=_run_fundamentals_job, args=(index_name, symbols, force), daemon=True,
                     name="fundamentals-refresh-all").start()
    return snapshot


@app.get("/api/fundamentals/refresh-all/status")
def fundamentals_refresh_all_status() -> dict[str, Any]:
    index_name, symbols = _fundamentals_universe()
    with _FUND_JOB_LOCK:
        job = dict(_FUND_JOB)
    return {"job": job, "index": index_name,
            "universe": progress_summary(_FUND_PROGRESS_PATH, symbols)}


@app.get("/api/fundamentals/{symbol}/summary")
def fundamentals_summary(symbol: str, as_of: datetime | None = None, limit: int = Query(default=500, ge=1, le=2000)) -> dict[str, Any]:
    normalized = _known_symbol(symbol)
    cutoff = as_of or datetime.now(INDIA_TZ)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise HTTPException(status_code=422, detail="as_of must include a timezone offset")
    try:
        return {"symbol": normalized, **summarize_facts(STORE.list_facts(normalized, limit), as_of=cutoff)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/fundamentals/xbrl/parse")
def fundamentals_parse_xbrl(request: XbrlParseRequest) -> dict[str, Any]:
    normalized = _known_symbol(request.symbol)
    try:
        return parse_xbrl_facts(request.xml_text, symbol=normalized, source_url=request.source_url,
                                published_at=request.published_at, scope=request.scope)
    except FundamentalDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/fundamentals/moneycontrol/fetch")
def fundamentals_moneycontrol_fetch(url: str = Query(min_length=12, max_length=2000)) -> dict[str, Any]:
    try:
        return fetch_statement_tables(url)
    except MoneycontrolPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MoneycontrolFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/fundamentals/import-csv")
def fundamentals_import_csv(csv_text: str = Body(media_type="text/csv")) -> dict[str, Any]:
    try:
        result = import_facts_csv(csv_text, STORE)
    except FundamentalDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@app.post("/api/live/orders")
def live_place_order(request: LiveOrderRequest) -> dict[str, Any]:
    normalized = _known_symbol(request.symbol)
    client = _fyers_order_client(request.confirm_live_order)
    try:
        return client.place_limit_order(symbol=normalized, side=request.side, quantity=request.quantity, limit_price=request.limit_price)
    except (ValueError, FyersOrderError) as exc:
        raise HTTPException(status_code=422 if isinstance(exc, ValueError) else 502, detail=str(exc)) from exc


@app.patch("/api/live/orders/{order_id}")
def live_modify_order(order_id: str, request: LiveModifyRequest) -> dict[str, Any]:
    client = _fyers_order_client(request.confirm_live_order)
    try:
        return client.modify_limit_order(order_id=order_id, quantity=request.quantity, limit_price=request.limit_price)
    except (ValueError, FyersOrderError) as exc:
        raise HTTPException(status_code=422 if isinstance(exc, ValueError) else 502, detail=str(exc)) from exc


@app.delete("/api/live/orders/{order_id}")
def live_cancel_order(order_id: str, request: LiveCancelRequest) -> dict[str, Any]:
    client = _fyers_order_client(request.confirm_live_order)
    try:
        return client.cancel_order(order_id=order_id)
    except (ValueError, FyersOrderError) as exc:
        raise HTTPException(status_code=422 if isinstance(exc, ValueError) else 502, detail=str(exc)) from exc


@app.get("/api/live/orders")
def live_orderbook(confirm_live_order: str = Query(min_length=1, max_length=40)) -> dict[str, Any]:
    client = _fyers_order_client(confirm_live_order)
    try:
        return client.orderbook()
    except FyersOrderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/paper/orders")
def paper_order(request: PaperOrderRequest) -> dict[str, Any]:
    normalized = _known_symbol(request.symbol)
    try:
        return PAPER_BROKER.place_order(symbol=normalized, side=request.side, quantity=request.quantity,
                                        reference_price=request.reference_price, stop_loss=request.stop_loss,
                                        target=request.target, note=request.note)
    except PaperTradingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/paper/orders")
def paper_orders(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    orders = STORE.list_paper_orders(limit)
    return {"mode": "paper", "count": len(orders), "orders": orders, "live_trading_enabled": False}


@app.get("/api/paper/portfolio")
def paper_portfolio() -> dict[str, Any]:
    return PAPER_BROKER.portfolio()
