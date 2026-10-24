"""Self-improvement: write lessons back into SKILL.md, or draft a new skill.

Safety: the previous file is copied to ``history/`` first, the new text must
still parse as a valid skill, and the write is atomic (tmp + replace), so a
bad reflection can never break the next production run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path

from ..llm.parsing import parse_json_object
from ..timeutil import CLOCK
from .loader import parse_skill_text

log = logging.getLogger(__name__)

HEADING = "## Learned Heuristics (auto-updated)"
_HEADING_RE = re.compile(r"^##\s+Learned Heuristics.*$", re.M | re.I)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def backup(path: Path) -> Path:
    hist = path.parent / "history"
    hist.mkdir(exist_ok=True)
    dest = hist / f"SKILL_{CLOCK.now():%Y%m%d_%H%M%S}.md"
    shutil.copy2(path, dest)
    return dest


def existing_heuristics(text: str) -> list[str]:
    m = _HEADING_RE.search(text)
    if not m:
        return []
    section = text[m.end():]
    nxt = re.search(r"^##\s", section, re.M)
    section = section[: nxt.start()] if nxt else section
    return [ln.strip()[2:] for ln in section.splitlines() if ln.strip().startswith("- ")]


def append_heuristics(path: Path, items: list[str]) -> int:
    text = path.read_text(encoding="utf-8")
    have = {re.sub(r"^\[[^\]]*\]:?\s*", "", h).lower() for h in existing_heuristics(text)}
    stamp = CLOCK.now().strftime("%Y-%m-%d %H:%M IST")
    fresh = [i.strip() for i in items if i and i.strip() and i.strip().lower() not in have]
    if not fresh:
        return 0
    bullets = "\n".join(f"- [Auto-learned {stamp}]: {i}" for i in fresh)
    m = _HEADING_RE.search(text)
    if m:
        rest = text[m.end():]
        nxt = re.search(r"^##\s", rest, re.M)
        cut = m.end() + (nxt.start() if nxt else len(rest))
        new_text = text[:cut].rstrip() + "\n" + bullets + "\n" + ("\n" + text[cut:] if nxt else "")
    else:
        new_text = text.rstrip() + f"\n\n{HEADING}\n{bullets}\n"
    parse_skill_text(new_text, path)  # raises if we produced an invalid skill
    backup(path)
    _atomic_write(path, new_text)
    return len(fresh)


def _history_digest(state: dict, limit: int = 30) -> str:
    lines = []
    for h in (state.get("history") or [])[-limit:]:
        if h.get("kind") == "handoff":
            lines.append(f"#{h['step']} HUMAN: {h['question'][:100]} -> {h['answer'][:80]}")
            continue
        acts = "; ".join(f"{a['tool']}({json.dumps(a.get('args', {}))[:100]}) -> {r[:120]}"
                         for a, r in zip(h.get("actions", []), h.get("results", [])))
        lines.append(f"#{h['step']} [{h.get('phase', '')}] {h.get('thought', '')[:120]} | {acts}")
    return "\n".join(lines)


async def reflect_on_run(ctx, state: dict) -> None:
    from ..runtime.prompts import DRAFT_SKILL_PROMPT, REFLECT_PROMPT

    status = state.get("status", "running")
    errors = state.get("error_ledger") or []
    # Learn only from real friction: a tool that failed, or a human who had to step in.
    # The planner merely doubting itself ("judged failed") is not evidence of anything durable.
    tool_errors = [e for e in errors if "judged failed" not in e and "planner" not in e]
    if ctx.skill is not None:
        if not tool_errors and not state.get("handoffs"):
            ctx.run.event("reflection", message="no tool errors or handoffs; nothing new to learn")
            return
        text = ctx.skill.path.read_text(encoding="utf-8")
        prompt = (REFLECT_PROMPT
                  .replace("{skill}", ctx.skill.name)
                  .replace("{description}", ctx.skill.description)
                  .replace("{existing}", "\n".join(existing_heuristics(text)) or "(none)")
                  .replace("{task}", state.get("task", ""))
                  .replace("{status}", status)
                  .replace("{errors}", "\n".join(tool_errors[-20:]) or "(none; a human had to step in)")
                  .replace("{history}", _history_digest(state)))
        resp = await ctx.router.complete("fast", [{"role": "user", "content": prompt}], max_tokens=500,
                                         temperature=0.2, deadline_s=40)
        items = [str(x) for x in parse_json_object(resp.text).get("heuristics", [])][:2]
        added = append_heuristics(ctx.skill.path, items)
        ctx.run.event("reflection", message=f"added {added} heuristic(s) to {ctx.skill.name}", heuristics=items)
        return

    # No skill was used: a successful zero-shot run becomes a draft skill.
    if status == "done" and state.get("step", 0) >= 3:
        prompt = (DRAFT_SKILL_PROMPT.replace("{task}", state.get("task", ""))
                  .replace("{history}", _history_digest(state, 50)))
        resp = await ctx.router.complete("fast", [{"role": "user", "content": prompt}], max_tokens=1800,
                                         temperature=0.2, deadline_s=60)
        md = parse_json_object(resp.text).get("skill_md", "")
        path = save_draft_skill(ctx.settings.skills_dir, md)
        ctx.run.event("reflection", message=f"drafted new skill at {path}")
        if ctx.channel:
            await ctx.channel.send(f"New draft skill saved: {path.parent.name} (review it before relying on it).")


def save_draft_skill(skills_dir: Path, md: str) -> Path:
    md = md.strip() + "\n"
    skill = parse_skill_text(md, skills_dir / "draft" / "SKILL.md")  # validate first
    slug = re.sub(r"[^a-z0-9_]+", "_", skill.name.lower()).strip("_") or "new_skill"
    target_dir = skills_dir / slug
    n = 2
    while (target_dir / "SKILL.md").exists():
        target_dir = skills_dir / f"{slug}_{n}"
        n += 1
    target_dir.mkdir(parents=True)
    path = target_dir / "SKILL.md"
    _atomic_write(path, md)
    return path
