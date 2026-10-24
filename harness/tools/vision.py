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
        from .browser import _locate  # same lookup as clicks: observation id, DOM id fallback, visible-first

        _, loc, _ = await _locate(ctx, id, selector)
        if not await loc.count():
            return ToolResult(False, error="no such image on the page")
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
            remote = await asyncio.wait_for(remote_task, timeout=9)
        except (asyncio.TimeoutError, Exception):
            remote = None
    local = re.sub(r"\s+", "", local or "")
    remote = re.sub(r"\s+", "", remote) if remote else None
    if remote and remote == local:
        return ToolResult(True, f"text={local!r} (local OCR and vision model agree)", data={"text": local})
    # ddddocr outputs lowercase only (verified 23:58: IRCTC captcha "nFKk" read as "nfkk" and rejected), while
    # captchas are case-sensitive: the vision model's reading carries the case.
    if remote and remote.lower() == local.lower():
        return ToolResult(True, f"text={remote!r} (vision model; local OCR agrees ignoring case)",
                          data={"text": remote})
    if remote and re.fullmatch(r"[A-Za-z0-9]{3,10}", remote) and abs(len(remote) - len(local)) <= 1:
        return ToolResult(True, f"text={remote!r} (vision model); local OCR read {local!r}. If one is rejected, "
                                f"refresh and read again.", data={"text": remote, "alt": local})
    if remote:
        return ToolResult(True, f"text={local!r} (local OCR); vision model read {remote!r}. "
                                f"If one is rejected, try the other.", data={"text": local, "alt": remote})
    return ToolResult(True, f"text={local!r} (local OCR only)", data={"text": local})


@tool("vision.ocr_into", "Read the text in an image element (e.g. a captcha) with local OCR and type it straight into "
      "an input, in one step (no copying text between steps).",
      "image_selector=None, image_id=None, input_selector=None, input_id=None, verify=true", timeout_s=40)
async def ocr_into(ctx, image_selector=None, image_id=None, input_selector=None, input_id=None,
                   verify: bool = True) -> ToolResult:
    read = await read_text(ctx, id=image_id, selector=image_selector, verify=verify)
    if not read.ok:
        return read
    text = read.data.get("text", "")
    if not text:
        return ToolResult(False, error="OCR read nothing: refresh the image and try again")
    from .browser import browser_type

    typed = await browser_type(ctx, text=text, id=input_id, selector=input_selector, slow=True)
    alt = f"; vision model read {read.data['alt']!r}" if read.data.get("alt") else ""
    return ToolResult(typed.ok, f"read {text!r} and typed it{alt}", error=typed.error, point=typed.point,
                      data={"text": text})
