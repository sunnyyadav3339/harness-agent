"""Tool registry: one uniform surface for browser, desktop, shell, files, clock, human."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..credentials import MissingSecret, render_args

if TYPE_CHECKING:
    from ..runtime.context import RunContext

log = logging.getLogger(__name__)


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str | None = None
    data: dict = field(default_factory=dict)
    # Where the action landed, as ("browser"|"desktop", x, y) in that surface's
    # screenshot space. Drawn as a crosshair in the run log.
    point: tuple[str, int, int] | None = None
    # The page/window changed; remaining actions of a batch are now stale.
    changed_view: bool = False
    elapsed_ms: int = 0

    def summary(self, limit: int = 700) -> str:
        if self.ok:
            text = self.output or "ok"
        else:
            text = f"ERROR: {self.error}" + (f" | {self.output}" if self.output else "")
        return text if len(text) <= limit else text[: limit - 20] + " ...[truncated]"


ToolFn = Callable[..., Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    params: str
    fn: ToolFn
    timeout_s: float = 60.0

    def signature(self) -> str:
        return f"- {self.name}({self.params}): {self.description}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, description: str, params: str = "", timeout_s: float = 60.0):
        def deco(fn: ToolFn) -> ToolFn:
            self._tools[name] = ToolSpec(name, description, params, fn, timeout_s)
            return fn

        return deco

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, allowed: list[str] | None = None) -> list[ToolSpec]:
        out = []
        for name in sorted(self._tools):
            if allowed and not any(name == a or name.startswith(a.rstrip("*").rstrip(".") + ".") for a in allowed):
                continue
            out.append(self._tools[name])
        return out

    def catalog(self, allowed: list[str] | None = None) -> str:
        return "\n".join(s.signature() for s in self.specs(allowed))

    async def call(self, name: str, args: dict | None, ctx: "RunContext") -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            close = [n for n in self._tools if n.split(".")[0] == name.split(".")[0]]
            return ToolResult(False, error=f"unknown tool '{name}'. Tools in that family: {', '.join(close) or 'none'}")
        args = dict(args or {})
        try:
            rendered = render_args(args, ctx.vault, ctx.secrets)
        except MissingSecret as exc:
            return ToolResult(False, error=f"secret {exc} is not configured; ask the human or use another approach")
        sig = inspect.signature(spec.fn)
        accepted = {p for p in sig.parameters if p != "ctx"}
        has_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        unknown = [k for k in rendered if k not in accepted]
        if unknown and not has_var_kw:
            return ToolResult(False, error=f"unexpected argument(s) {unknown} for {name}({spec.params})")
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(spec.fn(ctx, **rendered), timeout=spec.timeout_s)
        except asyncio.TimeoutError:
            result = ToolResult(False, error=f"{name} timed out after {spec.timeout_s:.0f}s")
        except TypeError as exc:
            result = ToolResult(False, error=f"bad arguments for {name}({spec.params}): {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # tools must never crash the loop
            # Tool errors are normal agent feedback (bad selector, page JS error); keep the log readable.
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            log.warning("tool %s failed: %s: %s", name, exc.__class__.__name__, first_line[:300])
            log.debug("tool %s traceback", name, exc_info=True)
            # The model needs the message, not Playwright's JS stack ("at eval (<anonymous>)...").
            lines = [ln for ln in str(exc).splitlines() if ln.strip() and not ln.strip().startswith("at ")]
            result = ToolResult(False, error=f"{exc.__class__.__name__}: {' '.join(lines[:3])[:600]}")
        result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        result.output = ctx.secrets.redact(result.output)
        if result.error:
            result.error = ctx.secrets.redact(result.error)
        return result


REGISTRY = ToolRegistry()
tool = REGISTRY.register
