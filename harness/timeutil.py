"""NTP-corrected clock with millisecond deadline waits.

The system clock is never changed (that needs admin and is a system setting);
instead the measured offset is applied to every reading.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def get_tz(name: str | None) -> timezone | ZoneInfo:
    if not name:
        return IST
    try:
        return ZoneInfo(name)
    except Exception:
        return IST


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0  # seconds to add to time.time()
        self.delay = None
        self.server: str | None = None
        self.synced_at: float | None = None

    def now_ts(self) -> float:
        return time.time() + self.offset

    def now(self, tz: str | None = None) -> datetime:
        return datetime.fromtimestamp(self.now_ts(), get_tz(tz))

    def status(self) -> dict:
        return {
            "offset_ms": round(self.offset * 1000, 1),
            "delay_ms": None if self.delay is None else round(self.delay * 1000, 1),
            "server": self.server,
            "synced": self.synced_at is not None,
            "age_s": None if self.synced_at is None else round(time.time() - self.synced_at),
        }

    async def sync(self, server: str = "time.google.com", attempts: int = 3) -> bool:
        import ntplib

        client = ntplib.NTPClient()
        best = None
        for _ in range(attempts):
            try:
                resp = await asyncio.to_thread(client.request, server, version=3, timeout=2)
                if best is None or resp.delay < best.delay:
                    best = resp
            except Exception as exc:
                log.debug("ntp attempt failed: %s", exc)
        if best is None:
            log.warning("NTP sync with %s failed; using system clock", server)
            return False
        self.offset, self.delay = best.offset, best.delay
        self.server, self.synced_at = server, time.time()
        log.info("NTP offset %+.1f ms (delay %.1f ms) via %s", best.offset * 1000, best.delay * 1000, server)
        return True

    async def sleep_until(self, target_ts: float) -> float:
        """Sleep until ``target_ts`` (corrected epoch). Returns lateness in ms."""
        while True:
            remaining = target_ts - self.now_ts()
            if remaining <= 0.03:
                break
            # Coarse sleeps in chunks so a re-sync during a long wait still applies.
            await asyncio.sleep(min(remaining - 0.02, 30.0))
        # Final stretch: spin. At most ~30 ms of blocking.
        while self.now_ts() < target_ts:
            time.sleep(0)
        return round((self.now_ts() - target_ts) * 1000, 2)


def parse_target(value: str, tz: str | None = None, on_date: str | None = None) -> datetime:
    """Parse "11:00:01.200", "10:59", or "2026-10-25 11:00:01.2" in ``tz``."""
    zone = get_tz(tz)
    value = value.strip()
    if " " in value or "T" in value:
        dt = datetime.fromisoformat(value.replace(" ", "T"))
        return dt if dt.tzinfo else dt.replace(tzinfo=zone)
    parts = value.split(":")
    hour, minute = int(parts[0]), int(parts[1])
    sec = float(parts[2]) if len(parts) > 2 else 0.0
    day = date.fromisoformat(on_date) if on_date else CLOCK.now(tz).date()
    base = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    return base + timedelta(seconds=sec)


CLOCK = Clock()
