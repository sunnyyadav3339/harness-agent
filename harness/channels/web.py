"""Local web console: give tasks, watch runs live, answer handoffs, replay past runs.

Served by the daemon on 127.0.0.1 only (``web_port``, default 8765), with no extra
dependencies: a small asyncio HTTP/1.1 server. It is also a handoff channel, so a
question shows up here with its screenshot and buttons at the same time as on
Telegram; the first answer from any channel wins.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from .base import Channel

if TYPE_CHECKING:
    from ..triggers.daemon import Daemon

log = logging.getLogger(__name__)

_PAGE = Path(__file__).with_name("web_console.html")
_MAX_BODY = 64 * 1024
_STATUS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
           500: "Internal Server Error"}


class WebChannel(Channel):
    name = "web"

    def __init__(self, daemon: "Daemon", port: int, host: str = "127.0.0.1") -> None:
        super().__init__()
        self.daemon = daemon
        self.host, self.port = host, port
        self._server: asyncio.base_events.Server | None = None
        self._ids = itertools.count(1)
        self._asks: dict[str, dict] = {}
        self.feed: list[dict] = []

    # ---------------------------------------------------------------- channel
    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(self._handle, self.host, self.port)
            log.info("web console on http://%s:%d", self.host, self.port)
        except OSError as exc:  # never let the console take the daemon down
            log.error("web console could not listen on %s:%d: %s", self.host, self.port, exc)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def send(self, text: str, image: bytes | None = None) -> None:
        self.feed.append({"ts": time.time(), "text": text})
        del self.feed[:-60]

    async def ask(self, question: str, options: list[str], image: bytes | None = None,
                  timeout_s: float = 600) -> str | None:
        ask_id = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._asks[ask_id] = {"question": question, "options": list(options), "image": image, "fut": fut,
                              "ts": time.time(), "timeout_s": timeout_s}
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
        finally:  # answered here, elsewhere, by the screen, or timed out: the card goes away
            self._asks.pop(ask_id, None)

    # ---------------------------------------------------------------- http
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            length = int(headers.get("content-length") or 0)
            if length > _MAX_BODY:
                await self._reply(writer, 413, {"error": "body too large"})
                return
            body = await reader.readexactly(length) if length else b""
            status, payload, ctype = await self._route(method, target, body)
            await self._reply(writer, status, payload, ctype)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        except Exception as exc:
            log.exception("web console request failed")
            try:
                await self._reply(writer, 500, {"error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    async def _reply(writer, status: int, payload, ctype: str | None = None) -> None:
        if isinstance(payload, (dict, list)):
            data, ctype = json.dumps(payload, ensure_ascii=False, default=str).encode(), "application/json"
        else:
            data = payload if isinstance(payload, bytes) else str(payload).encode()
            ctype = ctype or "text/plain; charset=utf-8"
        writer.write((f"HTTP/1.1 {status} {_STATUS.get(status, 'OK')}\r\nContent-Type: {ctype}\r\n"
                      f"Content-Length: {len(data)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").encode()
                     + data)
        await writer.drain()

    async def _route(self, method: str, target: str, body: bytes):
        url = urlsplit(target)
        path, query = unquote(url.path), parse_qs(url.query)
        if method == "GET":
            if path in ("/", "/index.html"):
                return 200, _PAGE.read_bytes(), "text/html; charset=utf-8"
            if path == "/api/state":
                return 200, self._state(), None
            if path.startswith("/api/run/"):
                return self._run_records(path[len("/api/run/"):], int((query.get("after") or ["0"])[0]))
            if path.startswith("/api/ask/") and path.endswith(".jpg"):
                ask = self._asks.get(path[len("/api/ask/"):-4])
                if ask and ask["image"]:
                    return 200, ask["image"], "image/jpeg"
                return 404, {"error": "no image"}, None
            if path.startswith("/runs/"):
                return self._static(path[len("/runs/"):])
            return 404, {"error": "not found"}, None
        if method != "POST":
            return 405, {"error": "method not allowed"}, None
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, {"error": "invalid JSON"}, None
        if path == "/api/task":
            task = str(data.get("task") or "").strip()
            if not task:
                return 400, {"error": "empty task"}, None
            n = self.daemon.enqueue(task, (data.get("skill") or None), trigger="web")
            return 200, {"queued": n, "busy": self.daemon._run_task is not None}, None
        if path == "/api/answer":
            ask = self._asks.get(str(data.get("id")))
            if not ask or ask["fut"].done():
                return 404, {"error": "that question is no longer open"}, None
            ask["fut"].set_result(str(data.get("answer") or ""))
            return 200, {"ok": True}, None
        if path == "/api/stop":
            return 200, {"message": await self.daemon.on_command("stop", "", self)}, None
        return 404, {"error": "not found"}, None

    # ---------------------------------------------------------------- data
    def _state(self) -> dict:
        from ..runtime.context import CONTEXTS
        from ..skills.loader import SkillLibrary
        from ..timeutil import CLOCK

        d = self.daemon
        running = None
        if d._current_job:
            st = d.runner.current_state
            running = {"task": d._current_job.task, "trigger": d._current_job.trigger,
                       "run_id": next(iter(CONTEXTS), None), "step": st.get("step", 0), "phase": st.get("phase", "")}
        try:
            skills = [{"name": s.name, "description": s.description, "draft": s.draft}
                      for s in SkillLibrary(d.settings.skills_dir).all()]
        except Exception:
            skills = []
        jobs = [{"id": j.id, "next": f"{j.next_run_time:%Y-%m-%d %H:%M:%S}"}
                for j in d.scheduler.get_jobs() if j.next_run_time and j.id.startswith("sched:")]
        return {
            "clock": f"{CLOCK.now():%Y-%m-%d %H:%M:%S}", "uptime_s": int(time.time() - d._started),
            "running": running, "queue": d.queue.qsize(), "jobs": jobs, "skills": skills, "runs": self._runs(),
            "asks": [{"id": k, "question": a["question"], "options": a["options"], "image": a["image"] is not None,
                      "age_s": int(time.time() - a["ts"])} for k, a in self._asks.items()],
            "feed": self.feed[-25:],
        }

    def _runs(self, limit: int = 20) -> list[dict]:
        from ..runtime.context import CONTEXTS

        root = self.daemon.settings.runs_dir
        dirs = sorted((p for p in root.glob("*") if (p / "run.json").exists()), key=lambda p: p.name, reverse=True)
        out = []
        for p in dirs[:limit]:
            try:
                m = json.loads((p / "run.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            status = m.get("status")
            if status == "running" and p.name not in CONTEXTS:  # its process died (crash, power cut, killed)
                status = "interrupted"
            out.append({"run_id": p.name, "task": m.get("task", ""), "status": status,
                        "skill": m.get("skill"), "trigger": m.get("trigger"), "steps": m.get("steps", 0),
                        "handoffs": m.get("handoffs", 0), "started": (m.get("started") or {}).get("ist", "")[:19],
                        "summary": m.get("summary", ""), "replay": (p / "replay.html").exists()})
        return out

    def _run_dir(self, run_id: str) -> Path | None:
        root = self.daemon.settings.runs_dir.resolve()
        p = (root / run_id).resolve()
        return p if p.parent == root and (p / "run.json").exists() else None

    def _run_records(self, run_id: str, after: int):
        run_dir = self._run_dir(run_id)
        if run_dir is None:
            return 404, {"error": "no such run"}, None
        records = []
        trace = run_dir / "trace.jsonl"
        if trace.exists():
            for line in trace.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # a line being written right now
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        return 200, {"meta": meta, "records": records[after:], "next": len(records)}, None

    def _static(self, rel: str):
        run_id, _, sub = rel.partition("/")
        run_dir = self._run_dir(run_id)
        if run_dir is None or not sub:
            return 404, {"error": "not found"}, None
        f = (run_dir / sub).resolve()
        if run_dir not in f.parents or not f.is_file():
            return 404, {"error": "not found"}, None
        if f.stat().st_size > 200 * 1024 * 1024:
            return 413, {"error": "file too large to serve; open it from the run folder"}, None
        return 200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream"
