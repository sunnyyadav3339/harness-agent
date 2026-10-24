"""Append-only run log: trace.jsonl + per-step screenshots + run.json.

Every write is flushed and fsync'd so a crash or power cut mid-run still
leaves a replayable log up to the last completed step.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from ..credentials import SECRETS
from ..timeutil import CLOCK, IST

log = logging.getLogger(__name__)


def _now() -> dict:
    ts = CLOCK.now_ts()
    return {
        "utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds"),
        "ist": datetime.fromtimestamp(ts, IST).isoformat(timespec="milliseconds"),
    }


class RunLogger:
    def __init__(self, runs_dir: Path, run_id: str, task: str, skill: str | None, trigger: str) -> None:
        self.run_id = run_id
        self.dir = runs_dir / run_id
        self.steps_dir = self.dir / "steps"
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        self._trace = (self.dir / "trace.jsonl").open("a", encoding="utf-8")
        self.artifacts: list[str] = []
        self.meta: dict[str, Any] = {
            "run_id": run_id,
            "task": task,
            "skill": skill,
            "trigger": trigger,
            "started": _now(),
            "status": "running",
            "handoffs": 0,
            "steps": 0,
        }
        if (self.dir / "run.json").exists():  # resumed run
            try:
                self.meta.update(json.loads((self.dir / "run.json").read_text(encoding="utf-8")))
                self.meta["status"] = "running"
            except Exception:
                pass
        self._write_meta()

    def _write(self, record: dict) -> None:
        line = SECRETS.redact(json.dumps(record, ensure_ascii=False, default=str))
        self._trace.write(line + "\n")
        self._trace.flush()
        os.fsync(self._trace.fileno())

    def _write_meta(self) -> None:
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(SECRETS.redact(json.dumps(self.meta, indent=2, default=str)), encoding="utf-8")
        os.replace(tmp, self.dir / "run.json")

    def save_image(self, step: int, kind: str, img: Image.Image | None, quality: int = 72) -> str | None:
        if img is None:
            return None
        name = f"{step:04d}_{kind}.jpg"
        try:
            img.convert("RGB").save(self.steps_dir / name, format="JPEG", quality=quality)
        except Exception as exc:
            log.warning("could not save %s: %s", name, exc)
            return None
        return f"steps/{name}"

    def step(self, record: dict) -> None:
        self.meta["steps"] = max(self.meta.get("steps", 0), int(record.get("step", 0)))
        self._write({"type": "step", **_now(), **record})

    def event(self, kind: str, **data: Any) -> None:
        if kind == "handoff":
            self.meta["handoffs"] = self.meta.get("handoffs", 0) + 1
            self._write_meta()
        self._write({"type": "event", "event": kind, **_now(), **data})

    def add_artifact(self, path: Path) -> None:
        try:
            rel = str(Path(path).resolve().relative_to(self.dir.resolve())).replace("\\", "/")
        except ValueError:
            dest = self.dir / Path(path).name
            shutil.copy2(path, dest)
            rel = dest.name
        if rel not in self.artifacts:
            self.artifacts.append(rel)
            self.meta["artifacts"] = self.artifacts
            self._write_meta()

    def finish(self, status: str, summary: str = "", extra: dict | None = None) -> None:
        self.meta.update({"status": status, "summary": summary, "finished": _now(), **(extra or {})})
        self._write_meta()
        self.event("finished", status=status, summary=summary)
        try:
            from .replay import build_replay

            build_replay(self.dir)
        except Exception as exc:
            log.error("replay build failed: %s", exc)

    def close(self) -> None:
        try:
            self._trace.close()
        except Exception:
            pass
