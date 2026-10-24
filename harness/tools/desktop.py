"""Desktop hand: mouse/keyboard on any Windows app (pyautogui + UIA)."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from .. import platform_win
from .base import ToolResult, tool

_KEY_ALIASES = {
    "win": "winleft", "windows": "winleft", "cmd": "winleft", "super": "winleft",
    "control": "ctrl", "return": "enter", "escape": "esc", "del": "delete",
    "pgup": "pageup", "pgdn": "pagedown",
}


def _gui():
    import pyautogui

    pyautogui.FAILSAFE = False  # hands-free runs must not abort when a click lands in a corner
    pyautogui.PAUSE = 0.03
    return pyautogui


def _to_physical(ctx, x, y) -> tuple[int, int]:
    """Model coordinates are in the downscaled screenshot it saw."""
    scale = ctx.desktop_scale or 1.0
    return int(round(float(x) / scale)), int(round(float(y) / scale))


def _target(ctx, id=None, x=None, y=None) -> tuple[int, int]:
    if id is not None:
        snap = ctx.desktop_snapshot
        el = snap.by_id(int(id)) if snap else None
        if el is None:
            raise LookupError(f"desktop element [{id}] is not in the latest observation")
        return el.center
    if x is None or y is None:
        raise ValueError("give id, or x and y")
    return _to_physical(ctx, x, y)


def _point(ctx, px: int, py: int):
    scale = ctx.desktop_scale or 1.0
    return ("desktop", int(px * scale), int(py * scale))


def _keys(chord: str) -> list[str]:
    parts = [p.strip().lower() for p in chord.replace(" ", "").split("+") if p.strip()]
    return [_KEY_ALIASES.get(p, p) for p in parts]


@tool("desktop.click", "Click a UI element by id, or screen coordinates x,y from the screenshot.",
      "id=None, x=None, y=None, button='left', double=false")
async def desktop_click(ctx, id=None, x=None, y=None, button: str = "left", double: bool = False) -> ToolResult:
    px, py = _target(ctx, id, x, y)
    gui = _gui()
    await asyncio.to_thread(gui.click, px, py, clicks=2 if double else 1, interval=0.08, button=button)
    await asyncio.sleep(0.35)
    return ToolResult(True, f"{'double-' if double else ''}clicked ({px},{py})", point=_point(ctx, px, py),
                      changed_view=True)


@tool("desktop.type", "Type text into the focused control (or click element id first). Unicode-safe.",
      "text, id=None, enter=false")
async def desktop_type(ctx, text: str, id=None, enter: bool = False) -> ToolResult:
    gui = _gui()
    point = None
    if id is not None:
        px, py = _target(ctx, id)
        await asyncio.to_thread(gui.click, px, py)
        await asyncio.sleep(0.2)
        point = _point(ctx, px, py)
    if text.isascii() and len(text) <= 60 and "\n" not in text:
        await asyncio.to_thread(gui.write, text, interval=0.012)
    else:
        # Paste via clipboard for unicode / long text, then restore the clipboard.
        try:
            previous = platform_win.get_clipboard()
        except Exception:
            previous = None
        platform_win.set_clipboard(text)
        await asyncio.to_thread(gui.hotkey, "ctrl", "v")
        await asyncio.sleep(0.15)
        if previous is not None:
            platform_win.set_clipboard(previous)
    if enter:
        await asyncio.to_thread(gui.press, "enter")
    await asyncio.sleep(0.2)
    return ToolResult(True, f"typed {len(text)} chars", point=point)


@tool("desktop.hotkey", "Press a key chord like 'ctrl+s', 'alt+f4', 'win+r'.", "keys")
async def desktop_hotkey(ctx, keys: str) -> ToolResult:
    await asyncio.to_thread(_gui().hotkey, *_keys(keys))
    await asyncio.sleep(0.3)
    return ToolResult(True, f"pressed {keys}", changed_view=True)


@tool("desktop.press", "Press a single key, optionally several times (enter, tab, esc, down, f5...).",
      "key, times=1")
async def desktop_press(ctx, key: str, times: int = 1) -> ToolResult:
    await asyncio.to_thread(_gui().press, _keys(key)[0], presses=int(times), interval=0.05)
    await asyncio.sleep(0.2)
    return ToolResult(True, f"pressed {key} x{times}")


@tool("desktop.scroll", "Scroll the mouse wheel (positive=up) at x,y or the current position.",
      "amount=-5, x=None, y=None")
async def desktop_scroll(ctx, amount: int = -5, x=None, y=None) -> ToolResult:
    gui = _gui()
    if x is not None and y is not None:
        px, py = _to_physical(ctx, x, y)
        await asyncio.to_thread(gui.scroll, int(amount) * 120, x=px, y=py)
    else:
        await asyncio.to_thread(gui.scroll, int(amount) * 120)
    await asyncio.sleep(0.25)
    return ToolResult(True, f"scrolled {amount}")


@tool("desktop.drag", "Drag from (x1,y1) to (x2,y2) in screenshot coordinates.", "x1, y1, x2, y2")
async def desktop_drag(ctx, x1, y1, x2, y2) -> ToolResult:
    gui = _gui()
    a, b = _to_physical(ctx, x1, y1), _to_physical(ctx, x2, y2)
    await asyncio.to_thread(gui.moveTo, *a)
    await asyncio.to_thread(gui.dragTo, *b, duration=0.4, button="left")
    return ToolResult(True, f"dragged {a}->{b}", point=_point(ctx, *b), changed_view=True)


@tool("desktop.open", "Open an app, file, folder or URI (e.g. 'notepad', 'C:\\path\\file.xlsx', 'explorer').",
      "target, args=''", timeout_s=30)
async def desktop_open(ctx, target: str, args: str = "") -> ToolResult:
    expanded = os.path.expandvars(os.path.expanduser(target))
    if Path(expanded).exists() and not args:
        os.startfile(expanded)  # type: ignore[attr-defined]
    else:
        cmd = f'start "" "{expanded}" {args}'.strip()
        subprocess.Popen(cmd, shell=True, creationflags=0x08000000)  # CREATE_NO_WINDOW
    await asyncio.sleep(1.5)
    return ToolResult(True, f"opened {target}", changed_view=True)


@tool("desktop.focus", "Bring the first window whose title contains the text to the foreground.", "title_contains")
async def desktop_focus(ctx, title_contains: str) -> ToolResult:
    needle = title_contains.lower()
    for w in platform_win.list_windows(80):
        if needle in w.title.lower():
            ok = platform_win.focus_window(w.hwnd)
            await asyncio.sleep(0.4)
            return ToolResult(ok, f"focused \"{w.title}\"" if ok else "", error=None if ok else "focus refused",
                              changed_view=True)
    return ToolResult(False, error=f"no window title contains {title_contains!r}")


@tool("desktop.windows", "List visible top-level windows.", "")
async def desktop_windows(ctx) -> ToolResult:
    wins = platform_win.list_windows(60)
    return ToolResult(True, "\n".join(f"\"{w.title}\" ({w.exe})" for w in wins))


@tool("desktop.read", "Read the full name/value text of a desktop UI element.", "id")
async def desktop_read(ctx, id) -> ToolResult:
    el = ctx.desktop_snapshot.by_id(int(id)) if ctx.desktop_snapshot else None
    if el is None:
        return ToolResult(False, error=f"desktop element [{id}] not in latest observation")
    return ToolResult(True, f"{el.control_type} name={el.name!r} value={el.value!r}")
