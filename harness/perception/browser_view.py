"""Browser "eyes": DOM index across frames + annotated viewport screenshot."""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from .annotate import draw_marks

log = logging.getLogger(__name__)

_JS_DIR = Path(__file__).parent
INDEXER_JS = (_JS_DIR / "dom_indexer.js").read_text(encoding="utf-8")
SETTLE_JS = (_JS_DIR / "settle.js").read_text(encoding="utf-8")
MAX_CHILD_FRAMES = 6


@dataclass
class BrowserElement:
    id: int
    tag: str
    text: str
    attrs: dict
    rect: tuple[int, int, int, int]  # x, y, w, h in viewport CSS px
    frame: Any = None  # patchright Frame (runtime only)
    offscreen: bool = False

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.rect
        return x + w // 2, y + h // 2

    def line(self) -> str:
        a = self.attrs
        bits = [self.tag]
        for key in ("type", "role"):
            if a.get(key) and not (key == "type" and self.tag == "button"):
                bits.append(f"{key}={a[key]}")
        if a.get("disabled"):
            bits.append("disabled")
        meta = []
        for key in ("id", "name", "formcontrolname", "aria-expanded"):
            if a.get(key):
                meta.append(f"{key}={a[key]}")
        if self.tag == "a" and a.get("href") and not self.text:
            meta.append(f"href={a['href']}")
        extra = f" ({', '.join(meta)})" if meta else ""
        off = " [offscreen]" if self.offscreen else ""
        return f"[{self.id}] <{' '.join(bits)}> \"{self.text}\"{extra}{off}"


@dataclass
class BrowserSnapshot:
    url: str = ""
    title: str = ""
    elements: list[BrowserElement] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    text: str = ""
    tabs: list[dict] = field(default_factory=list)
    viewport: tuple[int, int] = (0, 0)
    scroll: tuple[int, int] = (0, 0)
    page_height: int = 0
    image: Image.Image | None = None  # raw viewport screenshot
    error: str | None = None

    def by_id(self, element_id: int) -> BrowserElement | None:
        for el in self.elements:
            if el.id == element_id:
                return el
        return None

    def annotated(self) -> Image.Image | None:
        if self.image is None:
            return None
        sx = self.image.width / max(self.viewport[0], 1)
        sy = self.image.height / max(self.viewport[1], 1)
        boxes = [
            (e.id, (int(e.rect[0] * sx), int(e.rect[1] * sy), int(e.rect[2] * sx), int(e.rect[3] * sy)))
            for e in self.elements
            if not e.offscreen
        ]
        return draw_marks(self.image, boxes)

    def describe(self, max_elements: int = 150, text_chars: int = 1200) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if len(self.tabs) > 1:
            tabs = ", ".join(f"{t['index']}:{t['title'][:30]}{'*' if t['active'] else ''}" for t in self.tabs)
            lines.append(f"Tabs: {tabs}")
        lines.append(
            f"Viewport {self.viewport[0]}x{self.viewport[1]}, scrollY={self.scroll[1]} of {self.page_height}"
        )
        if self.alerts:
            lines.append("Alerts/dialogs: " + " || ".join(self.alerts))
        lines.append(f"Interactive elements ({len(self.elements)}):")
        lines += [e.line() for e in self.elements[:max_elements]]
        if len(self.elements) > max_elements:
            lines.append(f"... {len(self.elements) - max_elements} more (scroll or use browser.read)")
        if self.text and text_chars:
            lines.append("Visible page text (truncated): " + self.text[:text_chars])
        return "\n".join(lines)


async def settle(page, busy_selectors: list[str] | None = None, max_ms: int = 2500) -> int:
    try:
        return await page.evaluate(SETTLE_JS, {"quietMs": 250, "maxMs": max_ms, "busy": busy_selectors or []})
    except Exception:
        return -1


async def snapshot(
    page,
    *,
    max_elements: int = 180,
    text_limit: int = 1500,
    screenshot: bool = True,
    tabs: list[dict] | None = None,
) -> BrowserSnapshot:
    snap = BrowserSnapshot(tabs=tabs or [])
    next_id = 0
    # Ad-heavy pages carry dozens of iframes; a hung one must not stall the turn.
    frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame][:MAX_CHILD_FRAMES]
    for idx, frame in enumerate(frames):
        if next_id >= max_elements:
            break
        try:
            offset = (0, 0)
            if idx > 0:
                if frame.is_detached() or frame.url in ("", "about:blank"):
                    continue
                handle = await asyncio.wait_for(frame.frame_element(), 1.0)
                box = await asyncio.wait_for(handle.bounding_box(), 1.0)
                if not box or box["width"] < 40 or box["height"] < 20:
                    continue
                offset = (int(box["x"]), int(box["y"]))
            data = await asyncio.wait_for(frame.evaluate(
                INDEXER_JS,
                {
                    "maxElements": max_elements - next_id,
                    "idOffset": next_id,
                    "textLimit": text_limit if idx == 0 else 300,
                    "withText": True,
                },
            ), 4.0 if idx == 0 else 1.5)
        except Exception as exc:
            if idx == 0:
                snap.error = f"DOM index failed: {exc}"
                log.warning(snap.error)
            continue
        if idx == 0:
            snap.url, snap.title = data["url"], data["title"]
            snap.viewport = tuple(data["viewport"])
            snap.scroll = tuple(data["scroll"])
            snap.page_height = data["pageHeight"]
            snap.text = data["text"]
        elif data.get("text"):
            snap.text += f"\n[frame {data['url'][:60]}] {data['text']}"
        snap.alerts += [a for a in data["alerts"] if a not in snap.alerts]
        for e in data["elements"]:
            x, y, w, h = e["rect"]
            snap.elements.append(
                BrowserElement(
                    id=e["id"],
                    tag=e["tag"],
                    text=e["text"],
                    attrs=e["attrs"],
                    rect=(x + offset[0], y + offset[1], w, h),
                    frame=frame,
                    offscreen=e.get("offscreen", False),
                )
            )
        next_id = data["nextId"]
    if screenshot:
        try:
            # Short timeout: a turn without a screenshot still has the DOM list; a 6 s stall does not help.
            raw = await page.screenshot(type="jpeg", quality=70, scale="css", timeout=3000)
            snap.image = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as exc:
            log.warning("page screenshot failed: %s", exc)
    return snap
