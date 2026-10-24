"""Prompt assembly. Kept compact: free-tier TPM budgets are small."""

from __future__ import annotations

import base64
import json

import yaml

from ..perception.annotate import to_jpeg
from ..timeutil import CLOCK
from ..tools.base import REGISTRY
from .context import AgentState, RunContext

SYSTEM = """You are a computer-use agent operating a Windows 11 PC the way a careful, fast person does: web browser, desktop apps, terminal and files. Each turn you get the current screen state; you choose the next action(s); the harness runs them and shows you the result next turn.

Reply with ONE JSON object and nothing else:
{"verify_previous": "success|failed|n/a",
 "thought": "what you see and why the next action (max 3 sentences)",
 "phase": "short stage label (use the skill's state names when given)",
 "actions": [{"tool": "name", "args": {}}],
 "expect": "what the screen should show after these actions",
 "need_vision": false}

Rules:
- verify_previous: judge from the CURRENT observation whether the previous actions achieved their "expect".
- Use element ids from the CURRENT observation only; ids are reassigned every turn. Prefer ids; use x,y screenshot pixels only when no element fits.
- Batch 2-6 actions only when the view will not change between them (e.g. fill several fields, then click submit). Otherwise one action.
- Secrets: never write real credentials. Use placeholders {{secret.NAME}} in args; vault values via {{vault.path}} or copied from the vault.
- When an action fails, do not repeat it identically more than twice: try another element, a selector, the keyboard, scrolling, or waiting.
- Popups, banners, cookie dialogs: dismiss them (decline non-essential cookies) and continue.
- human.ask only to approve a payment, for a judgement call, for information you cannot obtain (an OTP), or when truly stuck. Every handoff costs points.
- Irreversible actions (paying, deleting, sending messages, submitting forms) only when the task or skill calls for them.
- Finish with task.done (summary + where the proof is saved) or task.fail when it is impossible. If you had to substitute something the task did not literally ask for (e.g. the page has no heading, so you used its title), say so plainly in the summary.
- Set need_vision true when you must see the screenshot next turn (layout, images, canvas apps); text-only turns are faster.

Tools:
{tools}"""


def _history_lines(history: list[dict], limit: int = 10) -> str:
    lines = []
    for h in history[-limit:]:
        if h.get("kind") == "handoff":
            lines.append(f"#{h['step']} HUMAN asked: {h['question'][:120]} -> answer: {h['answer'][:120]}")
            continue
        acts = "; ".join(
            f"{a['tool']}({json.dumps(a.get('args', {}), ensure_ascii=False)[:90]}) => {r}"
            for a, r in zip(h.get("actions", []), h.get("results", []) + ["not run"] * 10)
        )
        lines.append(f"#{h['step']} [{h.get('phase', '')}] {h.get('thought', '')[:160]} | {acts}")
    return "\n".join(lines) or "(none yet)"


def _skill_block(ctx: RunContext, limit: int = 7000) -> str:
    parts = []
    if ctx.skill:
        parts.append(f"# Skill: {ctx.skill.name}\n{ctx.skill.body[:limit]}")
        if ctx.skill.macros:
            parts.append("Macros (call skill.run_macro): " + ", ".join(
                f"{k}{' - ' + v.get('description', '') if isinstance(v, dict) else ''}" for k, v in ctx.skill.macros.items()))
    for s in ctx.extra_skills:
        parts.append(f"# Loaded skill: {s.name}\n{s.body[:3000]}")
    if ctx.other_skills:
        parts.append("Other skills (skill.load to use): " + "; ".join(f"{n}: {d[:80]}" for n, d in ctx.other_skills[:15]))
    if not parts:
        parts.append("# No skill matched this task: work it out from first principles.")
    return "\n\n".join(parts)


def build_messages(state: AgentState, ctx: RunContext, with_image: bool, last_results_full: str = "") -> list[dict]:
    allowed = ctx.skill.tools if ctx.skill and ctx.skill.tools else None
    if allowed:
        allowed = list(allowed) + ["task.", "human.", "skill.", "wait", "clock."]
    system = SYSTEM.replace("{tools}", REGISTRY.catalog(allowed))

    now = CLOCK.now()
    secrets = ", ".join(ctx.secrets.available_names()) or "none configured"
    vault = yaml.safe_dump(ctx.vault, allow_unicode=True, sort_keys=False)[:2500] if ctx.vault else "(empty)"
    notes = "\n".join(f"- {n}" for n in (state.get("notes") or [])[-15:]) or "(none)"
    step, max_steps = state.get("step", 0), state.get("max_steps", 80)

    text = "\n\n".join([
        f"# Task\n{state['task']}",
        f"# Now\n{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} IST ({now:%A}). Step {step + 1} of max {max_steps}.",
        _skill_block(ctx),
        f"# Vault (non-secret data)\n{vault}",
        f"# Secret placeholders available\n{secrets}",
        f"# Notes\n{notes}",
        f"# Recent steps\n{_history_lines(state.get('history') or [])}",
        f"# Result of the previous actions\n{last_results_full or '(first step)'}",
        f"# Current observation ({state.get('surface', '?')})\n{ctx.observation_text}",
    ])
    if state.get("failures"):
        text += f"\n\nWARNING: {state['failures']} consecutive failed steps. Change approach."

    content: list[dict] | str
    if with_image and ctx.model_image is not None:
        b64 = base64.b64encode(to_jpeg(ctx.model_image, quality=65)).decode()
        content = [
            {"type": "text", "text": text + "\n\nThe screenshot shows numbered boxes matching element ids."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    else:
        content = text
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


REFLECT_PROMPT = """You maintain the skill "{skill}" ({description}) for a computer-use agent.
Below is a run trace with the errors and retries it hit. Write at most 2 NEW heuristics for THIS skill that
would have avoided those errors or made the run faster next time.

A heuristic qualifies only if ALL hold:
- it is about the app/site/workflow this skill covers (not about an unrelated page the run happened to visit);
- it is backed by something that actually happened in the trace (an error, then what worked);
- it is concrete (selector, control name, ordering, timing) and at most 200 characters;
- it never relaxes the task: no "substitute X when Y is missing" or "skip verification" advice;
- it does not restate an existing heuristic.
Most runs teach nothing durable: return an empty list unless you are confident.
Reply JSON: {"heuristics": ["..."]}

Existing heuristics:
{existing}

Task: {task}
Final status: {status}
Errors and retries:
{errors}

Step history:
{history}"""


DRAFT_SKILL_PROMPT = """Turn this successful computer-use trajectory into a reusable skill file (SKILL.md) for the same kind of task.
Generalise specific values into named variables written as {{vault.<file>.<key>}} or plain descriptions, and keep exact selectors/ids that are stable.
Format exactly:
---
name: <snake_case_name>
description: <one sentence: what it does and when to use it>
triggers: [<3-6 lowercase phrases a user might say>]
draft: true
---
# <Title>
## Preconditions
## Steps
(numbered, state-by-state, with what to verify after each)
## Recovery Playbook
## Learned Heuristics (auto-updated)

Reply with JSON {"skill_md": "<the full file>"}.

Task: {task}
Trajectory:
{history}"""
