"""Append-only JSONL audit trace, one file per run."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agentbuild.guardrails.output import redact


class Tracer:
    def __init__(self, runs_dir: Path, run_id: str):
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.path = runs_dir / f"{run_id}.jsonl"

    def log(self, event: str, **data: Any) -> None:
        rec = {"ts": time.time(), "event": event, **data}
        line = redact(json.dumps(rec, default=str, ensure_ascii=False))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class NullTracer:
    path = None

    def log(self, event: str, **data: Any) -> None:
        pass
