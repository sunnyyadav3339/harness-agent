"""Full-screen capture (physical pixels; process must be DPI aware)."""

from __future__ import annotations

import threading

import mss
from PIL import Image

_local = threading.local()


def _sct():
    # mss handles are not thread-safe; keep one per thread.
    if not hasattr(_local, "sct"):
        _local.sct = mss.mss()
    return _local.sct


def grab(region: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Capture the primary monitor, or ``region`` = (left, top, width, height)."""
    sct = _sct()
    if region:
        left, top, width, height = region
        mon = {"left": left, "top": top, "width": width, "height": height}
    else:
        mon = sct.monitors[1]
    shot = sct.grab(mon)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def screen_size() -> tuple[int, int]:
    mon = _sct().monitors[1]
    return mon["width"], mon["height"]
