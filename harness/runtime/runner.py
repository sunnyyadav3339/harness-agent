"""Runs one task end to end: skill routing, graph execution, handoffs, logging.

Checkpoints go to a machine-local SQLite file keyed by run_id, so
``python -m harness resume <run_id>`` continues a crashed run from its last
completed node.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .. import platform_win
from ..channels.base import Channel, MultiChannel
from ..config import Settings, load_workspace_yaml
from ..credentials import SECRETS
from ..llm.router import Router, get_router
from ..perception.annotate import fit, to_jpeg
from ..perception.screen import grab
from ..policy import Policy
from ..runlog import RunLogger, ScreenRecorder
from ..skills.loader import SkillLibrary, load_vault
from ..timeutil import CLOCK
from ..tools.browser import BrowserManager
from .context import CONTEXTS, AgentState, RunContext
from .graph import build_graph

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    run_id: str
    status: str
    summary: str
    run_dir: str
    steps: int
    handoffs: int
    seconds: float


def _slug(text: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "task"


class Runner:
    def __init__(self, settings: Settings, channel: Channel | None = None, browser: BrowserManager | None = None,
                 router: Router | None = None) -> None:
        self.settings = settings
        self.channel = channel or MultiChannel([])
        self.browser = browser or BrowserManager(settings)
        self.router = router or get_router()
        self.current: RunContext | None = None
        self.current_state: dict = {}

    def cancel(self) -> bool:
        if self.current:
            self.current.cancel.set()
            return True
        return False

    async def run(self, task: str, skill_name: str | None = None, trigger: str = "cli",
                  resume_run_id: str | None = None, record: bool | None = None) -> RunOutcome:
        settings = self.settings
        settings.ensure_dirs()
        library = SkillLibrary(settings.skills_dir)

        prior: dict | None = None
        if resume_run_id:
            prior = await self._load_checkpoint(resume_run_id)
            if not prior:
                raise RuntimeError(f"no checkpoint for run {resume_run_id}")
            task, skill_name = prior["task"], prior.get("skill_name")

        if skill_name:
            skill = library.get(skill_name)
            if skill is None:
                raise RuntimeError(f"skill {skill_name!r} not found in {settings.skills_dir}")
            ranking = []
        else:
            skill, ranking = library.match(task)
        run_id = resume_run_id or f"{CLOCK.now():%Y%m%d_%H%M%S}_{_slug(skill.name if skill else task)}"
        logger = RunLogger(settings.runs_dir, run_id, task, skill.name if skill else None, trigger)
        logger.event("started", task=task, skill=skill.name if skill else None, ranking=ranking[:5],
                     resumed=bool(resume_run_id), clock=CLOCK.status())

        ctx = RunContext(
            settings=settings, run=logger, browser=self.browser, router=self.router, policy=Policy(),
            secrets=SECRETS, skill=skill, vault=load_vault(settings.workspace, skill), channel=self.channel,
            other_skills=[(s.name, s.description) for s in library.all() if not skill or s.name != skill.name],
        )
        ctx.extra["task"] = task
        if prior:
            ctx.notes = list(prior.get("notes") or [])
            for fp in prior.get("approvals") or []:
                ctx.policy.approve(fp)
        CONTEXTS[run_id] = ctx
        self.current = ctx

        run_cfg = load_workspace_yaml("runtime.yaml", {}) or {}
        recorder = None
        if record if record is not None else run_cfg.get("record_screen", True):
            recorder = ScreenRecorder(logger.dir / "video", fps=int(run_cfg.get("record_fps", 10)))
            if not recorder.start():
                recorder = None

        platform_win.keep_awake(True)
        started = time.time()
        await self._notify(f"▶️ Run started: {task[:200]}\nskill: {skill.name if skill else 'none (zero-shot)'}\nid: {run_id}")
        status, summary = "failed", ""
        state: dict = {}
        try:
            state = await self._drive(ctx, run_id, task, skill, trigger, resume=bool(prior))
            status = state.get("status", "failed")
            if status == "running":
                status = "cancelled" if ctx.cancel.is_set() else "failed"
            summary = state.get("summary", "")
        except asyncio.CancelledError:
            status, summary = "cancelled", "run cancelled"
            raise
        except Exception as exc:
            log.exception("run %s crashed", run_id)
            status, summary = "error", f"{exc.__class__.__name__}: {exc}"
            logger.event("error", message=summary)
        finally:
            if recorder:
                await asyncio.to_thread(recorder.stop)
            final_png = None
            try:
                shot = await asyncio.to_thread(grab)
                shot.save(logger.dir / "final_screen.png")
                final_png = to_jpeg(fit(shot, 1280, 1280)[0], 70)
            except Exception:
                pass
            logger.finish(status, summary, {"seconds": round(time.time() - started, 1),
                                            "router": self.router.stats()})
            logger.close()
            CONTEXTS.pop(run_id, None)
            self.current = None
            platform_win.keep_awake(False)
            emoji = {"done": "✅", "failed": "❌", "cancelled": "⏹", "error": "💥"}.get(status, "ℹ️")
            await self._notify(f"{emoji} Run {status}: {summary[:800]}\nlog: {logger.dir}", final_png)

        return RunOutcome(run_id, status, summary, str(logger.dir), state.get("step", 0),
                          state.get("handoffs", 0), round(time.time() - started, 1))

    async def _drive(self, ctx: RunContext, run_id: str, task: str, skill, trigger: str, resume: bool) -> dict:
        db = self.settings.checkpoint_db(run_id)
        db.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
            graph = build_graph(saver)
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100_000}
            initial: AgentState = {
                "run_id": run_id, "task": task, "skill_name": skill.name if skill else None, "trigger": trigger,
                "step": 0, "max_steps": int(skill.max_steps or 80) if skill else 60, "phase": "start",
                "history": [], "notes": [], "status": "running", "summary": "", "pending_handoff": None,
                "approvals": [], "failures": 0, "error_ledger": [], "need_vision": False, "handoffs": 0, "recoveries": 0,
            }
            payload = None if resume else initial
            while True:
                async for chunk in graph.astream(payload, config, stream_mode="updates"):
                    for node, update in chunk.items():
                        if isinstance(update, dict):
                            self.current_state.update(update)
                    if ctx.cancel.is_set():
                        break
                snap = await graph.aget_state(config)
                interrupts = list(getattr(snap, "interrupts", None) or [])
                if not interrupts:
                    for t in snap.tasks or []:
                        interrupts += list(t.interrupts or [])
                if interrupts and not ctx.cancel.is_set():
                    answer = await self.resolve_handoff(ctx, interrupts[0].value)
                    payload = Command(resume=answer)
                    continue
                return dict(snap.values)

    async def resolve_handoff(self, ctx: RunContext, req: dict) -> dict:
        question = req.get("question", "Need your input")
        options = req.get("options") or ["Done", "Cancel"]
        timeout = float(req.get("timeout_s", 600))
        img_bytes = None
        img_rel = None
        try:
            shot = await asyncio.to_thread(grab)
            step = ctx.run.meta.get("steps", 0)
            img_rel = ctx.run.save_image(step, "handoff", shot, quality=80)
            img_bytes = to_jpeg(fit(shot, 1600, 1600)[0], 80)
        except Exception:
            pass
        ctx.run.event("handoff", question=question, options=options, reason=req.get("reason"),
                      wait_for=req.get("wait_for"), image=img_rel)
        log.info("HANDOFF (%s) waiting up to %.0fs: %s", req.get("reason"), timeout, question[:200])
        t0 = time.time()

        tasks: dict[asyncio.Task, str] = {}
        tasks[asyncio.create_task(self.channel.ask(question, options, img_bytes, timeout))] = "human"
        if req.get("wait_for"):
            tasks[asyncio.create_task(self._watch(ctx, req["wait_for"], timeout))] = "screen"
        tasks[asyncio.create_task(self._wait_cancel(ctx))] = "cancel"
        answer, source = "timeout", "timeout"
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, timeout=timeout + 5, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                hit = None
                for t in done:
                    if not t.cancelled() and t.exception() is None and t.result() is not None:
                        hit = t
                        break
                if hit is not None:
                    answer, source = str(hit.result()), tasks[hit]
                    break
        finally:
            for t in tasks:
                t.cancel()
        if source == "cancel":
            answer = "Abort"
        waited = round(time.time() - t0, 1)
        ctx.run.event("handoff_resolved", answer=answer, source=source, waited_s=waited)
        log.info("HANDOFF resolved via %s after %.1fs: %s", source, waited, answer[:100])
        if source == "screen":
            await self._notify(f"👀 Detected on screen, continuing: {req.get('wait_for')}")
        return {"answer": answer, "source": source, "waited_s": waited}

    async def _wait_cancel(self, ctx: RunContext):
        await ctx.cancel.wait()
        return "Abort"

    async def _watch(self, ctx: RunContext, cond: dict, timeout: float):
        """Resolve a handoff automatically when the screen reaches the expected state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if ctx.browser.connected:
                    page = await ctx.browser.page()
                    if cond.get("url_contains") and cond["url_contains"] in page.url:
                        return f"screen: url contains {cond['url_contains']}"
                    if cond.get("text"):
                        if await page.get_by_text(cond["text"], exact=False).count() > 0:
                            return f"screen: text '{cond['text']}' visible"
                if cond.get("window_title"):
                    fg = platform_win.foreground_window()
                    if fg and cond["window_title"].lower() in fg.title.lower():
                        return f"screen: window '{fg.title}'"
            except Exception:
                pass
            await asyncio.sleep(1.0)
        return None

    async def _notify(self, text: str, image: bytes | None = None) -> None:
        try:
            await self.channel.send(text, image)
        except Exception as exc:
            log.warning("notify failed: %s", exc)

    async def _load_checkpoint(self, run_id: str) -> dict | None:
        db = self.settings.checkpoint_db(run_id)
        if not db.exists():
            return None
        async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
            graph = build_graph(saver)
            snap = await graph.aget_state({"configurable": {"thread_id": run_id}})
            return dict(snap.values) if snap and snap.values else None
