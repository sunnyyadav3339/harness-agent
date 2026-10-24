"""Email-arrival trigger over IMAP, built to survive long unattended runs.

* IMAP IDLE for push, re-issued every 9 minutes (Gmail drops idle sessions
  after ~10-29 min; RFC 2177 says re-IDLE before 29 min).
* Any socket error -> reconnect with exponential backoff; if IDLE is not
  supported the watcher silently degrades to polling.
* Processed mail is tracked by UID (+UIDVALIDITY) in a local state file, and
  messages are fetched with BODY.PEEK, so the mailbox is never modified and
  nothing is processed twice across restarts.
* Auth: app password (Gmail requires 2-step verification; revoked when the
  account password changes) or an OAuth2 access token via XOAUTH2.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import imaplib
import json
import logging
import select
import threading
import time
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

IDLE_RENEW_S = 9 * 60


@dataclass
class MailEvent:
    uid: int
    sender: str
    subject: str
    body: str
    date: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for text, enc in email.header.decode_header(value):
        parts.append(text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text)
    return "".join(parts)


def _body(msg: Message, limit: int = 2000) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")[:limit]
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")[:limit]


class ImapWatcher:
    def __init__(self, host: str, port: int, user: str, password: str = "", oauth_token: str = "",
                 state_file: Path | None = None, mailbox: str = "INBOX", poll_s: int = 60) -> None:
        self.host, self.port, self.user = host, port, user
        self.password, self.oauth_token = password, oauth_token
        self.mailbox = mailbox
        self.poll_s = poll_s
        self.state_file = state_file
        self._stop = threading.Event()
        self._state = {"uidvalidity": None, "last_uid": 0}
        if state_file and state_file.exists():
            try:
                self._state.update(json.loads(state_file.read_text()))
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self._state))

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
        if self.oauth_token:
            auth = f"user={self.user}\x01auth=Bearer {self.oauth_token}\x01\x01".encode()
            conn.authenticate("XOAUTH2", lambda _: auth)
        else:
            conn.login(self.user, self.password)
        typ, data = conn.select(self.mailbox, readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"select {self.mailbox} failed")
        uidvalidity = None
        for resp in conn.untagged_responses.get("UIDVALIDITY", []):
            uidvalidity = int(resp)
        if uidvalidity != self._state.get("uidvalidity"):
            # New mailbox identity: start from "now" rather than replaying history.
            self._state = {"uidvalidity": uidvalidity, "last_uid": self._max_uid(conn)}
            self._save()
        return conn

    @staticmethod
    def _max_uid(conn) -> int:
        typ, data = conn.uid("SEARCH", None, "ALL")
        uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
        return max(uids) if uids else 0

    def _fetch_new(self, conn) -> list[MailEvent]:
        start = int(self._state.get("last_uid", 0)) + 1
        typ, data = conn.uid("SEARCH", None, f"UID {start}:*")
        if typ != "OK":
            return []
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) >= start)
        events = []
        for uid in uids:
            typ, msg_data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            events.append(MailEvent(
                uid=uid,
                sender=_decode(msg.get("From")),
                subject=_decode(msg.get("Subject")),
                body=_body(msg),
                date=msg.get("Date", ""),
            ))
            self._state["last_uid"] = uid
        if uids:
            self._save()
        return events

    def _idle(self, conn, timeout: float) -> bool:
        """Block in IDLE until the server reports new mail or timeout. Returns True if mail arrived."""
        tag = conn._new_tag().decode()
        conn.send(f"{tag} IDLE\r\n".encode())
        first = conn.readline()
        if not first.startswith(b"+"):
            raise imaplib.IMAP4.error(f"IDLE refused: {first!r}")
        got = False
        deadline = time.time() + timeout
        try:
            while not self._stop.is_set() and time.time() < deadline:
                sock = conn.sock
                pending = getattr(sock, "pending", lambda: 0)()
                ready = pending or select.select([sock], [], [], min(5.0, deadline - time.time()))[0]
                if not ready:
                    continue
                line = conn.readline()
                if not line:
                    raise ConnectionError("IMAP connection closed")
                if b"EXISTS" in line or b"RECENT" in line:
                    got = True
                    break
        finally:
            conn.send(b"DONE\r\n")
            while True:
                line = conn.readline()
                if not line or line.startswith(tag.encode()):
                    break
        return got

    def run_forever(self, on_mail: Callable[[MailEvent], None]) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            conn = None
            try:
                conn = self._connect()
                log.info("IMAP watcher connected to %s as %s", self.host, self.user)
                backoff = 2.0
                caps = conn.capabilities
                for ev in self._fetch_new(conn):
                    on_mail(ev)
                while not self._stop.is_set():
                    if "IDLE" in caps:
                        self._idle(conn, IDLE_RENEW_S)
                    else:
                        self._stop.wait(self.poll_s)
                        conn.noop()
                    for ev in self._fetch_new(conn):
                        on_mail(ev)
            except Exception as exc:
                if self._stop.is_set():
                    break
                log.warning("IMAP watcher error: %s; reconnecting in %.0fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass


async def start_watcher(watcher: ImapWatcher, on_mail: Callable[[MailEvent], None]) -> asyncio.Future:
    loop = asyncio.get_running_loop()

    def _cb(ev: MailEvent) -> None:
        loop.call_soon_threadsafe(on_mail, ev)

    return loop.run_in_executor(None, watcher.run_forever, _cb)
