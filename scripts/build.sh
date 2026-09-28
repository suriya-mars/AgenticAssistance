#!/usr/bin/env sh
# Build both images.
# Usage: scripts/build.sh [version]
#   MODELS="qwen3:4b qwen3.5:9b"   models to bake in (default)
#   MODELS_SOURCE=auto|local|pull  auto (default): copy from this machine's Ollama if it has the models,
#                                  otherwise download them during the build (needs internet, no Ollama needed)
#   FLAVOR=cpu|gpu                 cpu (default): runs on any x86-64 machine; gpu: adds CUDA backends (+4.7 GB)
set -e
cd "$(dirname "$0")/.."
export AGENT_VERSION="${1:-1.0}"
export OLLAMA_FLAVOR="${FLAVOR:-cpu}"
export MODELS="${MODELS:-qwen3:4b qwen3.5:9b}"
SOURCE="${MODELS_SOURCE:-auto}"

if [ "$SOURCE" != "pull" ] && command -v python3 >/dev/null && python3 scripts/stage_models.py $MODELS; then
  export OLLAMA_DOCKERFILE=ollama.Dockerfile
elif [ "$SOURCE" = "local" ]; then
  echo "MODELS_SOURCE=local but the models are not in the local Ollama store."; exit 1
else
  echo "Downloading models during the build: $MODELS (about 9 GB for the defaults)"
  export OLLAMA_DOCKERFILE=ollama-pull.Dockerfile
fi
docker compose -f compose.yaml -f compose.build.yaml --profile cli build   # --profile: include the agent service
rm -rf docker/build            # staged hard links are no longer needed
docker image ls "agentbuild-*:$AGENT_VERSION"
