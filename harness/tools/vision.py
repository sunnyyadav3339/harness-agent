"""Local OCR for text inside images (captchas, image-only labels).

ddddocr runs on CPU in ~20-40 ms and costs no API quota. When a vision model
is configured it is consulted in parallel; agreement means high confidence.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import threading

from PIL import Image

from ..perception.screen import grab
from .base import ToolResult, tool

log = logging.getLogger(__name__)

_ocr = None
_ocr_lock = threading.Lock()


def _engine():
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            import ddddocr

            _ocr = ddddocr.DdddOcr(show_ad=False)
        return _ocr


def warm_ocr() -> None:
    """Load the ONNX model ahead of time (first load takes ~0.5 s)."""
    _engine()


def _prep(png: bytes) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if img.width < 120:
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _llm_read(ctx, png: bytes) -> str | None:
    router = ctx.router
    if router is None or not router.group_has_vision("vision"):
        return None
    b64 = base64.b64encode(png).decode()
    messages = [
        {"role": "system", "content": "You transcribe text from images exactly. Reply with JSON {\"text\": \"...\"}."},
        {"role": "user", "content": [
            {"type": "text", "text": "Transcribe the characters in this image exactly, preserving case. "
                                     "No spaces unless clearly present."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]},
    ]
    try:
        from ..llm.parsing import parse_json_object

        resp = await router.complete("vision", messages, max_tokens=60, temperature=0.0, deadline_s=8)
        if resp.images_stripped:
            return None
        return str(parse_json_object(resp.text).get("text", "")).strip()
    except Exception as exc:
        log.info("vision cross-check unavailable: %s", exc)
        return None


@tool("vision.read_text", "OCR the text inside an image element (browser id/selector) or a screen region [x,y,w,h]. "
      "Runs locally; cross-checks with the vision model when verify=true.",
      "id=None, selector=None, region=None, verify=true", timeout_s=30)
async def read_text(ctx, id=None, selector=None, region=None, verify: bool = True) -> ToolResult:
    if id is not None or selector:
        page = await ctx.browser.page()
        if id is not None:
            el = ctx.browser_snapshot.by_id(int(id)) if ctx.browser_snapshot else None
            frame = el.frame if el and el.frame else page.main_frame
            loc = frame.locator(f'[data-agent-id="{int(id)}"]').first
        else:
            loc = page.locator(selector).first
        png = await loc.screenshot(type="png", timeout=5000)
    elif region:
        scale = ctx.desktop_scale or 1.0
        x, y, w, h = (int(v / scale) for v in region)
        buf = io.BytesIO()
        grab((x, y, w, h)).save(buf, format="PNG")
        png = buf.getvalue()
    else:
        return ToolResult(False, error="give id, selector or region")

    prepared = _prep(png)
    # Local OCR is the answer; the vision model is only a bounded cross-check. Verified 24-Oct: with a slow
    # model (8-14 s) the old unbounded cross-check made the whole tool time out on the IRCTC login captcha.
    remote_task = asyncio.create_task(_llm_read(ctx, png)) if verify else None
    local = await asyncio.to_thread(lambda: _engine().classification(prepared))
    remote = None
    if remote_task is not None:
        try:
            remote = await asyncio.wait_for(remote_task, timeout=6)
        except (asyncio.TimeoutError, Exception):
            remote = None
    local = re.sub(r"\s+", "", local or "")
    remote = re.sub(r"\s+", "", remote) if remote else None
    if remote and remote == local:
        return ToolResult(True, f"text={local!r} (local OCR and vision model agree)", data={"text": local})
    if remote:
        return ToolResult(True, f"text={local!r} (local OCR); vision model read {remote!r}. "
                                f"If one is rejected, try the other.", data={"text": local, "alt": remote})
    return ToolResult(True, f"text={local!r} (local OCR only)", data={"text": local})
