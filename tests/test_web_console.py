"""The web console serves state, queues tasks, answers handoffs and only serves run files."""

import asyncio
import json
import socket
from types import SimpleNamespace

import httpx

from harness.channels.web import WebChannel


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeDaemon:
    def __init__(self, settings):
        self.settings = settings
        self.queued: list[tuple[str, str | None, str]] = []
        self._run_task = None
        self._current_job = None
        self._started = 0
        self.runner = SimpleNamespace(current_state={})
        self.scheduler = SimpleNamespace(get_jobs=lambda: [])
        self.queue = SimpleNamespace(qsize=lambda: len(self.queued))

    def enqueue(self, task, skill=None, trigger="chat", priority=5):
        self.queued.append((task, skill, trigger))
        return len(self.queued)

    async def on_command(self, cmd, args, ch):
        return "Nothing is running." if cmd == "stop" else None


async def test_console_state_task_and_handoff(workspace):
    run = workspace.runs_dir / "20260101_000000_demo"
    (run / "steps").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": run.name, "task": "demo", "status": "done", "steps": 1}))
    (run / "trace.jsonl").write_text(json.dumps({"type": "step", "step": 1, "thought": "look"}) + "\n")
    (run / "steps" / "0001_agent.jpg").write_bytes(b"jpg")
    (workspace.workspace / "secret.txt").write_text("not for the web")

    daemon = FakeDaemon(workspace)
    web = WebChannel(daemon, _free_port())
    await web.start()
    base = f"http://127.0.0.1:{web.port}"
    try:
        async with httpx.AsyncClient(base_url=base) as http:
            assert "Harness Console" in (await http.get("/")).text
            state = (await http.get("/api/state")).json()
            assert state["running"] is None and state["runs"][0]["run_id"] == run.name

            r = await http.post("/api/task", json={"task": "tidy my downloads", "skill": ""})
            assert r.json()["queued"] == 1 and daemon.queued == [("tidy my downloads", None, "web")]
            assert (await http.post("/api/task", json={"task": "  "})).status_code == 400

            recs = (await http.get(f"/api/run/{run.name}?after=0")).json()
            assert recs["next"] == 1 and recs["records"][0]["thought"] == "look"
            assert (await http.get(f"/runs/{run.name}/steps/0001_agent.jpg")).content == b"jpg"
            for bad in (f"/runs/{run.name}/../../secret.txt", "/runs/../secret.txt", "/api/run/..%2Fsecret"):
                assert (await http.get(bad)).status_code == 404

            # A handoff question appears with its picture and is answered from the page.
            ask = asyncio.create_task(web.ask("Approve the payment?", ["Paid", "Cancel"], b"img", timeout_s=10))
            await asyncio.sleep(0.05)
            pending = (await http.get("/api/state")).json()["asks"]
            assert pending[0]["question"] == "Approve the payment?" and pending[0]["image"]
            assert (await http.get(f"/api/ask/{pending[0]['id']}.jpg")).content == b"img"
            assert (await http.post("/api/answer", json={"id": pending[0]["id"], "answer": "Cancel"})).json()["ok"]
            assert await ask == "Cancel"
            assert (await http.get("/api/state")).json()["asks"] == []
            assert (await http.post("/api/answer", json={"id": pending[0]["id"], "answer": "Paid"})).status_code == 404
    finally:
        await web.stop()
