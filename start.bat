@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Install Python 3.11+ and ensure the Python launcher is available.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -c "import fastapi, uvicorn, langsmith" >nul 2>nul
if errorlevel 1 (
  echo Installing/updating TradingAgents and web dependencies...
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -e .[web]
  if errorlevel 1 exit /b 1
)
if not exist .env if exist .env.example copy .env.example .env >nul
if "%PORT%"=="" set PORT=8000
if "%HOST%"=="" set HOST=127.0.0.1
echo Dashboard: http://%HOST%:%PORT%/
echo API docs:  http://%HOST%:%PORT%/docs
.venv\Scripts\python.exe -m uvicorn tradingagents.web.app:app --host %HOST% --port %PORT%
