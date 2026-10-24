"""Out-of-band human channel: notify, ask-and-wait, and receive chat commands."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, str, "Channel"], Awaitable[str | None]]


class Channel:
    name = "base"

    def __init__(self) -> None:
        self.on_command: CommandHandler | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, text: str, image: bytes | None = None) -> None:
        raise NotImplementedError

    async def ask(self, question: str, options: list[str], image: bytes | None = None,
                  timeout_s: float = 600) -> str | None:
        """Return the human's answer (an option or free text), or None on timeout."""
        raise NotImplementedError


class MultiChannel(Channel):
    """Broadcast to every channel; the first answer from any of them wins."""

    name = "multi"

    def __init__(self, channels: list[Channel]) -> None:
        super().__init__()
        self.channels = channels

    @property
    def on_command(self):  # type: ignore[override]
        return self._on_command

    @on_command.setter
    def on_command(self, handler) -> None:
        self._on_command = handler
        for ch in getattr(self, "channels", []):
            ch.on_command = handler

    async def start(self) -> None:
        for ch in self.channels:
            await ch.start()

    async def stop(self) -> None:
        for ch in self.channels:
            await ch.stop()

    async def send(self, text: str, image: bytes | None = None) -> None:
        await asyncio.gather(*(self._safe_send(ch, text, image) for ch in self.channels))

    @staticmethod
    async def _safe_send(ch: Channel, text: str, image: bytes | None) -> None:
        try:
            await ch.send(text, image)
        except Exception as exc:
            log.warning("%s send failed: %s", ch.name, exc)

    async def ask(self, question: str, options: list[str], image: bytes | None = None,
                  timeout_s: float = 600) -> str | None:
        tasks = [asyncio.create_task(ch.ask(question, options, image, timeout_s)) for ch in self.channels]
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if not t.cancelled() and t.exception() is None and t.result() is not None:
                        return t.result()
            return None
        finally:
            for t in tasks:
                t.cancel()
