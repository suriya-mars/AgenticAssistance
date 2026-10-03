@echo off
rem One-time setup on Windows (no admin rights needed): a separate venv for the MCP servers, with AgentBuild
rem installed from the parent folder. Ollama and the models are the same ones AgentBuild uses.
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
  exit /b 1
)
if not exist venv\Scripts\python.exe (
  python -m venv venv
  if errorlevel 1 exit /b 1
)
venv\Scripts\python -m pip install --quiet --upgrade pip
venv\Scripts\python -m pip install --quiet -e .. -e .[dev]
if errorlevel 1 (
  echo [x] pip install failed. Behind a company proxy? Run:  set HTTPS_PROXY=http://proxy:port  and try again.
  exit /b 1
)
venv\Scripts\agentbuild-mcp.exe --list-tools
echo.
echo Done. The command MCP clients should launch: %CD%\venv\Scripts\agentbuild-mcp.exe
