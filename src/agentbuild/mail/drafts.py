"""Drafts are never sent. They are saved locally (.eml) or appended to your mail Drafts folder via IMAP,
where you review and press Send yourself."""
from __future__ import annotations

import mimetypes
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from agentbuild.mail.parser import MailMessage


def build_message(*, from_addr: str, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                  attachments: list[Path] | None = None, in_reply_to: str = "", references: str = "") -> EmailMessage:
    m = EmailMessage()
    m["From"] = from_addr
    m["To"] = ", ".join(to)
    if cc:
        m["Cc"] = ", ".join(cc)
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid()
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = f"{references} {in_reply_to}".strip()
    m["X-Agent-Draft"] = "true"
    m.set_content(body)
    for a in attachments or []:
        ctype, _ = mimetypes.guess_type(a.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        m.add_attachment(a.read_bytes(), maintype=maintype, subtype=subtype, filename=a.name)
    return m


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60]


class DraftSink(Protocol):
    label: str

    def save(self, msg: EmailMessage, name: str) -> str: ...


class LocalDraftSink:
    """Writes .eml files you can open in any mail client."""
    def __init__(self, directory: Path):
        self.dir = directory
        self.label = f"local folder {directory}"

    def save(self, msg: EmailMessage, name: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{_safe(name)}.eml"
        path.write_bytes(bytes(msg))
        return str(path)


class IMAPDraftSink:
    """Appends to your real Drafts folder (flagged \\Draft). Keeps a local .eml copy for the audit trail."""
    def __init__(self, imap, folder: str, local_copy_dir: Path):
        self.imap, self.folder = imap, folder
        self.local = LocalDraftSink(local_copy_dir)
        self.label = f"mail Drafts folder '{folder}' ({imap.user})"

    def save(self, msg: EmailMessage, name: str) -> str:
        self.imap.append_draft(bytes(msg), self.folder)
        copy = self.local.save(msg, name)
        return f"{self.folder} (copy: {copy})"


def save_draft(original: MailMessage, subject: str, body: str, drafts_dir: Path, from_addr: str = "") -> Path:
    m = build_message(from_addr=from_addr, to=[original.sender], subject=subject, body=body,
                      in_reply_to=original.message_id, references=original.references)
    return Path(LocalDraftSink(drafts_dir).save(m, f"draft_{original.uid}"))
