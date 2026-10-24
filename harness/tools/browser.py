"""Browser hand: drives a real Chrome over CDP via Patchright.

Chrome is started once as a detached process with its own profile and a
debugging port, then attached to. It outlives harness restarts, so a session
warmed up at 10:57 survives a crash-and-resume of the agent.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import httpx

from ..config import Settings
from ..perception import browser_view
from .base import ToolResult, tool

log = logging.getLogger(__name__)

_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw = None
        self.browser = None
        self.context = None
        self._page = None
        self._lock = asyncio.Lock()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.settings.cdp_port}"

    async def cdp_alive(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                return (await c.get(f"{self.endpoint}/json/version")).status_code == 200
        except Exception:
            return False

    def _launch_chrome(self) -> None:
        profile = self.settings.chrome_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        args = [
            self.settings.chrome_path,
            f"--remote-debugging-port={self.settings.cdp_port}",
            "--remote-allow-origins=http://127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            # Background mode parks the window off-screen: it still renders (see the
            # anti-throttling flags below) but never covers the user's work.
            *(["--start-maximized"] if self.settings.browser_foreground
              else ["--window-position=-2400,0", "--window-size=1536,864"]),
            "--disable-features=Translate,OptimizationHints,CalculateNativeWinOcclusion",
            "--disable-session-crashed-bubble",
            # Keep rendering and JS timers at full speed even when the window is
            # covered or in the background; otherwise screenshots stall and
            # page timers drift exactly when timing matters.
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "about:blank",
        ]
        log.info("launching Chrome with CDP on port %s", self.settings.cdp_port)
        # Launch through `cmd /c start`: cmd exits at once, so Chrome has no living parent
        # in our process tree. Stopping a run (Ctrl+C, /stop, taskkill /T) can then never
        # take Chrome's helper processes down with it and leave a half-dead browser.
        subprocess.Popen(["cmd.exe", "/d", "/c", "start", "", *args], creationflags=_DETACHED | 0x08000000,
                         close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @property
    def connected(self) -> bool:
        return bool(self.browser and self.browser.is_connected())

    async def ensure(self) -> None:
        async with self._lock:
            if self.connected:
                return
            if not await self.cdp_alive():
                if not Path(self.settings.chrome_path).exists():
                    raise RuntimeError(f"Chrome not found at {self.settings.chrome_path}")
                self._launch_chrome()
                # Cold start after an unclean shutdown (profile recovery) can take >10 s.
                for _ in range(120):
                    await asyncio.sleep(0.25)
                    if await self.cdp_alive():
                        break
                else:
                    raise RuntimeError("Chrome did not expose CDP within 30s")
            if self._pw is None:
                from patchright.async_api import async_playwright

                self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.connect_over_cdp(self.endpoint, timeout=15000)
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
            self.context.on("page", self._on_new_page)
            pages = [p for p in self.context.pages if not p.url.startswith(("devtools://", "chrome-extension://"))]
            self._page = pages[-1] if pages else await self.context.new_page()
            log.info("attached to Chrome (%d tab(s))", len(self.context.pages))

    def _on_new_page(self, page) -> None:
        # Follow popups / target=_blank like a person would.
        self._page = page

    async def page(self):
        await self.ensure()
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else await self.context.new_page()
        return self._page

    async def tabs(self) -> list[dict]:
        if not self.connected:
            return []
        out = []
        for i, p in enumerate(self.context.pages):
            if p.is_closed():
                continue
            try:
                title = await p.title()
            except Exception:
                title = ""
            out.append({"index": i, "title": title, "url": p.url, "active": p is self._page})
        return out

    async def ensure_painting(self, page) -> bool:
        """A minimized Chrome window stops painting, so screenshots hang.

        Foreground mode restores and maximizes it (the run must be visible). Background
        mode leaves the user's desktop alone and reports False, so the caller skips the
        screenshot and works from the DOM list. Returns whether a screenshot will work.
        """
        try:
            cdp = await self.context.new_cdp_session(page)
            try:
                win = await cdp.send("Browser.getWindowForTarget")
                if win.get("bounds", {}).get("windowState") != "minimized":
                    return True
                if not self.settings.browser_foreground:
                    return False
                await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": {"windowState": "normal"}})
                await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": {"windowState": "maximized"}})
                log.info("restored minimized Chrome window so the run is visible and observable")
                return True
            finally:
                await cdp.detach()
        except Exception as exc:
            log.debug("ensure_painting check failed: %s", exc)
            return True

    async def front(self, page) -> None:
        """Show the tab to the user, unless running in background (dev) mode."""
        if self.settings.browser_foreground:
            await page.bring_to_front()

    async def switch(self, index: int):
        pages = [p for p in self.context.pages if not p.is_closed()]
        self._page = pages[index]
        await self.front(self._page)
        return self._page

    async def close(self) -> None:
        """Detach only; Chrome keeps running for the next run."""
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = self.browser = self.context = self._page = None


# ---------------------------------------------------------------- helpers

# Same guard as the DOM indexer, run right after every click so a "show password"
# toggle is undone within milliseconds (before the recording or a screenshot sees it).
_REMASK_JS = """() => {
  const HINT = /(^|[^a-z])(pass(word|wd)?|pwd)([^a-z]|$)/; let n = 0;
  for (const el of document.querySelectorAll('input')) {
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'password') { el.dataset.agentPw = '1'; continue; }
    const h = [el.name, el.id, el.getAttribute('formcontrolname'), el.getAttribute('autocomplete')].join(' ').toLowerCase();
    if (el.dataset.agentPw === '1' || HINT.test(h)) { el.type = 'password'; el.dataset.agentPw = '1'; n++; }
  }
  return n;
}"""


async def _remask(page) -> None:
    for frame in page.frames[:4]:
        try:
            if await frame.evaluate(_REMASK_JS):
                log.warning("re-masked a revealed password field after a click")
        except Exception:
            pass


# Blur the focused element, scroll the document; return whether anything moved.
_SCROLL_JS = """(dy) => {
  if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
  const before = window.scrollY; window.scrollBy(0, dy); return window.scrollY !== before;
}"""


# Returns a short description of whatever covers the element's centre, or null.
_COVER_JS = """(el, [x, y]) => {
  const hit = document.elementFromPoint(x, y);
  if (!hit || el === hit || el.contains(hit) || hit.contains(el)) return null;
  const cls = typeof hit.className === 'string' ? hit.className.split(/\\s+/).slice(0, 2).join('.') : '';
  return `<${hit.tagName.toLowerCase()}${cls ? '.' + cls : ''}>`;
}"""

async def _locate(ctx, id=None, selector=None, text=None):
    page = await ctx.browser.page()
    if id is not None:
        snap = ctx.browser_snapshot
        el = snap.by_id(int(id)) if snap else None
        if el is None:
            raise LookupError(f"element [{id}] is not in the latest observation; re-observe and use a current id")
        frame = el.frame or page.main_frame
        return page, frame.locator(f'[data-agent-id="{int(id)}"]').first, el
    if selector:
        return page, page.locator(selector).first, None
    if text:
        return page, page.get_by_text(text, exact=False).first, None
    raise ValueError("give one of id, selector or text")


def _point(el, fallback_box=None):
    if el is not None:
        x, y = el.center
        return ("browser", x, y)
    if fallback_box:
        return ("browser", int(fallback_box["x"] + fallback_box["width"] / 2),
                int(fallback_box["y"] + fallback_box["height"] / 2))
    return None


async def _after(page, url_before: str) -> bool:
    changed = page.url != url_before
    if changed:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
    return changed


# ---------------------------------------------------------------- tools

@tool("browser.open", "Navigate the active tab (or a new tab) to a URL.", "url, new_tab=false", timeout_s=45)
async def browser_open(ctx, url: str, new_tab: bool = False) -> ToolResult:
    await ctx.browser.ensure()
    if new_tab:
        page = await ctx.browser.context.new_page()
        ctx.browser._page = page
    else:
        page = await ctx.browser.page()
    if "://" not in url:
        url = "https://" + url
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    await ctx.browser.front(page)
    status = resp.status if resp else "?"
    return ToolResult(True, f"opened {page.url} (HTTP {status})", changed_view=True)


@tool("browser.click", "Click an element by observation id (preferred), CSS/Playwright selector, or visible text.",
      "id=None, selector=None, text=None, double=false")
async def browser_click(ctx, id=None, selector=None, text=None, double: bool = False) -> ToolResult:
    page, loc, el = await _locate(ctx, id, selector, text)
    before = page.url
    box = None
    try:
        if double:
            await loc.dblclick(timeout=4000)
        else:
            await loc.click(timeout=4000)
    except Exception as first:
        # Overlays / animations: fall back to a raw mouse click at the element centre.
        try:
            box = await loc.bounding_box(timeout=2000)
        except Exception:
            box = None
        if box is None and el is not None:
            x, y, w, h = el.rect
            box = {"x": x, "y": y, "width": w, "height": h}
        if box is None:
            return ToolResult(False, error=f"click failed: {str(first).splitlines()[0]}")
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        # Only fall back to a raw mouse click if the target is really on top;
        # otherwise we would silently click whatever overlay covers it.
        try:
            cover = await loc.evaluate(_COVER_JS, [cx, cy])
        except Exception:
            cover = None
        if cover:
            return ToolResult(False, error=f"element is covered by {cover}; close that overlay first "
                                           f"(Escape, its close button, or click elsewhere)")
        await page.mouse.click(cx, cy, click_count=2 if double else 1)
    await _remask(page)
    changed = await _after(page, before)
    label = f"[{id}]" if id is not None else (selector or text)
    return ToolResult(True, f"clicked {label}" + (f"; navigated to {page.url}" if changed else ""),
                      point=_point(el, box), changed_view=changed)


@tool("browser.type", "Type text into a field. slow=true sends real key events (for autocompletes).",
      "text, id=None, selector=None, clear=true, enter=false, slow=false")
async def browser_type(ctx, text: str, id=None, selector=None, clear: bool = True, enter: bool = False,
                       slow: bool = False) -> ToolResult:
    page, loc, el = await _locate(ctx, id, selector)
    before = page.url
    await loc.click(timeout=4000)
    if clear:
        await loc.fill("", timeout=4000)
    if slow:
        await loc.press_sequentially(text, delay=45, timeout=15000)
    else:
        await loc.fill(text, timeout=4000)
    if enter:
        await loc.press("Enter")
    changed = await _after(page, before)
    shown = "***" if (el and el.attrs.get("type") == "password") else text
    return ToolResult(True, f"typed {shown!r}", point=_point(el), changed_view=changed)


@tool("browser.select", "Choose an option in a <select> by visible label or value.", "value, id=None, selector=None")
async def browser_select(ctx, value: str, id=None, selector=None) -> ToolResult:
    page, loc, el = await _locate(ctx, id, selector)
    try:
        chosen = await loc.select_option(label=value, timeout=4000)
    except Exception:
        chosen = await loc.select_option(value=value, timeout=4000)
    return ToolResult(True, f"selected {chosen}", point=_point(el))


@tool("browser.press", "Press a key or chord in the page, e.g. Enter, Escape, Tab, Control+A.", "key")
async def browser_press(ctx, key: str) -> ToolResult:
    page = await ctx.browser.page()
    before = page.url
    await page.keyboard.press(key)
    changed = await _after(page, before)
    return ToolResult(True, f"pressed {key}", changed_view=changed)


@tool("browser.scroll", "Scroll the page (direction up/down, pixels) or scroll an element into view.",
      "direction='down', amount=600, id=None")
async def browser_scroll(ctx, direction: str = "down", amount: int = 600, id=None) -> ToolResult:
    page = await ctx.browser.page()
    if id is not None:
        _, loc, _ = await _locate(ctx, id)
        await loc.scroll_into_view_if_needed(timeout=4000)
        return ToolResult(True, f"scrolled [{id}] into view")
    dy = -abs(int(amount)) if direction == "up" else abs(int(amount))
    # Never use the mouse wheel over a focused field: on a number input (e.g. a passenger's
    # age) the wheel silently changes the value. Blur first and scroll the document itself;
    # only fall back to the wheel (at a neutral spot) for pages that scroll an inner container.
    moved = await page.evaluate(_SCROLL_JS, dy)
    if not moved:
        vw, vh = await page.evaluate("[window.innerWidth, window.innerHeight]")
        await page.mouse.move(vw - 12, vh // 2)
        await page.mouse.wheel(0, dy)
    await asyncio.sleep(0.25)
    return ToolResult(True, f"scrolled {direction} {abs(dy)}px")


@tool("browser.hover", "Move the mouse over an element.", "id=None, selector=None")
async def browser_hover(ctx, id=None, selector=None) -> ToolResult:
    _, loc, el = await _locate(ctx, id, selector)
    await loc.hover(timeout=4000)
    return ToolResult(True, "hovered", point=_point(el))


@tool("browser.wait_for", "Wait until text/selector appears (or disappears with gone=true) or the URL contains a string.",
      "text=None, selector=None, url_contains=None, gone=false, timeout_s=10", timeout_s=320)
async def browser_wait_for(ctx, text=None, selector=None, url_contains=None, gone: bool = False,
                           timeout_s: float = 10) -> ToolResult:
    page = await ctx.browser.page()
    ms = int(float(timeout_s) * 1000)
    state = "hidden" if gone else "visible"
    try:
        if url_contains:
            await page.wait_for_url(f"**{url_contains}**", timeout=ms)
            return ToolResult(True, f"url now {page.url}", changed_view=True)
        if selector:
            await page.locator(selector).first.wait_for(state=state, timeout=ms)
        elif text:
            await page.get_by_text(text, exact=False).first.wait_for(state=state, timeout=ms)
        else:
            await asyncio.sleep(float(timeout_s))
        return ToolResult(True, f"condition met ({'gone' if gone else 'present'})")
    except Exception:
        return ToolResult(False, error=f"timed out after {timeout_s}s waiting for {text or selector or url_contains}")


@tool("browser.read", "Read the full visible text of an element or the whole page (more than the observation shows).",
      "id=None, selector=None, max_chars=4000")
async def browser_read(ctx, id=None, selector=None, max_chars: int = 4000) -> ToolResult:
    page = await ctx.browser.page()
    if id is None and selector is None:
        text = await page.inner_text("body", timeout=5000)
    else:
        _, loc, _ = await _locate(ctx, id, selector)
        text = await loc.inner_text(timeout=5000)
    text = " ".join(text.split())
    return ToolResult(True, text[: int(max_chars)])


@tool("browser.eval", "Run a JavaScript expression in the page and return its JSON result (for data extraction).",
      "script")
async def browser_eval(ctx, script: str) -> ToolResult:
    page = await ctx.browser.page()
    value = await page.evaluate(script)
    return ToolResult(True, str(value)[:4000])


@tool("browser.tabs", "List open tabs.", "")
async def browser_tabs(ctx) -> ToolResult:
    await ctx.browser.ensure()
    tabs = await ctx.browser.tabs()
    return ToolResult(True, "\n".join(f"{t['index']}: {t['title'][:60]} {t['url'][:80]}{' (active)' if t['active'] else ''}" for t in tabs))


@tool("browser.switch_tab", "Make the tab at index active.", "index")
async def browser_switch_tab(ctx, index: int) -> ToolResult:
    await ctx.browser.ensure()
    page = await ctx.browser.switch(int(index))
    return ToolResult(True, f"active tab: {page.url}", changed_view=True)


@tool("browser.back", "Go back in history.", "")
async def browser_back(ctx) -> ToolResult:
    page = await ctx.browser.page()
    await page.go_back(wait_until="domcontentloaded", timeout=15000)
    return ToolResult(True, f"back to {page.url}", changed_view=True)


@tool("browser.reload", "Reload the page. Avoid on single-page apps that drop the session on reload.", "")
async def browser_reload(ctx) -> ToolResult:
    page = await ctx.browser.page()
    await page.reload(wait_until="domcontentloaded", timeout=30000)
    return ToolResult(True, "reloaded", changed_view=True)


@tool("browser.focus", "Bring the automated Chrome window to the foreground.", "")
async def browser_focus(ctx) -> ToolResult:
    page = await ctx.browser.page()
    if not ctx.settings.browser_foreground:
        return ToolResult(True, "background mode: browser left unfocused (pages are still observed)")
    await page.bring_to_front()
    from .. import platform_win

    title = await page.title()
    for w in platform_win.list_windows(60):
        if w.exe == "chrome.exe" and title and title[:30] in w.title:
            platform_win.focus_window(w.hwnd)
            break
    return ToolResult(True, "browser focused", changed_view=True)


@tool("browser.screenshot", "Save a PNG of the page as proof. Relative paths go into this run's folder.",
      "path='page.png', full_page=true, selector=None")
async def browser_screenshot(ctx, path: str = "page.png", full_page: bool = True, selector=None) -> ToolResult:
    page = await ctx.browser.page()
    target = Path(path)
    if not target.is_absolute():
        target = ctx.run.dir / target
    target.parent.mkdir(parents=True, exist_ok=True)
    if selector:
        await page.locator(selector).first.screenshot(path=str(target), timeout=10000)
    else:
        await page.screenshot(path=str(target), full_page=bool(full_page), timeout=15000)
    ctx.run.add_artifact(target)
    return ToolResult(True, f"saved {target}", data={"path": str(target)})


async def observe_browser(ctx, busy_selectors: list[str] | None = None, max_elements: int = 160):
    page = await ctx.browser.page()
    can_paint = await ctx.browser.ensure_painting(page)
    await browser_view.settle(page, busy_selectors)
    return await browser_view.snapshot(page, max_elements=max_elements, tabs=await ctx.browser.tabs(),
                                       screenshot=can_paint)
