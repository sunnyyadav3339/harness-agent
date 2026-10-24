"""Long-running supervisor: one task queue fed by schedules, chat and email.

The computer is a single shared resource, so runs execute strictly one at a
time. Trigger definitions live in ``<workspace>/config/triggers.yaml`` and are
re-read every 30 s, so schedules can change after the code freeze.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .. import platform_win
from ..channels import build_channel
from ..config import Settings, load_workspace_yaml
from ..llm.router import get_router
from ..runtime.runner import Runner
from ..skills.loader import SkillLibrary
from ..timeutil import CLOCK, IST, parse_target
from .email_imap import ImapWatcher, MailEvent, start_watcher

log = logging.getLogger(__name__)


@dataclass(order=True)
class Job:
    priority: int
    seq: int
    task: str = field(compare=False)
    skill: str | None = field(compare=False, default=None)
    trigger: str = field(compare=False, default="chat")


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.channel = build_channel(settings, console=False)
        self.web = None
        if settings.web_port:
            from ..channels.web import WebChannel

            self.web = WebChannel(self, settings.web_port)
            self.channel.channels.append(self.web)
        self.channel.on_command = self.on_command
        self.runner = Runner(settings, self.channel)
        self.queue: asyncio.PriorityQueue[Job] = asyncio.PriorityQueue()
        self.scheduler = AsyncIOScheduler(timezone=IST)
        self._seq = 0
        self._triggers_sig = None
        self._run_task: asyncio.Task | None = None
        self._current_job: Job | None = None
        self._watchers: list[ImapWatcher] = []
        self._started = time.time()

    # ------------------------------------------------------------ queue
    def enqueue(self, task: str, skill: str | None = None, trigger: str = "chat", priority: int = 5) -> int:
        self._seq += 1
        self.queue.put_nowait(Job(priority, self._seq, task, skill, trigger))
        log.info("queued #%d (%s): %s", self._seq, trigger, task[:100])
        return self._seq

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            self._current_job = job
            self._run_task = asyncio.create_task(self.runner.run(job.task, job.skill, trigger=job.trigger))
            try:
                outcome = await self._run_task
                log.info("run %s finished: %s", outcome.run_id, outcome.status)
            except asyncio.CancelledError:
                log.info("run cancelled")
            except Exception:
                log.exception("run failed")
            finally:
                self._run_task = None
                self._current_job = None

    # ------------------------------------------------------------ triggers
    def _sync_schedules(self) -> None:
        cfg = load_workspace_yaml("triggers.yaml", {}) or {}
        sig = json.dumps(cfg, sort_keys=True, default=str)
        if sig == self._triggers_sig:
            return
        self._triggers_sig = sig
        for job in self.scheduler.get_jobs():
            if job.id.startswith("sched:") or job.id.startswith("prewarm:"):
                job.remove()
        now = CLOCK.now()
        for item in cfg.get("schedules", []) or []:
            if not item.get("enabled", True):
                continue
            sid = item["id"]
            try:
                if item.get("cron"):
                    trig = CronTrigger.from_crontab(item["cron"], timezone=IST)
                    run_at = None
                else:
                    run_at = parse_target(str(item["at"]))
                    if run_at < now:
                        log.info("schedule %s at %s is in the past; skipped", sid, run_at)
                        continue
                    trig = DateTrigger(run_date=run_at)
            except Exception as exc:
                log.error("bad schedule %s: %s", sid, exc)
                continue
            self.scheduler.add_job(
                self.enqueue, trig, id=f"sched:{sid}", replace_existing=True, misfire_grace_time=120, coalesce=True,
                kwargs={"task": item["task"], "skill": item.get("skill"), "trigger": f"schedule:{sid}",
                        "priority": int(item.get("priority", 1))},
            )
            prewarm = int(item.get("prewarm_s", 0))
            if prewarm and run_at:
                warm_at = run_at - timedelta(seconds=prewarm)
                if warm_at > now:
                    self.scheduler.add_job(self.prewarm, DateTrigger(run_date=warm_at), id=f"prewarm:{sid}",
                                           replace_existing=True, misfire_grace_time=60)
            log.info("scheduled %s (%s)", sid, item.get("cron") or run_at)
        self._email_rules = cfg.get("email", []) or []

    async def _watch_config(self) -> None:
        while True:
            try:
                self._sync_schedules()
            except Exception:
                log.exception("triggers.yaml reload failed; keeping previous schedules")
            await asyncio.sleep(30)

    def _on_mail(self, ev: MailEvent) -> None:
        for rule in getattr(self, "_email_rules", []):
            if rule.get("enabled", True) is False:
                continue
            if rule.get("from_contains") and rule["from_contains"].lower() not in ev.sender.lower():
                continue
            if rule.get("subject_contains") and rule["subject_contains"].lower() not in ev.subject.lower():
                continue
            if rule.get("subject_regex") and not re.search(rule["subject_regex"], ev.subject, re.I):
                continue
            task = (rule["task"].replace("{subject}", ev.subject).replace("{from}", ev.sender)
                    .replace("{body}", ev.body[:1500]).replace("{date}", ev.date))
            log.info("email trigger %s matched: %s", rule.get("id"), ev.subject)
            self.enqueue(task, rule.get("skill"), trigger=f"email:{rule.get('id', '?')}", priority=3)

    async def prewarm(self) -> None:
        """Before a precise schedule: fresh NTP, warm TLS to model APIs, attach Chrome, load OCR."""
        log.info("pre-warming for upcoming scheduled run")
        await CLOCK.sync(self.settings.ntp_server)
        results = await get_router().warmup()
        bad = {k: v for k, v in results.items() if v != "ok"}
        try:
            await self.runner.browser.ensure()
        except Exception as exc:
            bad["browser"] = str(exc)
        try:
            from ..tools.vision import warm_ocr

            await asyncio.to_thread(warm_ocr)
        except Exception as exc:
            bad["ocr"] = str(exc)
        msg = "🔥 Pre-warm done. Clock offset {:.0f} ms.".format(CLOCK.offset * 1000)
        if bad:
            msg += "\nIssues: " + "; ".join(f"{k}: {v}" for k, v in bad.items())
        await self.channel.send(msg)

    # ------------------------------------------------------------ chat commands
    async def on_command(self, cmd: str, args: str, ch) -> str | None:
        if cmd in ("start", "help"):
            return ("Commands:\n/run <task> - start a task (or just type it)\n/status - what I'm doing\n"
                    "/stop - cancel the current run\n/skills - list skills\n/jobs - scheduled jobs\n"
                    "/clock - NTP-corrected time")
        if cmd == "run":
            if not args:
                return "Usage: /run <what to do>"
            skill = None
            m = re.match(r"skill:(\S+)\s+(.*)", args)
            if m:
                skill, args = m.group(1), m.group(2)
            n = self.enqueue(args, skill, trigger="chat")
            busy = " (after the current run)" if self._run_task else ""
            return f"Queued #{n}{busy}."
        if cmd == "status":
            lines = [f"Up {int(time.time() - self._started)}s, queue {self.queue.qsize()}."]
            if self._current_job:
                st = self.runner.current_state
                lines.append(f"Running: {self._current_job.task[:120]}\nstep {st.get('step', 0)}, phase {st.get('phase', '?')}")
            else:
                lines.append("Idle.")
            return "\n".join(lines)
        if cmd == "stop":
            if not self._run_task:
                return "Nothing is running."
            # Soft stop between actions first; hard-cancel if a long tool call is still blocking.
            self.runner.cancel()
            task = self._run_task
            asyncio.get_running_loop().call_later(8, lambda: task.done() or task.cancel())
            return "Stopping the current run."
        if cmd == "skills":
            skills = SkillLibrary(self.settings.skills_dir).all()
            return "\n".join(f"• {s.name}{' (draft)' if s.draft else ''}: {s.description[:90]}" for s in skills) or "No skills."
        if cmd == "jobs":
            jobs = self.scheduler.get_jobs()
            return "\n".join(f"• {j.id}: {j.next_run_time:%Y-%m-%d %H:%M:%S}" for j in jobs if j.next_run_time) or "No jobs."
        if cmd == "clock":
            st = CLOCK.status()
            return f"{CLOCK.now():%H:%M:%S.%f}"[:-3] + f" IST (offset {st['offset_ms']} ms, server {st['server']})"
        return f"Unknown command /{cmd}. Try /help."

    # ------------------------------------------------------------ lifecycle
    async def serve(self) -> None:
        self.settings.ensure_dirs()
        platform_win.keep_awake(True)
        await CLOCK.sync(self.settings.ntp_server)
        await self.channel.start()
        self._sync_schedules()
        self.scheduler.add_job(CLOCK.sync, "interval", minutes=10, id="ntp-resync",
                               kwargs={"server": self.settings.ntp_server})
        self.scheduler.start()

        background = [asyncio.create_task(self._worker()), asyncio.create_task(self._watch_config()),
                      asyncio.create_task(self._heartbeat())]
        if self.settings.imap_user and (self.settings.imap_password or self.settings.imap_oauth_token):
            watcher = ImapWatcher(self.settings.imap_host, self.settings.imap_port, self.settings.imap_user,
                                  self.settings.imap_password, self.settings.imap_oauth_token,
                                  state_file=self.settings.state_dir / "imap_state.json")
            self._watchers.append(watcher)
            background.append(await start_watcher(watcher, self._on_mail))
        console = f" Console: http://127.0.0.1:{self.settings.web_port}" if self.web else ""
        await self.channel.send(f"🟢 Harness daemon online. {len(self.scheduler.get_jobs())} job(s). /help for commands.{console}")
        try:
            await asyncio.gather(*background)
        finally:
            for w in self._watchers:
                w.stop()
            self.scheduler.shutdown(wait=False)
            await self.channel.stop()
            platform_win.keep_awake(False)

    async def _heartbeat(self) -> None:
        path = self.settings.state_dir / "daemon_heartbeat.json"
        while True:
            path.write_text(json.dumps({
                "ts": datetime.now(IST).isoformat(), "queue": self.queue.qsize(),
                "running": self._current_job.task[:200] if self._current_job else None,
                "clock": CLOCK.status(),
            }))
            await asyncio.sleep(15)
