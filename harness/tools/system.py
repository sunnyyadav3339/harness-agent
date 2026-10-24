"""Shell, files and clock hands."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import timedelta
from pathlib import Path

from ..policy import fingerprint
from ..timeutil import CLOCK, parse_target
from .base import ToolResult, tool

_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
_NO_WINDOW = 0x08000000


def _truncate(text: str, head: int = 2500, tail: int = 1500) -> str:
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n... [{len(text) - head - tail} chars omitted] ...\n{text[-tail:]}"


def _approval(ctx, tool_name: str, args: dict, reason: str) -> ToolResult:
    return ToolResult(
        False,
        error=f"needs human approval: {reason}",
        data={"needs_approval": {"tool": tool_name, "args": args, "reason": reason,
                                 "fingerprint": fingerprint(tool_name, args)}},
    )


def resolve_path(path: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    return p if p.is_absolute() else Path.home() / p


@tool("shell.run", "Run a command and return exit code + output. shell: powershell (default), cmd, or bash.",
      "command, shell='powershell', cwd=None, timeout_s=60", timeout_s=900)
async def shell_run(ctx, command: str, shell: str = "powershell", cwd=None, timeout_s: float = 60) -> ToolResult:
    args = {"command": command, "shell": shell, "cwd": cwd}
    verdict = ctx.policy.check_shell(command, fingerprint("shell.run", args))
    if not verdict.allowed:
        if verdict.needs_approval:
            return _approval(ctx, "shell.run", args, verdict.reason)
        return ToolResult(False, error=f"refused by policy: {verdict.reason}")

    if shell == "cmd":
        argv = ["cmd.exe", "/d", "/s", "/c", command]
    elif shell == "bash":
        argv = [_GIT_BASH if Path(_GIT_BASH).exists() else "bash", "-lc", command]
    else:
        prelude = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", prelude + command]
    workdir = resolve_path(cwd) if cwd else Path.home()
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(workdir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, creationflags=_NO_WINDOW,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_s))
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        out = b""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        except Exception:
            pass
    text = _truncate(out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip())
    if timed_out:
        return ToolResult(False, text, error=f"timed out after {timeout_s}s (process tree killed)")
    code = proc.returncode
    return ToolResult(code == 0, f"exit={code}\n{text}" if text else f"exit={code}",
                      error=None if code == 0 else f"exit code {code}", data={"exit_code": code})


@tool("file.read", "Read a text file (relative paths are under the home folder).", "path, offset=0, max_chars=8000")
async def file_read(ctx, path: str, offset: int = 0, max_chars: int = 8000) -> ToolResult:
    p = resolve_path(path)
    if not p.exists():
        return ToolResult(False, error=f"{p} does not exist")
    text = p.read_text(encoding="utf-8", errors="replace")
    chunk = text[int(offset): int(offset) + int(max_chars)]
    more = len(text) - int(offset) - len(chunk)
    return ToolResult(True, chunk + (f"\n... [{more} more chars; use offset]" if more > 0 else ""))


@tool("file.write", "Write (or append to) a text file, creating folders as needed.", "path, content, append=false")
async def file_write(ctx, path: str, content: str, append: bool = False) -> ToolResult:
    p = resolve_path(path)
    verdict = ctx.policy.check_write(p, fingerprint("file.write", {"path": path}))
    if not verdict.allowed:
        return ToolResult(False, error=f"refused by policy: {verdict.reason}")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return ToolResult(True, f"wrote {len(content)} chars to {p}")


@tool("file.edit", "Replace one exact occurrence of old with new in a text file.", "path, old, new")
async def file_edit(ctx, path: str, old: str, new: str) -> ToolResult:
    p = resolve_path(path)
    verdict = ctx.policy.check_write(p, fingerprint("file.edit", {"path": path}))
    if not verdict.allowed:
        return ToolResult(False, error=f"refused by policy: {verdict.reason}")
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        return ToolResult(False, error=f"'old' occurs {count} times; it must occur exactly once")
    p.write_text(text.replace(old, new), encoding="utf-8")
    return ToolResult(True, f"edited {p}")


@tool("file.list", "List a folder (optionally filtered by a glob like '*.pdf', recursive=true for subfolders).",
      "path, pattern='*', recursive=false")
async def file_list(ctx, path: str, pattern: str = "*", recursive: bool = False) -> ToolResult:
    p = resolve_path(path)
    if not p.is_dir():
        return ToolResult(False, error=f"{p} is not a folder")
    it = p.rglob(pattern) if recursive else p.glob(pattern)
    rows = []
    for i, entry in enumerate(sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))):
        if i >= 300:
            rows.append("... more entries omitted")
            break
        try:
            size = "" if entry.is_dir() else f" {entry.stat().st_size}B"
        except OSError:
            size = ""
        rows.append(f"{'[dir] ' if entry.is_dir() else ''}{entry.relative_to(p)}{size}")
    return ToolResult(True, "\n".join(rows) or "(empty)")


@tool("file.delete", "Delete a file (asks a human first).", "path")
async def file_delete(ctx, path: str) -> ToolResult:
    p = resolve_path(path)
    args = {"path": path}
    verdict = ctx.policy.check_write(p, fingerprint("file.delete", args), delete=True)
    if not verdict.allowed:
        if verdict.needs_approval:
            return _approval(ctx, "file.delete", args, verdict.reason)
        return ToolResult(False, error=f"refused by policy: {verdict.reason}")
    if not p.is_file():
        return ToolResult(False, error=f"{p} is not a file")
    p.unlink()
    return ToolResult(True, f"deleted {p}")


@tool("clock.now", "Current NTP-corrected time (default Asia/Kolkata).", "tz=None")
async def clock_now(ctx, tz=None) -> ToolResult:
    now = CLOCK.now(tz)
    st = CLOCK.status()
    return ToolResult(True, f"{now.isoformat(timespec='milliseconds')} (ntp offset {st['offset_ms']} ms, synced={st['synced']})")


@tool("clock.wait_until", "Block until a wall-clock time, millisecond precise, e.g. time='11:00:01.200'.",
      "time, tz=None, date=None, max_wait_s=7200", timeout_s=7300)
async def clock_wait_until(ctx, time: str, tz=None, date=None, max_wait_s: float = 7200) -> ToolResult:
    target = parse_target(time, tz, date)
    remaining = target.timestamp() - CLOCK.now_ts()
    if date is None and remaining < -12 * 3600:
        # A bare time far in the past means the next occurrence (verified 24-Oct 23:59: "00:20:05", the end of
        # IRCTC's nightly maintenance, was read as that morning and the wait was skipped).
        target += timedelta(days=1)
        remaining += 86400
    if remaining < 0:
        return ToolResult(True, f"{target.isoformat(timespec='milliseconds')} already passed {-remaining:.2f}s ago; continue now")
    if remaining > float(max_wait_s):
        return ToolResult(False, error=f"target is {remaining:.0f}s away, more than max_wait_s={max_wait_s}")
    if remaining > 120 and ctx.settings.ntp_server:
        await CLOCK.sync(ctx.settings.ntp_server)
    late_ms = await CLOCK.sleep_until(target.timestamp())
    return ToolResult(True, f"reached {CLOCK.now(tz).isoformat(timespec='milliseconds')} (late by {late_ms} ms)")


@tool("wait", "Pause for a few seconds (for slow pages/apps).", "seconds=1")
async def wait(ctx, seconds: float = 1) -> ToolResult:
    await asyncio.sleep(min(float(seconds), 120))
    return ToolResult(True, f"waited {seconds}s")
