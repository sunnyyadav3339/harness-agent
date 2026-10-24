"""Builds a self-contained replay.html next to trace.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

_TEMPLATE = Path(__file__).with_name("replay_template.html")


def build_replay(run_dir: Path) -> Path:
    records = []
    trace = run_dir / "trace.jsonl"
    if trace.exists():
        for line in trace.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    meta = {}
    if (run_dir / "run.json").exists():
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    videos = sorted(p.name for p in (run_dir / "video").glob("*.mp4")) if (run_dir / "video").exists() else []
    payload = json.dumps({"meta": meta, "records": records, "videos": videos}, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")  # keep </script> out of the inline JSON
    html = _TEMPLATE.read_text(encoding="utf-8").replace("/*__RUN_DATA__*/null", payload)
    out = run_dir / "replay.html"
    out.write_text(html, encoding="utf-8")
    return out
