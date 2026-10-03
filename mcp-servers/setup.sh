#!/usr/bin/env sh
# One-time setup (Linux / macOS): a separate venv for the MCP servers, with AgentBuild installed from the parent
# folder. Ollama and the models are the same ones AgentBuild uses (see ../README.md).
set -e
cd "$(dirname "$0")"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ is required"; exit 1; }
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/python -m pip install --quiet --upgrade pip
venv/bin/python -m pip install --quiet -e .. -e '.[dev]'
venv/bin/agentbuild-mcp --list-tools
echo
echo "Done. The command MCP clients should launch: $(pwd)/venv/bin/agentbuild-mcp"
