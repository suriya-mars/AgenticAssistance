"""Copy selected models from the local Ollama store into docker/build/models for the image build.

Uses hard links when possible (no extra disk space). Only the listed models' manifests and blobs are staged.
Usage: python scripts/stage_models.py qwen3:4b qwen3.5:9b
"""
import json
import os
import shutil
import sys
from pathlib import Path

SRC = Path(os.environ.get("OLLAMA_MODELS", Path.home() / ".ollama" / "models"))
DST = Path(__file__).resolve().parents[1] / "docker" / "build" / "models"


def manifest_path(root: Path, tag: str) -> Path:
    name, _, version = tag.partition(":")
    ns, _, model = name.rpartition("/")
    return root / "manifests" / "registry.ollama.ai" / (ns or "library") / model / (version or "latest")


def link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main(tags: list[str]) -> None:
    if not tags:
        sys.exit(__doc__)
    shutil.rmtree(DST, ignore_errors=True)
    total = 0
    for tag in tags:
        mp = manifest_path(SRC, tag)
        if not mp.is_file():
            sys.exit(f"model '{tag}' is not in {SRC}. Run: ollama pull {tag}")
        manifest = json.loads(mp.read_text())
        for layer in [manifest["config"], *manifest["layers"]]:
            blob = SRC / "blobs" / layer["digest"].replace(":", "-")
            link(blob, DST / "blobs" / blob.name)
            total += layer["size"]
        link(mp, manifest_path(DST, tag))
        print(f"staged {tag}")
    print(f"{total / 1e9:.2f} GB staged in {DST}")


if __name__ == "__main__":
    main(sys.argv[1:])
