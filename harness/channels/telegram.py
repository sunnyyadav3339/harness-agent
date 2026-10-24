"""Telegram bot channel over the plain Bot HTTP API (long polling, no webhook).

Long polling needs no public URL, which suits a laptop on a phone hotspot.
Only chat ids listed in TELEGRAM_CHAT_IDS can command the bot or answer.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging

import httpx

from .base import Channel

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_ids: set[str]) -> None:
        super().__init__()
        self.token = token
        self.chat_ids = chat_ids
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10))
        self._offset = 0
        self._poller: asyncio.Task | None = None
        self._asks: dict[str, asyncio.Future] = {}
        self._ask_options: dict[str, list[str]] = {}
        self._ids = itertools.count(1)

    async def _call(self, method: str, **kwargs) -> dict:
        resp = await self._client.post(API.format(token=self.token, method=method), **kwargs)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    # ---------------- lifecycle
    async def start(self) -> None:
        if self._poller is None:
            me = await self._call("getMe")
            log.info("telegram bot @%s ready for chats %s", me.get("username"), sorted(self.chat_ids))
            self._poller = asyncio.create_task(self._poll_loop(), name="telegram-poll")

    async def stop(self) -> None:
        if self._poller:
            self._poller.cancel()
            self._poller = None
        await self._client.aclose()

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                updates = await self._call("getUpdates", json={
                    "offset": self._offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]})
                backoff = 1.0
                for upd in updates:
                    self._offset = max(self._offset, upd["update_id"] + 1)
                    try:
                        await self._handle(upd)
                    except Exception:
                        log.exception("telegram update handling failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("telegram poll error: %s (retry in %.0fs)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    # ---------------- inbound
    async def _handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat = str(cq["message"]["chat"]["id"])
            if chat not in self.chat_ids:
                return
            ask_id, _, idx = (cq.get("data") or "").partition(":")
            fut = self._asks.get(ask_id)
            options = self._ask_options.get(ask_id, [])
            await self._call("answerCallbackQuery", json={"callback_query_id": cq["id"]})
            if fut and not fut.done() and idx.isdigit() and int(idx) < len(options):
                choice = options[int(idx)]
                fut.set_result(choice)
                try:
                    await self._call("editMessageReplyMarkup", json={
                        "chat_id": chat, "message_id": cq["message"]["message_id"], "reply_markup": {"inline_keyboard": []}})
                    await self.send(f"Got it: {choice}")
                except Exception:
                    pass
            return

        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat not in self.chat_ids:
            log.warning("ignoring telegram message from unauthorised chat %s", chat)
            return
        if text.startswith("/"):
            cmd, _, args = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            if self.on_command:
                reply = await self.on_command(cmd, args.strip(), self)
                if reply:
                    await self.send(reply)
            return
        # Free text answers the oldest open question, if any.
        for ask_id, fut in list(self._asks.items()):
            if not fut.done():
                fut.set_result(text)
                return
        if self.on_command:
            reply = await self.on_command("run", text, self)
            if reply:
                await self.send(reply)

    # ---------------- outbound
    async def send(self, text: str, image: bytes | None = None, markup: dict | None = None) -> None:
        for chat in self.chat_ids:
            if image:
                data = {"chat_id": chat, "caption": text[:1024]}
                if markup:
                    data["reply_markup"] = json.dumps(markup)
                await self._call("sendPhoto", data=data, files={"photo": ("screen.jpg", image, "image/jpeg")})
                if len(text) > 1024:
                    await self._call("sendMessage", json={"chat_id": chat, "text": text[1024:4096]})
            else:
                payload = {"chat_id": chat, "text": text[:4096]}
                if markup:
                    payload["reply_markup"] = markup
                await self._call("sendMessage", json=payload)

    async def ask(self, question: str, options: list[str], image: bytes | None = None,
                  timeout_s: float = 600) -> str | None:
        ask_id = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._asks[ask_id] = fut
        self._ask_options[ask_id] = options
        markup = {"inline_keyboard": [[{"text": o, "callback_data": f"{ask_id}:{i}"} for i, o in enumerate(options)]]}
        try:
            await self.send(f"🙋 {question}\n\n(tap a button or reply with text)", image=image, markup=markup)
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
        finally:
            self._asks.pop(ask_id, None)
            self._ask_options.pop(ask_id, None)


async def discover_chat_ids(token: str, wait_s: int = 60) -> list[tuple[str, str]]:
    """Helper for setup: returns (chat_id, name) of anyone who messaged the bot."""
    seen: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=wait_s + 10) as client:
        resp = await client.post(API.format(token=token, method="getUpdates"), json={"timeout": wait_s})
        for upd in resp.json().get("result", []):
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            if chat:
                seen[str(chat["id"])] = chat.get("username") or chat.get("first_name") or ""
    return list(seen.items())
