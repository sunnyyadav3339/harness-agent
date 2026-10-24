from __future__ import annotations

import logging
import sys

from ..config import Settings
from .base import Channel, MultiChannel
from .console import ConsoleChannel

log = logging.getLogger(__name__)


def build_channel(settings: Settings, console: bool | None = None) -> Channel:
    channels: list[Channel] = []
    if settings.telegram_bot_token and settings.allowed_chat_ids():
        from .telegram import TelegramChannel

        channels.append(TelegramChannel(settings.telegram_bot_token, settings.allowed_chat_ids()))
    elif settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN set but TELEGRAM_CHAT_IDS empty; run `python -m harness telegram-setup`")
    if console if console is not None else (sys.stdin is not None and sys.stdin.isatty()):
        channels.append(ConsoleChannel())
    if not channels:
        log.warning("no human channel configured; handoffs will time out")
    return MultiChannel(channels)


__all__ = ["Channel", "ConsoleChannel", "MultiChannel", "build_channel"]
