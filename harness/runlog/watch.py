"""Live, human-readable view of a run: `python -m harness watch [run_id] [--new]`."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


def format_record(r: dict) -> str | None:
    t = (r.get("ist") or "")[11:19]
    if r.get("type") == "step":
        acts = "; ".join(f"{a['tool']}({json.dumps(a.get('args', {}), ensure_ascii=False)[:90]})"
                         for a in r.get("actions", [])) or "(no action)"
        got = " | ".join(("OK " if x["ok"] else "ERR ") + x["summary"].replace("\n", " ")[:150]
                         for x in r.get("results", [])) or "-"
        return (f"{t} step {r['step']} [{r.get('phase', '')}] {r.get('surface', '')} "
                f"model={r.get('model')} llm={r.get('llm_ms')}ms verify_prev={r.get('verify_previous')}\n"
                f"    why: {(r.get('thought') or '')[:240]}\n    did: {acts}\n    got: {got}")
    ev = r.get("event")
    if ev == "handoff":
        return f"{t} HANDOFF ({r.get('reason')}): {(r.get('question') or '')[:300]} options={r.get('options')}"
    if ev == "handoff_resolved":
        return f"{t} HANDOFF RESOLVED via {r.get('source')}: {r.get('answer')} (waited {r.get('waited_s')}s)"
    if ev == "finished":
        return f"{t} FINISHED status={r.get('status')}: {(r.get('summary') or '')[:400]}"
    if ev == "started":
        return f"{t} STARTED skill={r.get('skill')} task={(r.get('task') or '')[:160]}"
    return f"{t} {ev}: {r.get('message') or ''}"[:400]


def latest_run(runs_dir: Path, created_after: float | None = None) -> Path | None:
    runs = [p for p in runs_dir.glob("*") if p.is_dir() and (p / "run.json").exists()]
    if created_after is not None:
        runs = [p for p in runs if p.stat().st_ctime >= created_after - 1]
    return max(runs, key=lambda p: p.stat().st_ctime) if runs else None


def follow(run_dir: Path, emit: Callable[[str], None] = print, poll_s: float = 0.5,
           stop_on_finish: bool = True, max_wait_s: float | None = None) -> str | None:
    """Emit each trace record as it is written. Returns the final status, if the run finished."""
    trace = run_dir / "trace.jsonl"
    pos, buf = 0, ""
    started = time.time()
    while True:
        if trace.exists():
            with trace.open(encoding="utf-8") as fh:
                fh.seek(pos)
                buf += fh.read()
                pos = fh.tell()
            *lines, buf = buf.split("\n")  # keep a partially written last line for next time
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = format_record(rec)
                if text:
                    emit(text)
                if rec.get("event") == "finished" and stop_on_finish:
                    return rec.get("status")
        if max_wait_s is not None and time.time() - started > max_wait_s:
            return None
        time.sleep(poll_s)
