"""Desktop "eyes": UI Automation tree of the foreground window + screen grab.

Tier A is the Windows UIA tree (exact control boxes). When an app exposes
nothing useful (canvas apps, remote viewers) the element list is empty and the
planner falls back to coordinates on the screenshot (Tier B).
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field

from PIL import Image

from .. import platform_win
from .annotate import draw_marks
from .screen import grab

log = logging.getLogger(__name__)

INTERACTIVE = {
    "Button", "Edit", "ComboBox", "ListItem", "MenuItem", "TabItem", "CheckBox",
    "RadioButton", "Hyperlink", "TreeItem", "DataItem", "SplitButton", "Slider",
    "Spinner", "MenuBar", "Document", "HeaderItem", "Thumb",
}
READABLE = {"Text", "StatusBar", "TitleBar"}


def _com_init() -> None:
    try:
        import pythoncom

        pythoncom.CoInitializeEx(0)
    except Exception:
        pass


# UIA is COM; keep all of it on one dedicated, COM-initialised thread.
_UIA_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, initializer=_com_init, thread_name_prefix="uia")


@dataclass
class DesktopElement:
    id: int
    control_type: str
    name: str
    automation_id: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom (physical px)
    enabled: bool = True
    value: str = ""

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2

    def line(self) -> str:
        bits = [self.control_type]
        if not self.enabled:
            bits.append("disabled")
        extra = f" value=\"{self.value[:60]}\"" if self.value else ""
        aid = f" (automation_id={self.automation_id})" if self.automation_id else ""
        return f"[{self.id}] <{' '.join(bits)}> \"{self.name[:80]}\"{extra}{aid}"


@dataclass
class DesktopSnapshot:
    foreground: dict | None = None
    windows: list[dict] = field(default_factory=list)
    elements: list[DesktopElement] = field(default_factory=list)
    image: Image.Image | None = None
    uia_ms: int = 0
    truncated: bool = False

    def by_id(self, element_id: int) -> DesktopElement | None:
        for el in self.elements:
            if el.id == element_id:
                return el
        return None

    def annotated(self) -> Image.Image | None:
        if self.image is None:
            return None
        boxes = [(e.id, (e.rect[0], e.rect[1], e.rect[2] - e.rect[0], e.rect[3] - e.rect[1])) for e in self.elements]
        return draw_marks(self.image, boxes)

    def describe(self, max_elements: int = 120) -> str:
        fg = self.foreground or {}
        lines = [f"Foreground window: \"{fg.get('title', '?')}\" ({fg.get('exe', '?')})"]
        if self.image is not None:
            lines.append(f"Screen: {self.image.width}x{self.image.height} physical px")
        others = [w for w in self.windows if w.get("hwnd") != fg.get("hwnd")][:12]
        if others:
            lines.append("Other windows: " + " | ".join(f"\"{w['title'][:40]}\" ({w['exe']})" for w in others))
        if self.elements:
            lines.append(f"UI elements in foreground window ({len(self.elements)}):")
            lines += [e.line() for e in self.elements[:max_elements]]
            if self.truncated:
                lines.append("... element scan truncated (large window)")
        else:
            lines.append("No accessible UI elements; use x,y coordinates on the screenshot.")
        return "\n".join(lines)


def _scan_uia(hwnd: int, max_elements: int, budget_s: float) -> tuple[list[DesktopElement], bool]:
    from pywinauto.uia_element_info import UIAElementInfo

    start = time.perf_counter()
    root = UIAElementInfo(hwnd)
    out: list[DesktopElement] = []
    queue = [root]
    truncated = False
    visited = 0
    while queue:
        if time.perf_counter() - start > budget_s or len(out) >= max_elements:
            truncated = True
            break
        node = queue.pop(0)
        visited += 1
        try:
            children = node.children()
        except Exception:
            children = []
        for child in children:
            try:
                ctype = child.control_type or ""
                rect = child.rectangle
                box = (rect.left, rect.top, rect.right, rect.bottom)
            except Exception:
                continue
            visible = box[2] - box[0] > 2 and box[3] - box[1] > 2
            if visible and (ctype in INTERACTIVE or (ctype in READABLE and child.name)):
                name = (child.name or "").strip()
                value = ""
                if ctype in ("Edit", "ComboBox", "Document"):
                    try:
                        value = (child.rich_text or "")[:200]
                    except Exception:
                        value = ""
                try:
                    enabled = bool(child.enabled)
                except Exception:
                    enabled = True
                out.append(
                    DesktopElement(
                        id=len(out),
                        control_type=ctype,
                        name=name,
                        automation_id=(child.automation_id or "")[:40],
                        rect=box,
                        enabled=enabled,
                        value=value if value != name else "",
                    )
                )
            # Do not descend into huge content controls; their children are rarely actionable.
            if ctype not in ("Document",) or len(out) < 40:
                queue.append(child)
    return out, truncated


def _snapshot_sync(max_elements: int, budget_s: float, with_uia: bool) -> DesktopSnapshot:
    snap = DesktopSnapshot()
    fg = platform_win.foreground_window()
    snap.foreground = fg.to_dict() if fg else None
    snap.windows = [w.to_dict() for w in platform_win.list_windows()]
    snap.image = grab()
    if with_uia and fg:
        t0 = time.perf_counter()
        try:
            snap.elements, snap.truncated = _scan_uia(fg.hwnd, max_elements, budget_s)
        except Exception as exc:
            log.warning("UIA scan failed: %s", exc)
        snap.uia_ms = int((time.perf_counter() - t0) * 1000)
    return snap


async def snapshot(max_elements: int = 120, budget_s: float = 1.5, with_uia: bool = True) -> DesktopSnapshot:
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_UIA_POOL, _snapshot_sync, max_elements, budget_s, with_uia)
