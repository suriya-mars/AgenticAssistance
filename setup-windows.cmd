@echo off
rem One-time setup on Windows WITHOUT Docker. Needs no admin rights if Python and Ollama were installed per-user.
rem   1. Python 3.11+  from python.org  (choose "Install for me only" and tick "Add python.exe to PATH")
rem   2. Ollama        from ollama.com/download  (installs into your user folder)
rem   3. Run:  setup-windows.cmd
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [x] Python not found. Install Python 3.11+ from python.org: "Install for me only", tick "Add python.exe to PATH".
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo [x] Python 3.11 or newer is required.
  python --version
  exit /b 1
)

if not exist venv\Scripts\python.exe (
  echo Creating the Python environment...
  python -m venv venv
  if errorlevel 1 exit /b 1
)
echo Installing the agent and its libraries...
venv\Scripts\python -m pip install --quiet --upgrade pip
venv\Scripts\python -m pip install --quiet -e .[dev]
if errorlevel 1 (
  echo [x] pip install failed. Behind a company proxy? Run:  set HTTPS_PROXY=http://proxy:port  and try again.
  exit /b 1
)

where ollama >nul 2>&1
if errorlevel 1 (
  echo [x] Ollama not found. Install it from https://ollama.com/download, then run this again.
  exit /b 1
)
ollama list >nul 2>&1
if errorlevel 1 (
  echo [x] Ollama is installed but not running. Start "Ollama" from the Start menu, then run this again.
  exit /b 1
)
rem Models: already-installed ones are not downloaded again.
rem Only the 9B model?   set "MODELS=qwen3.5:9b"   then run this script (everything then uses 9B).
if not defined MODELS set "MODELS=qwen3:4b qwen3.5:9b"
for %%m in (%MODELS%) do (
  ollama show %%m >nul 2>&1
  if errorlevel 1 (
    echo Downloading %%m ...
    ollama pull %%m
    if errorlevel 1 exit /b 1
  ) else (
    echo %%m is already installed.
  )
)

rem Demo PDFs used in the README examples (generated, not stored in git)
venv\Scripts\python samples\make_sample_pdf.py >nul
venv\Scripts\python samples\make_asoc_pdf.py >nul

if not exist .env copy .env.example .env >nul
rem Without qwen3:4b, every role uses qwen3.5:9b (later lines in .env win)
ollama show qwen3:4b >nul 2>&1
if errorlevel 1 (
  findstr /b /c:"AGENT_MODEL_FAST=qwen3.5:9b" .env >nul 2>&1
  if errorlevel 1 (
    echo.>>.env
    echo # Added by setup-windows.cmd: qwen3:4b is not installed, so every role uses qwen3.5:9b>>.env
    echo AGENT_MODEL_FAST=qwen3.5:9b>>.env
    echo AGENT_MODEL_MAIN=qwen3.5:9b>>.env
    echo AGENT_MODEL_QUALITY=qwen3.5:9b>>.env
    echo Only qwen3.5:9b is installed: all roles set to it in .env
  )
)
for %%d in (data files repos) do if not exist "%%d" mkdir "%%d"
echo.
call agent.cmd doctor
echo.
echo Setup done. Use:  agent doctor   agent pdf chat files/report.pdf   agent bug status BUG-12345
