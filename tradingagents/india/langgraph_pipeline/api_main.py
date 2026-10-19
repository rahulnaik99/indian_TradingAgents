"""ASGI entry point: uvicorn tradingagents.india.langgraph_pipeline.api_main:app"""
from dotenv import load_dotenv

# Load project .env before importing modules that read execution settings.
load_dotenv()

from tradingagents.india.langgraph_pipeline.api import app  # noqa: E402

if app is None:  # defensive for explicit opt-out in environment
    raise RuntimeError("Set TRADING_ANALYSIS_API_AUTOCREATE=1 to create the API app")
