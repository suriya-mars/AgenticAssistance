#!/usr/bin/env sh
# Build and export a self-contained bundle folder: images + compose + config + scripts.
# Copy the folder to any machine with Docker and run ./install.sh there.
# Usage: [FLAVOR=cpu|gpu] scripts/export.sh [version] [output-dir]   e.g. scripts/export.sh 1.0 /run/media/$USER/usb
# FLAVOR=cpu (default) runs on any x86-64 machine. Use a version name per flavor, e.g. 1.0 and 1.0-gpu.
# Builds only if the images are missing (REBUILD=1 to force). The bundle is ~13 GB (models don't compress).
set -e
cd "$(dirname "$0")/.."
VERSION="${1:-1.0}"
OUT="${2:-dist}/agentbuild-$VERSION"
if [ -n "${REBUILD:-}" ] || ! docker image inspect "agentbuild-app:$VERSION" "agentbuild-ollama:$VERSION" >/dev/null 2>&1; then
  scripts/build.sh "$VERSION"
fi
mkdir -p "$(dirname "$OUT")"
FREE_KB=$(df -Pk "$(dirname "$OUT")" | awk 'NR==2 {print $4}')
[ "$FREE_KB" -gt 11000000 ] || { echo "Need ~11 GB free (cpu) / ~15 GB (gpu) at $(dirname "$OUT") (have $((FREE_KB/1000000)) GB). Pass another output dir."; exit 1; }
rm -rf "$OUT" && mkdir -p "$OUT"
echo "Saving images to $OUT/images.tar ..."
docker save -o "$OUT/images.tar" "agentbuild-app:$VERSION" "agentbuild-ollama:$VERSION"
cp compose.yaml compose.gpu.yaml .env.example "$OUT/"
sed -i "s/AGENT_VERSION:-1.0/AGENT_VERSION:-$VERSION/" "$OUT/compose.yaml"
cp -r config "$OUT/config"
cp docker/bundle/agent docker/bundle/install.sh docker/bundle/README.md "$OUT/"
mkdir -p "$OUT/data" "$OUT/files" "$OUT/repos"
du -sh "$OUT"
echo "Done. Copy $OUT to the target machine and run ./install.sh"
