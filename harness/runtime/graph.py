"""The loop: observe -> plan -> act -> (verify/route) -> observe ... -> reflect.

Verification is split in two so it costs no extra model call:
* deterministic checks right after acting (tool errors, policy, step budget,
  repeated identical actions), and
* the planner's ``verify_previous`` judgement of the last ``expect`` against
  the fresh observation, made in the same call that plans the next step.

Human handoff is a dedicated node that only calls ``interrupt()``, so resuming
never re-executes side-effecting actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..llm.parsing import ParseError, parse_json_object
from ..llm.router import AllModelsExhausted
from ..perception import desktop_view
from ..perception.annotate import draw_crosshair, fit
from ..perception.screen import grab
from ..tools.base import REGISTRY, ToolResult
from ..tools.browser import observe_browser
from .context import CONTEXTS, AgentState, RunContext
from .prompts import build_messages

log = logging.getLogger(__name__)

MAX_BATCH = 8
MAX_FAILURES = 4
HISTORY_KEEP = 40
_BROWSER_FAMILY = ("browser.", "vision.read_text")


def _ctx(state: AgentState) -> RunContext:
    return CONTEXTS[state["run_id"]]


def _choose_surface(state: AgentState, ctx: RunContext) -> str:
    plan = state.get("plan") or {}
    hint = plan.get("surface")
    if hint in ("browser", "desktop"):
        return hint
    actions = plan.get("actions") or []
    for a in reversed(actions):
        tool = a.get("tool", "")
        if tool.startswith("browser."):
            return "browser"
        if tool.startswith("desktop."):
            return "desktop"
    if state.get("surface"):
        return state["surface"]
    if ctx.skill and ctx.skill.tools:
        fams = {t.split(".")[0] for t in ctx.skill.tools}
        if "browser" in fams and "desktop" not in fams:
            return "browser"
    if not ctx.settings.browser_foreground:
        # Background (dev) mode: the screen belongs to the user, so start from the agent's browser.
        return "browser"
    return "desktop"


# ------------------------------------------------------------------ nodes

async def observe(state: AgentState) -> dict:
    ctx = _ctx(state)
    t0 = time.perf_counter()
    surface = _choose_surface(state, ctx)
    if surface == "browser":
        try:
            screen_task = asyncio.to_thread(grab)
            busy = ctx.skill.busy_selectors if ctx.skill else None
            snap, ctx.screen_image = await asyncio.gather(observe_browser(ctx, busy), screen_task)
            ctx.browser_snapshot = snap
            img = snap.annotated()
            if img is not None:
                ctx.model_image, scale = fit(img, 1280, 1280)
                ctx.browser_scale = (img.width / max(snap.viewport[0], 1)) * scale
            else:
                ctx.model_image = None
            ctx.observation_text = snap.describe()
        except Exception as exc:
            log.warning("browser observation failed (%s); falling back to desktop", exc)
            surface = "desktop"
    if surface == "desktop":
        snap = await desktop_view.snapshot()
        ctx.desktop_snapshot = snap
        ctx.screen_image = snap.image
        img = snap.annotated()
        ctx.model_image, ctx.desktop_scale = fit(img, 1280, 1280) if img is not None else (None, 1.0)
        text = snap.describe()
        if ctx.browser.connected:
            try:
                page = await ctx.browser.page()
                text += f"\n(Automated Chrome is open at {page.url}; use browser.* tools or browser.focus for web pages.)"
            except Exception:
                pass
        ctx.observation_text = text + (
            f"\nCoordinates: screenshot is {ctx.model_image.width}x{ctx.model_image.height}; give x,y in that space."
            if ctx.model_image is not None else "")
    ctx.observe_ms = int((time.perf_counter() - t0) * 1000)
    return {"surface": surface}


def _normalise_plan(raw: dict) -> dict:
    actions = raw.get("actions")
    if actions is None and raw.get("action"):
        actions = [raw["action"]]
    if isinstance(actions, dict):
        actions = [actions]
    clean = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("tool") or a.get("name") or "")
        if name and REGISTRY.get(name) is None:
            alt = name.replace("_", ".", 1)
            if REGISTRY.get(alt):
                name = alt
        args = a.get("args") or a.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        clean.append({"tool": name, "args": args})
    return {
        "verify_previous": str(raw.get("verify_previous", "n/a")),
        "thought": str(raw.get("thought", ""))[:1200],
        "phase": str(raw.get("phase", ""))[:60],
        "actions": clean[:MAX_BATCH],
        "expect": str(raw.get("expect", ""))[:400],
        "need_vision": bool(raw.get("need_vision", False)),
        "surface": raw.get("surface"),
    }


def _vision_group(state: AgentState, ctx: RunContext) -> str:
    router = ctx.router
    mode = ctx.skill.vision if ctx.skill else "auto"
    # A single failed lookup is usually fixable from the element list in a 0.5 s
    # text turn; only a second consecutive failure pays for a 2-5 s vision turn.
    want = (
        mode == "always"
        or state.get("surface") == "desktop"
        or state.get("need_vision")
        or state.get("failures", 0) >= 2
        or state.get("step", 0) == 0
    )
    if mode == "never":
        want = False
    group = "vision" if want else "fast"
    if not router.has_group(group):
        group = "fast" if group == "vision" else "vision"
    return group


async def plan(state: AgentState) -> dict:
    ctx = _ctx(state)
    group = _vision_group(state, ctx)
    last = (state.get("history") or [])[-1:] or [{}]
    last_full = "\n".join(last[0].get("results_full", [])) if last[0].get("kind") != "handoff" else (
        f"Human answered: {last[0].get('answer')}")
    messages = build_messages(state, ctx, with_image=group == "vision", last_results_full=last_full)
    t0 = time.perf_counter()
    try:
        resp = await ctx.router.complete(group, messages, max_tokens=900, temperature=0.2, deadline_s=45)
        try:
            raw = parse_json_object(resp.text)
        except ParseError as exc:
            messages = messages + [
                {"role": "assistant", "content": resp.text[:2000]},
                {"role": "user", "content": f"That was not valid JSON ({exc}). Reply with the JSON object only."},
            ]
            resp = await ctx.router.complete(group, messages, max_tokens=900, temperature=0.0, deadline_s=30)
            raw = parse_json_object(resp.text)
    except (AllModelsExhausted, ParseError) as exc:
        # act() sees the empty plan and counts the failure; back off a little first.
        log.error("planning failed: %s", exc)
        await asyncio.sleep(min(2 * (state.get("failures", 0) + 1), 10))
        return {
            "plan": {"actions": [], "thought": f"planner unavailable: {exc}", "phase": state.get("phase", ""),
                     "verify_previous": "n/a", "expect": "", "need_vision": False},
            "llm": {"group": group, "error": str(exc)[:300], "ms": int((time.perf_counter() - t0) * 1000)},
        }
    p = _normalise_plan(raw)
    return {
        "plan": p,
        "phase": p["phase"] or state.get("phase", ""),
        "need_vision": p["need_vision"],
        "llm": {"group": group, "deployment": resp.deployment, "ms": resp.latency_ms,
                "tokens": resp.prompt_tokens + resp.completion_tokens, "images_stripped": resp.images_stripped},
    }


def _annotated_for_log(ctx: RunContext, results: list[ToolResult]):
    img = ctx.model_image
    if img is None:
        return None
    for res in results:
        if res.point:
            surface, x, y = res.point
            scale = ctx.browser_scale if surface == "browser" else 1.0
            return draw_crosshair(img, (int(x * scale), int(y * scale)))
    return img


async def act(state: AgentState) -> dict:
    ctx = _ctx(state)
    p = state.get("plan") or {}
    step = state.get("step", 0) + 1
    actions = p.get("actions") or []
    results: list[ToolResult] = []
    handoff = None
    terminal = None

    for i, action in enumerate(actions):
        if ctx.cancel.is_set():
            break
        name, args = action["tool"], action.get("args", {})
        if name == "human.ask":
            res = await REGISTRY.call(name, args, ctx)
            results.append(res)
            handoff = {"kind": "ask", **res.data.get("handoff", {})}
            break
        res = await REGISTRY.call(name, args, ctx)
        results.append(res)
        if res.data.get("terminal"):
            terminal = (res.data["terminal"], res.output)
            break
        if res.data.get("needs_approval"):
            req = res.data["needs_approval"]
            handoff = {
                "kind": "approval", "question": f"Allow {req['tool']}? {req['reason']}\n{json.dumps(req['args'])[:300]}",
                "options": ["Approve", "Deny"], "reason": "approval", "fingerprint": req["fingerprint"],
                "timeout_s": 600, "wait_for": None,
            }
            break
        if not res.ok:
            break
        nxt = actions[i + 1] if i + 1 < len(actions) else None
        if res.changed_view and nxt and "id" in (nxt.get("args") or {}):
            results.append(ToolResult(False, error="skipped: the view changed, so later element ids were stale"))
            break

    # ---- deterministic verification
    errors = [r for r in results if not r.ok and not (r.error or "").startswith("skipped")]
    planner_says_failed = p.get("verify_previous", "").lower().startswith("fail")
    failures = state.get("failures", 0)
    ledger = list(state.get("error_ledger") or [])
    if errors or not actions:
        failures += 1
        for a, r in zip(actions, results):
            if not r.ok:
                ledger.append(f"step {step} [{p.get('phase', '')}] {a['tool']}({json.dumps(a.get('args', {}))[:120]}): {r.error}")
        if not actions:
            ledger.append(f"step {step}: planner returned no actions")
    elif planner_says_failed:
        failures += 1
        ledger.append(f"step {step} [{p.get('phase', '')}] previous step judged failed: {p.get('thought', '')[:200]}")
    else:
        failures = 0

    history = list(state.get("history") or [])
    sig = json.dumps(actions, sort_keys=True)
    repeats = sum(1 for h in history[-3:] if h.get("sig") == sig)
    if repeats >= 3 and not terminal:
        failures = max(failures, MAX_FAILURES - 1)
        ledger.append(f"step {step}: same action repeated {repeats + 1} times")

    history.append({
        "step": step,
        "phase": p.get("phase", ""),
        "thought": p.get("thought", "")[:300],
        "actions": actions,
        "results": [r.summary(200) for r in results],
        "results_full": [f"{a['tool']}: {r.summary(1500)}" for a, r in zip(actions, results)],
        "ok": not errors,
        "sig": sig,
    })

    # ---- run log
    images = {
        "screen": ctx.run.save_image(step, "screen", ctx.screen_image, quality=60),
        "annotated": ctx.run.save_image(step, "agent", _annotated_for_log(ctx, results)),
    }
    snap = ctx.browser_snapshot if state.get("surface") == "browser" else None
    if snap:
        location = f"{snap.title} — {snap.url}"
    else:
        fg = (ctx.desktop_snapshot.foreground or {}) if ctx.desktop_snapshot else {}
        location = f"{fg.get('title', '')} ({fg.get('exe', '')})"
    llm = state.get("llm") or {}
    ctx.run.step({
        "step": step,
        "phase": p.get("phase", ""),
        "surface": state.get("surface"),
        "location": location,
        "thought": p.get("thought", ""),
        "verify_previous": p.get("verify_previous"),
        "actions": actions,
        "results": [{"ok": r.ok, "summary": r.summary(1000), "ms": r.elapsed_ms} for r in results],
        "expect": p.get("expect", ""),
        "verification": "ok" if not errors and not planner_says_failed else f"failure #{failures}",
        "model": llm.get("deployment") or llm.get("error"),
        "llm_ms": llm.get("ms"),
        "observe_ms": ctx.observe_ms,
        "images": {k: v for k, v in images.items() if v},
    })

    update: dict = {
        "step": step,
        "history": history[-HISTORY_KEEP:],
        "notes": list(ctx.notes),
        "failures": failures,
        "error_ledger": ledger[-60:],
        "pending_handoff": None,
    }
    if terminal:
        update["status"] = "done" if terminal[0] == "done" else "failed"
        update["summary"] = terminal[1]
        return update
    if handoff:
        update["pending_handoff"] = handoff
        return update
    if failures >= MAX_FAILURES:
        update["pending_handoff"] = {
            "kind": "stuck", "reason": "stuck",
            "question": f"I'm stuck at '{p.get('phase', '')}' after {failures} failed attempts. Last error: "
                        f"{ledger[-1] if ledger else 'unknown'}. Reply with guidance, or choose an option.",
            "options": ["Retry", "Abort"], "timeout_s": 900, "wait_for": None,
        }
        return update
    if step >= state.get("max_steps", 80):
        update["pending_handoff"] = {
            "kind": "budget", "reason": "step budget",
            "question": f"Reached the step budget ({step}). Continue for another batch of steps?",
            "options": ["Continue", "Abort"], "timeout_s": 600, "wait_for": None,
        }
    return update


async def handoff(state: AgentState) -> dict:
    req = state.get("pending_handoff") or {}
    answer = interrupt(req)  # pauses here; the runner resumes with the human's answer
    ctx = _ctx(state)
    text = str(answer.get("answer", "")) if isinstance(answer, dict) else str(answer)
    source = answer.get("source", "?") if isinstance(answer, dict) else "?"
    low = text.strip().lower()
    update: dict = {"pending_handoff": None, "failures": 0, "handoffs": state.get("handoffs", 0) + 1}
    history = list(state.get("history") or [])
    history.append({"kind": "handoff", "step": state.get("step", 0), "question": req.get("question", ""),
                    "answer": f"{text} (via {source})"})
    update["history"] = history[-HISTORY_KEEP:]
    if req.get("kind") == "approval":
        if low in ("approve", "approved", "yes", "y", "ok"):
            ctx.policy.approve(req["fingerprint"])
            update["approvals"] = (state.get("approvals") or []) + [req["fingerprint"]]
        return update
    if low in ("abort", "cancel", "stop", "no"):
        update["status"] = "cancelled"
        update["summary"] = f"cancelled by human at handoff: {req.get('question', '')[:200]}"
    elif req.get("kind") == "budget" and low == "continue":
        update["max_steps"] = state.get("max_steps", 80) + 40
    elif low == "timeout":
        update["status"] = "failed"
        update["summary"] = "handoff timed out without an answer"
    return update


async def reflect(state: AgentState) -> dict:
    ctx = _ctx(state)
    try:
        from ..skills.reflector import reflect_on_run

        await reflect_on_run(ctx, state)
    except Exception as exc:
        log.error("reflection failed: %s", exc)
    return {}


# ------------------------------------------------------------------ routing

def _after_act(state: AgentState) -> str:
    if state.get("status") in ("done", "failed", "cancelled"):
        return "reflect"
    if state.get("pending_handoff"):
        return "handoff"
    if CONTEXTS[state["run_id"]].cancel.is_set():
        return "reflect"
    return "observe"


def _after_handoff(state: AgentState) -> str:
    if state.get("status") in ("done", "failed", "cancelled"):
        return "reflect"
    return "observe"


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("observe", observe)
    g.add_node("plan", plan)
    g.add_node("act", act)
    g.add_node("handoff", handoff)
    g.add_node("reflect", reflect)
    g.add_edge(START, "observe")
    g.add_edge("observe", "plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges("act", _after_act, {"observe": "observe", "handoff": "handoff", "reflect": "reflect"})
    g.add_conditional_edges("handoff", _after_handoff, {"observe": "observe", "reflect": "reflect"})
    g.add_edge("reflect", END)
    return g.compile(checkpointer=checkpointer)
