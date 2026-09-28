"""Mail sources. IMAP is strictly read-only: SELECT readonly + BODY.PEEK (never sets \\Seen)."""
from __future__ import annotations

import imaplib
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from agentbuild.mail.parser import MailMessage, parse_message


class MailSource(Protocol):
    def list_unread(self, limit: int = 20) -> list[MailMessage]: ...
    def get(self, uid: str) -> MailMessage: ...
    def search_subject(self, text: str, since_days: int = 60, limit: int = 50) -> list[MailMessage]: ...


class IMAPSource:
    def __init__(self, host: str, user: str, password_env: str = "AGENT_IMAP_PASSWORD",
                 folder: str = "INBOX", port: int = 993):
        self.host, self.user, self.folder, self.port = host, user, folder, port
        self.password_env = password_env

    def _password(self) -> str:
        pw = os.environ.get(self.password_env)
        if pw:
            return pw
        try:
            import keyring  # optional dependency
            pw = keyring.get_password("agentbuild-imap", self.user)
        except ImportError:
            pw = None
        if not pw:
            raise RuntimeError(f"IMAP password not found: set ${self.password_env} or keyring 'agentbuild-imap'")
        return pw

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        conn.login(self.user, self._password())
        typ, _ = conn.select(self.folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot select folder {self.folder}")
        return conn

    def _fetch(self, conn: imaplib.IMAP4_SSL, uid: str) -> MailMessage:
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"fetch failed for uid {uid}")
        return parse_message(data[0][1], uid)

    def list_unread(self, limit: int = 20) -> list[MailMessage]:
        conn = self._connect()
        try:
            typ, data = conn.uid("SEARCH", None, "UNSEEN")
            uids = data[0].split() if typ == "OK" and data[0] else []
            uids = [u.decode() for u in uids][-limit:]  # newest N
            return [self._fetch(conn, u) for u in reversed(uids)]
        finally:
            conn.logout()

    def get(self, uid: str) -> MailMessage:
        conn = self._connect()
        try:
            return self._fetch(conn, uid)
        finally:
            conn.logout()

    def search_subject(self, text: str, since_days: int = 60, limit: int = 50) -> list[MailMessage]:
        """Read AND unread mails whose subject contains `text` (e.g. a bug id). Still read-only."""
        if '"' in text or "\\" in text:
            raise ValueError("search text may not contain quotes or backslashes")
        since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        conn = self._connect()
        try:
            typ, data = conn.uid("SEARCH", None, f'(SINCE "{since}" SUBJECT "{text}")')
            uids = [u.decode() for u in (data[0].split() if typ == "OK" and data[0] else [])][-limit:]
            return [self._fetch(conn, u) for u in uids]
        finally:
            conn.logout()

    def append_draft(self, raw: bytes, folder: str) -> None:
        """The ONLY write this class does: put a message in the Drafts folder, flagged as a draft."""
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self._password())
            typ, resp = conn.append(folder, r"(\Draft)", imaplib.Time2Internaldate(time.time()), raw)
            if typ != "OK":
                raise RuntimeError(f"could not save draft to '{folder}': {resp}")
        finally:
            conn.logout()

    def check_login(self) -> None:
        self._connect().logout()


class LocalEmlSource:
    """Reads *.eml files from a directory - for offline development, tests and evals."""
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def list_unread(self, limit: int = 20) -> list[MailMessage]:
        files = sorted(self.dir.glob("*.eml"))[:limit]
        return [parse_message(f.read_bytes(), f.stem) for f in files]

    def get(self, uid: str) -> MailMessage:
        return parse_message((self.dir / f"{uid}.eml").read_bytes(), uid)

    def search_subject(self, text: str, since_days: int = 60, limit: int = 50) -> list[MailMessage]:
        t = text.lower()
        msgs = [parse_message(f.read_bytes(), f.stem) for f in sorted(self.dir.glob("*.eml"))]
        return [m for m in msgs if t in m.subject.lower()][:limit]
