#!/usr/bin/env python3
"""Start TradingAgents India and, when possible, its local Wigolo REST daemon.

Wigolo remains a separate process/package (AGPL-3.0); this launcher only starts
`npx wigolo serve` on loopback and talks to its documented REST API. If Wigolo
cannot start, the dashboard still starts and the news adapter can use RSS fallback.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def _get_json(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _is_wigolo_healthy(base_url: str) -> bool:
    payload = _get_json(base_url.rstrip("/") + "/health")
    return bool(payload and str(payload.get("status", "")).lower() in {"healthy", "ok", "ready"})



def _wigolo_failure_hint(log_path: Path) -> str:
    """Return a safe, actionable cause hint without printing raw daemon logs."""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-30000:].lower()
    except OSError:
        return "Inspect data/wigolo.log for the startup error."
    if any(token in tail for token in ("node_module_version", "better-sqlite3", "module did not self-register", "was compiled against a different node.js version")):
        return "Node native-module mismatch detected. Try `npm rebuild better-sqlite3` in the Wigolo package cache; if that fails, remove the stale `~/.npm/_npx` cache and rerun `npx wigolo serve`."
    if "eaddrinuse" in tail or "address already in use" in tail:
        return "Port is already in use; check the existing service and WIGOLO_BASE_URL."
    if "permission denied" in tail or "eacces" in tail:
        return "A filesystem permission error was detected; inspect npm cache permissions."
    if "network" in tail and any(x in tail for x in ("enotfound", "econnreset", "fetch failed")):
        return "A network/package-fetch error was detected; verify connectivity and npm registry access."
    return "No known startup signature detected. Inspect data/wigolo.log; raw log lines are intentionally not echoed."


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=False)
    base_url = os.getenv("WIGOLO_BASE_URL", "http://127.0.0.1:3333").rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    auto_start = os.getenv("WIGOLO_AUTO_START", "true").strip().lower() not in {"0", "false", "no", "off"}
    wigolo_process: subprocess.Popen | None = None
    wigolo_log = None

    if _is_wigolo_healthy(base_url):
        print(f"Wigolo is already available at {base_url}")
    elif auto_start and parsed.hostname in local_hosts:
        npx = shutil.which("npx")
        if not npx:
            print("Wigolo not started: `npx` was not found. News search will use RSS fallback.", flush=True)
        else:
            port = parsed.port or 3333
            env = os.environ.copy()
            # The user's init output confirmed a local Ollama server. Enable it for
            # Wigolo synthesis unless the operator explicitly configured a provider.
            if not env.get("WIGOLO_LLM_PROVIDER"):
                env["WIGOLO_LLM_PROVIDER"] = "ollama"
            log_dir = ROOT / "data"
            log_dir.mkdir(parents=True, exist_ok=True)
            wigolo_log = (log_dir / "wigolo.log").open("ab")
            command = [npx, "--yes", "wigolo", "serve", "--host", "127.0.0.1", "--port", str(port)]
            try:
                wigolo_process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=wigolo_log,
                                                  stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if wigolo_process.poll() is not None:
                        break
                    if _is_wigolo_healthy(base_url):
                        print(f"Started Wigolo at {base_url} (LLM provider: {env.get('WIGOLO_LLM_PROVIDER', 'unset')}); log: data/wigolo.log", flush=True)
                        break
                    time.sleep(0.5)
                else:
                    print("Wigolo did not become healthy within 30s; continuing with RSS fallback. See data/wigolo.log.", flush=True)
                    wigolo_process.terminate()
                    wigolo_process.wait(timeout=5)
                    wigolo_process = None
                if wigolo_process is not None and wigolo_process.poll() is not None:
                    print("Wigolo exited during startup; continuing with RSS fallback. " + _wigolo_failure_hint(log_dir / "wigolo.log"), flush=True)
                    wigolo_process = None
            except OSError as exc:
                print(f"Wigolo could not be started ({type(exc).__name__}); continuing with RSS fallback.", flush=True)
                wigolo_process = None
    else:
        print(f"Wigolo is unavailable at {base_url}; auto-start is disabled or endpoint is remote. RSS fallback will be used.", flush=True)

    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "tradingagents.web.app:app",
                               "--host", args.host, "--port", str(args.port), "--app-dir", str(ROOT)], cwd=ROOT)
    try:
        return server.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        if server.poll() is None:
            server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.terminate()
        if wigolo_process is not None and wigolo_process.poll() is None:
            wigolo_process.terminate()
            try:
                wigolo_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                wigolo_process.kill()
        if wigolo_log is not None:
            wigolo_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
