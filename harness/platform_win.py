"""Thin Windows helpers: DPI, keep-awake, windows, clipboard.

Everything that needs physical-pixel coordinates (mss, pyautogui, UIA) only
agrees once the process is per-monitor DPI aware, so ``init_process`` must run
before any of those libraries is imported.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

_initialised = False


def init_process() -> None:
    global _initialised
    if _initialised or not IS_WINDOWS:
        return
    _initialised = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            log.warning("could not set DPI awareness; clicks may drift on scaled displays")
    try:
        # 1 ms timer resolution so asyncio sleeps near a deadline are not 15.6 ms coarse.
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def keep_awake(enabled: bool = True) -> None:
    """Ask Windows not to sleep or blank the display while this process runs.

    This is a per-process request, not a change to power settings; it ends
    when the process exits.
    """
    if not IS_WINDOWS:
        return
    flags = _ES_CONTINUOUS
    if enabled:
        flags |= _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom (physical px)

    def to_dict(self) -> dict:
        return asdict(self)


def _exe_name(pid: int) -> str:
    try:
        import win32api
        import win32con
        import win32process

        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
        return path.rsplit("\\", 1)[-1].lower()
    except Exception:
        return ""


def _window_info(hwnd: int) -> WindowInfo:
    import win32gui
    import win32process

    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return WindowInfo(
        hwnd=hwnd,
        title=win32gui.GetWindowText(hwnd),
        pid=pid,
        exe=_exe_name(pid),
        rect=tuple(win32gui.GetWindowRect(hwnd)),
    )


def foreground_window() -> WindowInfo | None:
    if not IS_WINDOWS:
        return None
    import win32gui

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    try:
        return _window_info(hwnd)
    except Exception:
        return None


def list_windows(limit: int = 30) -> list[WindowInfo]:
    if not IS_WINDOWS:
        return []
    import win32gui

    found: list[WindowInfo] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title or title in ("Program Manager", "Windows Input Experience"):
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 50 or bottom - top < 50:
            return True
        try:
            found.append(_window_info(hwnd))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return found[:limit]


def focus_window(hwnd: int) -> bool:
    """Bring a window to the front despite Windows' foreground-lock rules."""
    if not IS_WINDOWS:
        return False
    import win32con
    import win32gui

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # A synthetic Alt press lets a background process take the foreground.
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception as exc:
        log.debug("focus_window failed: %s", exc)
        try:
            from pywinauto import Application

            Application(backend="uia").connect(handle=hwnd).window(handle=hwnd).set_focus()
            return True
        except Exception:
            return False


def set_clipboard(text: str) -> None:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def get_clipboard() -> str:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return ""
    finally:
        win32clipboard.CloseClipboard()
