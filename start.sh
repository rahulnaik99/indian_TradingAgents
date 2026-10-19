#!/usr/bin/env bash
set -Eeuo pipefail

# Launches the FastAPI backend and its served frontend in one local process.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: Python 3.11+ is required. Install Python 3.11, 3.12 or 3.13 and retry." >&2
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Creating virtual environment at $VENV_DIR using $PYTHON_BIN …"
  rm -rf "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# Reinstall/update dependencies only when project dependency metadata changes.
DEPS_HASH="$($VENV_PY -c 'import hashlib,pathlib; p=[pathlib.Path("pyproject.toml"),pathlib.Path("requirements.txt")]; h=hashlib.sha256(); [h.update(x.read_bytes()) for x in p if x.exists()]; print(h.hexdigest())')"
HASH_FILE="$VENV_DIR/.tradingagents-deps-hash"
if [[ ! -f "$HASH_FILE" ]] || [[ "$(cat "$HASH_FILE")" != "$DEPS_HASH" ]] || ! "$VENV_PY" -c 'import fastapi, uvicorn, yfinance, dotenv' >/dev/null 2>&1; then
  echo "Installing/updating TradingAgents and web dashboard dependencies …"
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PIP" install -e '.[web]'
  printf '%s' "$DEPS_HASH" > "$HASH_FILE"
else
  echo "Virtual environment and dependencies are present; skipping installation."
fi

if [[ -f .env ]]; then
  # Secret-safe check: reports line number and key only, never the value.
  if ! "$VENV_PY" "$ROOT_DIR/scripts/check_env_syntax.py" "$ROOT_DIR/.env"; then
    echo "WARNING: Fix the .env syntax warning above before relying on affected settings." >&2
  fi
fi

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "Created .env from .env.example. Add your chosen LLM API key and FYERS credentials if needed."
  else
    touch .env
  fi
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo
echo "TradingAgents India is starting…"
echo "Dashboard: http://${HOST}:${PORT}/"
echo "API docs:  http://${HOST}:${PORT}/docs"
echo "Stop with Ctrl+C. Paper orders are simulated; live order placement is disabled."
echo
"$VENV_PY" "$ROOT_DIR/scripts/start_local_stack.py" --host "$HOST" --port "$PORT"
