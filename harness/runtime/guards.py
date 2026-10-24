"""Skill-declared guards, enforced by the harness rather than left to the model.

A skill lists guards in its frontmatter; before any tool call (including steps inside
macros) the harness checks whether a guard applies and either runs a verification
macro that must pass, or blocks the action outright:

    guards:
      - name: passenger_verified
        when: {url_contains: "/booking/psgninput", click_text: "Continue"}
        run_macro: verify_passenger
      - name: no_payment_in_rehearsal
        when: {task_contains: "REHEARSAL", click_text: "Pay & Book"}
        block: "REHEARSAL: never click Pay & Book."

`when` keys (all given keys must match, case-insensitive):
  tool          exact tool name (e.g. browser.click)
  macro         skill.run_macro name
  click_text    substring of the clicked element's text (id lookup, text= or selector)
  url_contains  substring of the current browser URL
  task_contains substring of the task text
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..tools.base import ToolResult

if TYPE_CHECKING:
    from .context import RunContext

log = logging.getLogger(__name__)

_CLICK_TOOLS = {"browser.click", "desktop.click"}


def _target_text(ctx: "RunContext", tool: str, args: dict) -> str:
    if tool not in _CLICK_TOOLS:
        return ""
    if args.get("id") is not None:
        snap = ctx.browser_snapshot if tool == "browser.click" else ctx.desktop_snapshot
        try:
            el = snap.by_id(int(args["id"])) if snap else None
        except (TypeError, ValueError):
            el = None
        if el is not None:
            return getattr(el, "text", "") or getattr(el, "name", "")
    return str(args.get("text") or args.get("selector") or "")


def _current_url(ctx: "RunContext") -> str:
    try:
        page = ctx.browser._page
        return page.url if page is not None and not page.is_closed() else ""
    except Exception:
        return ""


def _matches(ctx: "RunContext", when: dict, tool: str, args: dict) -> bool:
    if not when:
        return False
    low = lambda s: str(s or "").lower()  # noqa: E731
    if "tool" in when and low(when["tool"]) != low(tool):
        return False
    if "macro" in when and not (tool == "skill.run_macro" and low(args.get("name")) == low(when["macro"])):
        return False
    if "click_text" in when and low(when["click_text"]) not in low(_target_text(ctx, tool, args)):
        return False
    if "url_contains" in when and low(when["url_contains"]) not in low(_current_url(ctx)):
        return False
    if "task_contains" in when and low(when["task_contains"]) not in low(ctx.extra.get("task", "")):
        return False
    return True


async def check(ctx: "RunContext", tool: str, args: dict) -> ToolResult | None:
    """Return a failing ToolResult if a guard blocks this action, else None."""
    skill = ctx.skill
    guards = (skill.meta.get("guards") if skill else None) or []
    if not guards or ctx.extra.get("in_guard"):
        return None
    for guard in guards:
        if not _matches(ctx, guard.get("when") or {}, tool, args):
            continue
        name = guard.get("name") or "guard"
        if guard.get("block"):
            log.warning("guard %s blocked %s", name, tool)
            ctx.run.event("guard", name=name, outcome="blocked", tool=tool)
            return ToolResult(False, error=f"blocked by guard '{name}': {guard['block']}")
        if guard.get("run_macro"):
            from ..tools.base import REGISTRY

            ctx.extra["in_guard"] = True
            try:
                res = await REGISTRY.call("skill.run_macro", {"name": guard["run_macro"]}, ctx)
            finally:
                ctx.extra["in_guard"] = False
            ctx.run.event("guard", name=name, outcome="passed" if res.ok else "failed", tool=tool)
            if not res.ok:
                log.warning("guard %s failed; %s not executed", name, tool)
                return ToolResult(False, res.output, error=f"blocked by guard '{name}': check "
                                  f"'{guard['run_macro']}' failed. Fix what it reports, then retry. {res.error or ''}")
    return None
