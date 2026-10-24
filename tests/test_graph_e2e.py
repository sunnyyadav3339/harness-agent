"""Full loop with a scripted model: tools, batching, notes, handoff, logging, replay, reflection."""

import json

from PIL import Image

import harness.tools  # noqa: F401  (registers tools)
from harness.channels.base import Channel
from harness.llm.router import LLMResponse
from harness.perception.desktop_view import DesktopSnapshot
from harness.runtime import graph as graph_mod
from harness.runtime import runner as runner_mod
from harness.runtime.runner import Runner


class ScriptedRouter:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def has_group(self, group):
        return True

    def group_has_vision(self, group):
        return False

    async def complete(self, group, messages, **kw):
        self.prompts.append(messages)
        text = self.replies.pop(0)
        return LLMResponse(text=text, deployment=f"{group}:fake#1", model="fake", latency_ms=1)

    def stats(self):
        return []

    async def aclose(self):
        pass


class FakeChannel(Channel):
    def __init__(self, answer="Yes"):
        super().__init__()
        self.sent, self.asked = [], []
        self.answer = answer

    async def send(self, text, image=None):
        self.sent.append(text)

    async def ask(self, question, options, image=None, timeout_s=600):
        self.asked.append(question)
        return self.answer


def _fake_desktop(monkeypatch):
    img = Image.new("RGB", (320, 200), "white")

    async def snapshot(*a, **k):
        return DesktopSnapshot(foreground={"hwnd": 1, "title": "Test", "exe": "test.exe"}, image=img)

    monkeypatch.setattr(graph_mod.desktop_view, "snapshot", snapshot)
    monkeypatch.setattr(runner_mod, "grab", lambda: img)


async def test_end_to_end_run(workspace, tmp_path, monkeypatch):
    _fake_desktop(monkeypatch)
    (workspace.data_dir / "common.yaml").write_text("name: Asha\n", encoding="utf-8")
    out_file = tmp_path / "out.txt"
    plans = [
        {"verify_previous": "n/a", "thought": "write the file", "phase": "WRITE", "expect": "file exists",
         "actions": [{"tool": "file.write", "args": {"path": str(out_file), "content": "hello {{vault.common.name}}"}},
                     {"tool": "task.note", "args": {"text": "file written"}}]},
        {"verify_previous": "success", "thought": "confirm", "phase": "ASK",
         "actions": [{"tool": "human.ask", "args": {"question": "OK to finish?", "options": ["Yes", "No"]}}]},
        {"verify_previous": "success", "thought": "finish", "phase": "DONE",
         "actions": [{"tool": "task.done", "args": {"summary": "wrote the greeting"}}]},
    ]
    draft = {"skill_md": "---\nname: write_greeting\ndescription: write a greeting file\ntriggers: [greeting]\n"
                         "draft: true\n---\n# Write greeting\n## Steps\n1. file.write\n"}
    router = ScriptedRouter([json.dumps(p) for p in plans] + [json.dumps(draft)])
    channel = FakeChannel()
    runner = Runner(workspace, channel=channel, router=router)

    outcome = await runner.run("write a hello file for the user", trigger="test", record=False)

    assert outcome.status == "done", outcome.summary
    assert outcome.summary == "wrote the greeting"
    assert out_file.read_text() == "hello Asha"
    assert channel.asked == ["OK to finish?"]
    assert outcome.handoffs == 1

    run_dir = workspace.runs_dir / outcome.run_id
    records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    steps = [r for r in records if r["type"] == "step"]
    events = [r["event"] for r in records if r["type"] == "event"]
    assert [s["phase"] for s in steps] == ["WRITE", "ASK", "DONE"]
    assert steps[0]["results"][0]["ok"] and steps[0]["images"]["annotated"]
    assert "handoff" in events and "handoff_resolved" in events and "finished" in events
    assert (run_dir / "replay.html").exists()
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "done" and meta["handoffs"] == 1

    # the second planner prompt saw the note and the first step's result
    content = router.prompts[1][1]["content"]
    # desktop surface -> vision turn: text part + annotated screenshot
    assert isinstance(content, list) and content[1]["type"] == "image_url"
    second_prompt = content[0]["text"]
    assert "file written" in second_prompt and "wrote 10 chars" in second_prompt
    # zero-shot success became a draft skill
    assert (workspace.skills_dir / "write_greeting" / "SKILL.md").exists()


async def test_abort_at_stuck_handoff_ends_cleanly(workspace, monkeypatch):
    """Regression: answering Abort crashed the run with KeyError: 'run_id' (rehearsal 24-Oct 16:16)."""
    _fake_desktop(monkeypatch)
    bad = {"thought": "try", "phase": "LOOP", "actions": [{"tool": "file.read", "args": {"path": "Z:/nope.txt"}}]}
    router = ScriptedRouter([json.dumps(bad)] * 4 + ['{"heuristics": []}'])
    channel = FakeChannel(answer="Abort")
    outcome = await Runner(workspace, channel=channel, router=router).run("read a file", trigger="test", record=False)
    assert outcome.status == "cancelled", outcome.summary
    assert "cancelled by human" in outcome.summary


async def test_repeated_failures_trigger_stuck_handoff(workspace, monkeypatch):
    _fake_desktop(monkeypatch)
    bad = {"thought": "try", "phase": "LOOP", "actions": [{"tool": "file.read", "args": {"path": "Z:/nope.txt"}}]}
    done = {"thought": "give up", "phase": "END", "actions": [{"tool": "task.fail", "args": {"reason": "file missing"}}]}
    router = ScriptedRouter([json.dumps(bad)] * 4 + [json.dumps(done)])
    channel = FakeChannel()
    outcome = await Runner(workspace, channel=channel, router=router).run("read a file", trigger="test", record=False)
    assert outcome.status == "failed"
    assert len(channel.asked) == 1 and "stuck" in channel.asked[0]
