@echo off
rem Runs any AgentBuild command in Docker from Windows cmd or PowerShell, e.g.
rem   agent-docker doctor
rem   agent-docker pdf chat files/report.pdf        (use forward slashes in paths)
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
for %%d in (data files repos) do if not exist "%%d" mkdir "%%d"

rem Read the settings this script needs from .env (docker compose reads the rest itself)
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="AGENT_GPU" if not defined AGENT_GPU set "AGENT_GPU=%%b"
    if /i "%%a"=="AGENT_MODEL_MAIN" if not defined AGENT_MODEL_MAIN set "AGENT_MODEL_MAIN=%%b"
    if /i "%%a"=="AGENT_MODEL_QUALITY" if not defined AGENT_MODEL_QUALITY set "AGENT_MODEL_QUALITY=%%b"
    if /i "%%a"=="AGENT_TIMEOUT_SCALE" if not defined AGENT_TIMEOUT_SCALE set "AGENT_TIMEOUT_SCALE=%%b"
    if /i "%%a"=="TZ" if not defined TZ set "TZ=%%b"
  )
)

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is not running. Start Docker Desktop, wait for "Engine running", then try again.
  exit /b 1
)

rem GPU if Docker has the NVIDIA runtime, else CPU
if not defined AGENT_GPU (
  set "AGENT_GPU=0"
  docker info --format "{{json .Runtimes}}" 2>nul | findstr /i nvidia >nul && set "AGENT_GPU=1"
)
set "FILES=-f compose.yaml"
if "%AGENT_GPU%"=="1" set "FILES=!FILES! -f compose.gpu.yaml"
if exist compose.local.yaml set "FILES=!FILES! -f compose.local.yaml"

rem Per-machine defaults: GPU = 9B main model; CPU = 4B main model and 4x longer time limits
if "%AGENT_GPU%"=="1" (
  if not defined AGENT_MODEL_MAIN set "AGENT_MODEL_MAIN=qwen3.5:9b"
  if not defined AGENT_TIMEOUT_SCALE set "AGENT_TIMEOUT_SCALE=1"
) else (
  if not defined AGENT_MODEL_MAIN set "AGENT_MODEL_MAIN=qwen3:4b"
  if not defined AGENT_TIMEOUT_SCALE set "AGENT_TIMEOUT_SCALE=4"
)
if not defined AGENT_MODEL_QUALITY set "AGENT_MODEL_QUALITY=qwen3.5:9b"

rem Windows time zone -> IANA name for the container (set TZ in .env if yours is not listed)
if not defined TZ (
  set "TZ=UTC"
  for /f "delims=" %%z in ('tzutil /g') do set "WINTZ=%%z"
  if /i "!WINTZ!"=="India Standard Time" set "TZ=Asia/Kolkata"
  if /i "!WINTZ!"=="Sri Lanka Standard Time" set "TZ=Asia/Colombo"
  if /i "!WINTZ!"=="Singapore Standard Time" set "TZ=Asia/Singapore"
  if /i "!WINTZ!"=="Arabian Standard Time" set "TZ=Asia/Dubai"
  if /i "!WINTZ!"=="GMT Standard Time" set "TZ=Europe/London"
  if /i "!WINTZ!"=="W. Europe Standard Time" set "TZ=Europe/Berlin"
  if /i "!WINTZ!"=="Eastern Standard Time" set "TZ=America/New_York"
  if /i "!WINTZ!"=="Central Standard Time" set "TZ=America/Chicago"
  if /i "!WINTZ!"=="Pacific Standard Time" set "TZ=America/Los_Angeles"
  if /i "!WINTZ!"=="AUS Eastern Standard Time" set "TZ=Australia/Sydney"
)

docker compose %FILES% up -d --wait ollama >nul
if errorlevel 1 (
  echo Could not start the Ollama container. Run  docker compose logs ollama  to see why.
  exit /b 1
)
docker compose %FILES% run --rm agent %*
exit /b %errorlevel%
