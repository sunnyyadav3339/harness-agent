"""Skills from a demonstration: ``python -m harness learn --name <skill>``.

Records what a person does once, then asks the model to generalise it into a
SKILL.md draft:
* OS level (pynput): clicks with the window title, the UIA control under the
  pointer and a crop around it; typed text grouped into strings; hotkeys.
* Browser level (when the agent's Chrome is open): DOM clicks and field
  changes with a stable selector and visible text.
Password fields are never recorded (browser: type=password; desktop: UIA
IsPassword). Stop with Ctrl+Shift+Q (or Ctrl+C in the terminal).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from pathlib import Path

from .. import platform_win
from ..llm.parsing import parse_json_object
from ..llm.router import get_router
from ..perception.annotate import draw_crosshair
from ..perception.screen import grab
from ..timeutil import CLOCK
from .reflector import save_draft_skill

log = logging.getLogger(__name__)

_DOM_LISTENER = r"""
(() => {
  if (window.__demoInstalled) return; window.__demoInstalled = true; window.__demoEvents = [];
  const sel = (el) => {
    if (!el || !el.tagName) return '';
    for (const a of ['id', 'formcontrolname', 'name', 'aria-label', 'placeholder']) {
      const v = el.getAttribute(a);
      if (v && !/^\d+$/.test(v)) return `${el.tagName.toLowerCase()}[${a}="${v.slice(0, 60)}"]`;
    }
    const t = (el.innerText || '').trim().slice(0, 40);
    return t ? `${el.tagName.toLowerCase()}:has-text("${t}")` : el.tagName.toLowerCase();
  };
  const push = (e) => { window.__demoEvents.push({...e, t: Date.now(), url: location.href}); };
  document.addEventListener('click', (ev) => push({kind: 'click', selector: sel(ev.target),
      text: (ev.target.innerText || ev.target.value || '').trim().slice(0, 80)}), true);
  document.addEventListener('change', (ev) => {
    const el = ev.target; const pw = (el.type || '') === 'password';
    push({kind: 'change', selector: sel(el), value: pw ? '<password>' : String(el.value || '').slice(0, 120)});
  }, true);
})()
"""


class _Recorder:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.events: list[dict] = []
        self.frames: list[Path] = []
        self._buf = ""
        self._mods: set[str] = set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._uia = None

    # ------------------------------------------------ helpers
    def _uia_at(self, x: int, y: int) -> dict:
        try:
            from pywinauto import Desktop

            if self._uia is None:
                self._uia = Desktop(backend="uia")
            el = self._uia.from_point(x, y)
            info = el.element_info
            return {"control": info.control_type, "name": (info.name or "")[:80],
                    "automation_id": (info.automation_id or "")[:40]}
        except Exception:
            return {}

    @staticmethod
    def _focused_is_password() -> bool:
        try:
            from pywinauto.uia_defines import IUIA

            focused = IUIA().iuia.GetFocusedElement()
            return bool(focused.CurrentIsPassword)
        except Exception:
            return False

    def _flush_text(self) -> None:
        if self._buf:
            text = "<password>" if self._focused_is_password() else self._buf
            self._add({"kind": "type", "text": text})
            self._buf = ""

    def _add(self, ev: dict) -> None:
        fg = platform_win.foreground_window()
        ev.update({"t": round(time.time(), 2), "window": fg.title[:80] if fg else "", "exe": fg.exe if fg else ""})
        with self._lock:
            self.events.append(ev)

    # ------------------------------------------------ pynput callbacks
    def on_click(self, x, y, button, pressed):
        if not pressed or self._stop.is_set():
            return
        self._flush_text()
        ev = {"kind": "click", "x": int(x), "y": int(y), "button": str(button).split(".")[-1], **self._uia_at(int(x), int(y))}
        try:
            shot = grab()
            n = len(self.frames) + 1
            crop = shot.crop((max(x - 160, 0), max(y - 120, 0), min(x + 160, shot.width), min(y + 120, shot.height)))
            path = self.out_dir / f"click_{n:03d}.jpg"
            draw_crosshair(crop, (min(160, x), min(120, y))).save(path, quality=80)
            self.frames.append(path)
            ev["frame"] = path.name
        except Exception:
            pass
        self._add(ev)

    def on_press(self, key):
        if self._stop.is_set():
            return False
        from pynput import keyboard

        name = getattr(key, "name", None)
        if name in ("ctrl_l", "ctrl_r", "shift", "shift_r", "alt_l", "alt_r", "cmd"):
            self._mods.add(name.split("_")[0])
            return
        char = getattr(key, "char", None)
        if {"ctrl", "shift"} <= self._mods and (char or "").lower() in ("q", "\x11"):
            self._stop.set()
            return False
        if self._mods & {"ctrl", "alt", "cmd"}:
            self._flush_text()
            self._add({"kind": "hotkey", "keys": "+".join(sorted(self._mods) + [name or (char or "?")])})
            return
        if char and char.isprintable():
            self._buf += char
        elif key == keyboard.Key.space:
            self._buf += " "
        elif key == keyboard.Key.backspace:
            self._buf = self._buf[:-1]
        elif name in ("enter", "tab", "esc"):
            self._flush_text()
            self._add({"kind": "key", "key": name})

    def on_release(self, key):
        name = getattr(key, "name", None) or ""
        self._mods.discard(name.split("_")[0])


async def _poll_browser(rec: _Recorder, settings) -> None:
    from ..tools.browser import BrowserManager

    bm = BrowserManager(settings)
    if not await bm.cdp_alive():
        return
    try:
        await bm.ensure()
        await bm.context.add_init_script(_DOM_LISTENER)
        for p in bm.context.pages:
            try:
                await p.evaluate(_DOM_LISTENER)
            except Exception:
                pass
        while not rec._stop.is_set():
            for p in list(bm.context.pages):
                try:
                    evs = await p.evaluate("() => { const e = window.__demoEvents || []; window.__demoEvents = []; return e; }")
                except Exception:
                    continue
                for e in evs:
                    rec._add({"kind": f"web_{e['kind']}", **{k: v for k, v in e.items() if k != "kind"}})
            await asyncio.sleep(0.7)
    finally:
        await bm.close()


def _trace_text(events: list[dict]) -> str:
    lines = []
    t0 = events[0]["t"] if events else 0
    for e in events:
        rel = f"+{e['t'] - t0:5.1f}s"
        where = f"[{e.get('exe', '')}: {e.get('window', '')[:50]}]"
        if e["kind"] == "click":
            target = f"{e.get('control', '?')} '{e.get('name', '')}'" if e.get("control") else f"({e['x']},{e['y']})"
            lines.append(f"{rel} {where} click {target}")
        elif e["kind"] == "type":
            lines.append(f"{rel} {where} type {e['text']!r}")
        elif e["kind"] in ("hotkey", "key"):
            lines.append(f"{rel} {where} key {e.get('keys') or e.get('key')}")
        elif e["kind"].startswith("web_"):
            detail = e.get("value") if e["kind"] == "web_change" else e.get("text", "")
            lines.append(f"{rel} [web {e.get('url', '')[:60]}] {e['kind'][4:]} {e.get('selector', '')} {detail!r}")
    return "\n".join(lines)


DEMO_PROMPT = """A person demonstrated a computer task once on Windows 11. Below is the recorded event trace
(OS clicks with the UI control under the pointer, typed text, keys, and browser DOM events with selectors).
Write a reusable skill file (SKILL.md) that teaches an agent to do this kind of task.
Generalise specific values (names, dates, file names, search terms) into placeholders described in the text,
keep stable selectors/menu names, and add verification after each step plus a short recovery section.
Format:
---
name: {name}
description: <one sentence: what it does and when to use it>
triggers: [<3-6 lowercase phrases>]
draft: true
---
# <Title>
## Preconditions
## Steps
## Recovery Playbook
## Learned Heuristics (auto-updated)

Reply with JSON {{"skill_md": "<full file>"}}.

Skill name: {name}
What the person said the task is: {description}
Trace:
{trace}"""


async def record_demo(settings, name: str, description: str = "", max_minutes: float = 15) -> Path | None:
    from pynput import keyboard, mouse

    out_dir = settings.runs_dir / f"demo_{CLOCK.now():%Y%m%d_%H%M%S}_{name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = _Recorder(out_dir)
    print(f"Recording demo '{name}'. Perform the task now; press Ctrl+Shift+Q when done.")
    ml = mouse.Listener(on_click=rec.on_click)
    kl = keyboard.Listener(on_press=rec.on_press, on_release=rec.on_release)
    ml.start()
    kl.start()
    browser_task = asyncio.create_task(_poll_browser(rec, settings))
    deadline = time.time() + max_minutes * 60
    try:
        while not rec._stop.is_set() and time.time() < deadline:
            await asyncio.sleep(0.3)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        rec._stop.set()
        rec._flush_text()
        ml.stop()
        kl.stop()
        browser_task.cancel()
    events = sorted(rec.events, key=lambda e: e["t"])
    (out_dir / "demo.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    trace = _trace_text(events)
    (out_dir / "trace.txt").write_text(trace, encoding="utf-8")
    print(f"captured {len(events)} events -> {out_dir}")
    if not events:
        return None

    prompt = DEMO_PROMPT.format(name=name, description=description or "(not given)", trace=trace[:12000])
    content: list[dict] = [{"type": "text", "text": prompt}]
    for frame in rec.frames[:6]:  # a few keyframes help the model name controls
        b64 = base64.b64encode(frame.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    router = get_router()
    resp = await router.complete("vision", [{"role": "user", "content": content}], max_tokens=2000,
                                 temperature=0.2, deadline_s=90)
    md = parse_json_object(resp.text).get("skill_md", "")
    path = save_draft_skill(settings.skills_dir, md)
    (out_dir / "drafted_skill.txt").write_text(str(path), encoding="utf-8")
    await router.aclose()
    return path
