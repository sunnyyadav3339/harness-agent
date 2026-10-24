from __future__ import annotations

import asyncio
import sys

from .base import Channel


class ConsoleChannel(Channel):
    """Terminal fallback for local development; ignored when stdin is not a TTY."""

    name = "console"

    async def send(self, text: str, image: bytes | None = None) -> None:
        print(f"\n[agent] {text}" + ("  (screenshot attached in run log)" if image else ""), flush=True)

    async def ask(self, question: str, options: list[str], image: bytes | None = None,
                  timeout_s: float = 600) -> str | None:
        if not sys.stdin or not sys.stdin.isatty():
            await asyncio.sleep(timeout_s)
            return None
        prompt = f"\n[agent needs you] {question}\nOptions: {' / '.join(options)} (or type a reply): "
        try:
            answer = await asyncio.wait_for(asyncio.to_thread(input, prompt), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
        answer = answer.strip()
        for opt in options:
            if answer.lower() == opt.lower() or (answer.isdigit() and int(answer) == options.index(opt) + 1):
                return opt
        return answer or None
