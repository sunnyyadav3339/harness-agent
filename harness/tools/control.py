"""Control tools: human handoff, notes, completion, and skill macros."""

from __future__ import annotations

import re

from .base import REGISTRY, ToolResult, tool

_PARAM_RE = re.compile(r"\{\{\s*param\.([A-Za-z0-9_]+)\s*\}\}")


@tool("human.ask",
      "Pause and ask the human through the handoff channel (payment approval, OTP, judgement call, or when stuck). "
      "wait_for={'url_contains':..}|{'text':..} lets the run resume automatically when the screen shows it.",
      "question, options=None, reason='judgement', wait_for=None, timeout_s=600")
async def human_ask(ctx, question: str, options=None, reason: str = "judgement", wait_for=None,
                    timeout_s: float = 600) -> ToolResult:
    return ToolResult(True, "handoff requested", data={"handoff": {
        "question": question,
        "options": list(options) if options else ["Done", "Cancel"],
        "reason": reason,
        "wait_for": wait_for,
        "timeout_s": float(timeout_s),
    }})


@tool("task.note", "Remember a fact for later steps (e.g. fares compared, file paths found).", "text")
async def task_note(ctx, text: str) -> ToolResult:
    ctx.notes.append(text[:400])
    return ToolResult(True, "noted")


@tool("task.done", "Finish the task successfully with a short summary of what was achieved and where proof is.",
      "summary")
async def task_done(ctx, summary: str) -> ToolResult:
    return ToolResult(True, summary, data={"terminal": "done"})


@tool("task.fail", "Give up on the task, explaining why (only after recovery options are exhausted).", "reason")
async def task_fail(ctx, reason: str) -> ToolResult:
    return ToolResult(True, reason, data={"terminal": "failed"})


def _fill_params(value, params: dict):
    if isinstance(value, str):
        return _PARAM_RE.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _fill_params(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_params(v, params) for v in value]
    return value


@tool("skill.run_macro",
      "Run a named fast-path macro from the active skill: a verified sequence of tool calls executed without "
      "model round-trips. Stops at the first failing non-optional step.",
      "name, params=None", timeout_s=180)
async def run_macro(ctx, name: str, params=None) -> ToolResult:
    skill = ctx.skill
    if skill is None or name not in skill.macros:
        available = ", ".join(skill.macros) if skill and skill.macros else "none"
        return ToolResult(False, error=f"macro '{name}' not found; available: {available}")
    macro = skill.macros[name]
    steps = macro.get("steps", []) if isinstance(macro, dict) else macro
    params = {**(macro.get("defaults", {}) if isinstance(macro, dict) else {}), **(params or {})}
    log_lines, point, changed = [], None, False
    for i, step in enumerate(steps, 1):
        tool_name = step.get("tool")
        if tool_name == "skill.run_macro":
            return ToolResult(False, error="macros cannot call macros")
        args = _fill_params(step.get("args", {}), params)
        res = await REGISTRY.call(tool_name, args, ctx)
        point = res.point or point
        changed = changed or res.changed_view
        # Reads are the point of a macro step; keep their output, trim the rest.
        limit = 900 if tool_name.endswith((".read", ".eval")) else 200
        log_lines.append(f"{i}. {tool_name} -> {res.summary(limit)}")
        if not res.ok and not step.get("optional"):
            return ToolResult(False, "\n".join(log_lines), error=f"macro '{name}' stopped at step {i}",
                              point=point, changed_view=True)
    return ToolResult(True, "\n".join(log_lines), point=point, changed_view=True)


@tool("skill.load", "Load another skill's instructions mid-task (to combine jobs).", "name")
async def skill_load(ctx, name: str) -> ToolResult:
    from ..skills.loader import SkillLibrary

    skill = SkillLibrary(ctx.settings.skills_dir).get(name)
    if skill is None:
        return ToolResult(False, error=f"no skill named {name!r}")
    ctx.extra_skills.append(skill)
    return ToolResult(True, skill.body[:6000])
