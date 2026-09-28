#!/usr/bin/env sh
# One-time setup on a new machine: check the machine, load the images, create folders, run doctor.
set -e
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required"; exit 1; }
CV=$(docker compose version --short 2>/dev/null | sed 's/^v//')
if [ "$(printf '%s\n2.24.0\n' "$CV" | sort -V | head -1)" != "2.24.0" ]; then
  echo "Docker Compose $CV is too old; 2.24 or newer is required (update Docker)."; exit 1
fi
docker info >/dev/null 2>&1 || { echo "Cannot talk to Docker. Is it running, and is your user in the 'docker' group?"; exit 1; }

[ "$(uname -m)" = "x86_64" ] || echo "WARNING: this bundle is built for x86-64; this machine is $(uname -m)."
if [ -r /proc/cpuinfo ] && ! grep -q -m1 avx2 /proc/cpuinfo; then
  echo "NOTE: CPU has no AVX2; the models will run, but slowly."
fi
if [ -r /proc/meminfo ]; then
  MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  [ "$MEM_GB" -ge 12 ] || echo "WARNING: ${MEM_GB} GB RAM. qwen3:4b needs ~4 GB free, qwen3.5:9b ~8 GB."
fi
NEED_KB=25000000   # images unpacked (plus images.tar while loading)
FREE_KB=$(df -Pk "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /)" 2>/dev/null | awk 'NR==2 {print $4}')
[ -z "$FREE_KB" ] || [ "$FREE_KB" -ge "$NEED_KB" ] || \
  echo "WARNING: Docker's disk has $((FREE_KB/1000000)) GB free; loading needs ~25 GB."

echo "Loading images (a few minutes)..."
docker load -i images.tar
[ -f .env ] || cp .env.example .env
mkdir -p data files repos
chmod +x agent
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
  echo "NVIDIA runtime found: models run on the GPU (main model qwen3.5:9b)."
else
  echo "No GPU runtime: models run on the CPU (main model qwen3:4b, longer time limits)."
fi
./agent doctor
echo
echo "Installed. images.tar is no longer needed and can be deleted to free space."
