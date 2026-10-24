"""Skill guards are enforced by the harness, including inside macros."""

import harness.tools  # noqa: F401  (registers tools)
from harness.credentials import SECRETS
from harness.llm.router import Router
from harness.policy import Policy
from harness.runlog import RunLogger
from harness.runtime.context import RunContext
from harness.skills.loader import parse_skill_text
from harness.tools import REGISTRY
from harness.tools.browser import BrowserManager

SKILL = """---
name: guarded
description: test skill with guards
guards:
  - name: no_pay_in_rehearsal
    when: {task_contains: "REHEARSAL", click_text: "Pay & Book"}
    block: "never pay in a rehearsal"
  - name: must_check
    when: {tool: task.note, task_contains: "CHECKED"}
    run_macro: check
---
body
```yaml macros
check:
  steps:
    - {tool: file.read, args: {path: "{{param.path}}"}}
  defaults: {path: "Z:/definitely/missing.txt"}
pay:
  steps:
    - {tool: browser.click, args: {text: "Pay & Book"}}
```
"""


def _ctx(workspace, task):
    skill = parse_skill_text(SKILL, workspace.skills_dir / "guarded" / "SKILL.md")
    ctx = RunContext(settings=workspace, run=RunLogger(workspace.runs_dir, "guard-test", task, skill.name, "test"),
                     browser=BrowserManager(workspace), router=Router(config={"groups": {}}), policy=Policy(),
                     secrets=SECRETS, skill=skill)
    ctx.extra["task"] = task
    return ctx


async def test_block_guard_stops_a_click_before_the_browser_is_touched(workspace):
    ctx = _ctx(workspace, "REHEARSAL: book a ticket")
    res = await REGISTRY.call("browser.click", {"text": "Pay & Book"}, ctx)
    assert not res.ok and "no_pay_in_rehearsal" in res.error
    assert not ctx.browser.connected  # never even attached to Chrome


async def test_block_guard_also_applies_inside_macros(workspace):
    ctx = _ctx(workspace, "REHEARSAL: book a ticket")
    res = await REGISTRY.call("skill.run_macro", {"name": "pay"}, ctx)
    assert not res.ok and "no_pay_in_rehearsal" in res.output


async def test_guard_is_inactive_when_conditions_do_not_match(workspace):
    ctx = _ctx(workspace, "real booking")
    res = await REGISTRY.call("task.note", {"text": "hello"}, ctx)
    assert res.ok


async def test_failed_check_macro_blocks_the_action(workspace):
    ctx = _ctx(workspace, "CHECKED run")
    res = await REGISTRY.call("task.note", {"text": "hello"}, ctx)
    assert not res.ok and "must_check" in res.error
    assert ctx.notes == []  # the guarded action did not run
