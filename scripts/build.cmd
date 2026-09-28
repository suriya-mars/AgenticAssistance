@echo off
rem Build both images on Windows (cmd or PowerShell). No WSL, Python or Ollama needed, only Docker Desktop.
rem The models are downloaded during the build (~9 GB, once; later builds reuse the cache).
rem Usage:  scripts\build.cmd [version]        e.g.  scripts\build.cmd 1.0
setlocal
cd /d "%~dp0.."
set "AGENT_VERSION=%~1"
if "%AGENT_VERSION%"=="" set "AGENT_VERSION=1.0"
if not defined FLAVOR set "FLAVOR=cpu"
set "OLLAMA_FLAVOR=%FLAVOR%"
if not defined MODELS set "MODELS=qwen3:4b qwen3.5:9b"
set "OLLAMA_DOCKERFILE=ollama-pull.Dockerfile"

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is not running. Start Docker Desktop, wait for "Engine running", then try again.
  exit /b 1
)
echo Building AgentBuild %AGENT_VERSION% [%OLLAMA_FLAVOR%]. Downloading models during the build: %MODELS%
docker compose -f compose.yaml -f compose.build.yaml --profile cli build
if errorlevel 1 exit /b 1
docker image ls agentbuild-app
docker image ls agentbuild-ollama
echo.
echo Done. Next:  copy .env.example .env   then   agent-docker doctor
