@echo off
rem Runs AgentBuild natively on Windows (no Docker), e.g.  agent doctor   agent bug status BUG-12345
rem First time: run setup-windows.cmd
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
if not exist venv\Scripts\agent.exe (
  echo Not set up yet. Run  setup-windows.cmd  first.
  exit /b 1
)

rem Load settings from .env (IMAP password, model choices). TZ is skipped: Windows already knows your time zone.
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if /i not "%%a"=="TZ" if not "%%b"=="" if not defined %%a set "%%a=%%b"
  )
)

rem Ollama uses an NVIDIA GPU by itself when there is one. Without a GPU: 4B main model, 4x longer time limits.
set "HAS_GPU=0"
where nvidia-smi >nul 2>&1 && set "HAS_GPU=1"
if "%HAS_GPU%"=="1" (
  if not defined AGENT_MODEL_MAIN set "AGENT_MODEL_MAIN=qwen3.5:9b"
  if not defined AGENT_TIMEOUT_SCALE set "AGENT_TIMEOUT_SCALE=1"
) else (
  if not defined AGENT_TIMEOUT_SCALE set "AGENT_TIMEOUT_SCALE=4"
)

venv\Scripts\agent.exe %*
exit /b %errorlevel%
