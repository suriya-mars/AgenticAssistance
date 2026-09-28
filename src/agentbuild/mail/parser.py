"""MIME -> clean MailMessage. Attachments are listed by metadata only, never opened."""
from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

from agentbuild.guardrails.input import html_to_text


@dataclass
class MailMessage:
    uid: str
    message_id: str
    sender: str            # bare address, lower-cased
    sender_name: str
    to: str
    subject: str
    date: str
    body: str              # plain text (raw, NOT yet sanitized)
    in_reply_to: str = ""
    references: str = ""
    attachments: list[dict] = field(default_factory=list)


def _body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        content = part.get_payload(decode=True).decode("utf-8", errors="replace")
    return html_to_text(content) if part.get_content_type() == "text/html" else content


def parse_message(raw: bytes, uid: str) -> MailMessage:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    name, addr = parseaddr(str(msg.get("From", "")))
    attachments = [
        {"filename": p.get_filename(), "type": p.get_content_type(),
         "size": len(p.get_payload(decode=True) or b"")}
        for p in msg.iter_attachments()
    ]
    return MailMessage(
        uid=uid,
        message_id=str(msg.get("Message-ID", "")).strip() or f"<uid-{uid}>",
        sender=addr.lower(),
        sender_name=name,
        to=str(msg.get("To", "")),
        subject=str(msg.get("Subject", "")),
        date=str(msg.get("Date", "")),
        body=_body_text(msg),
        in_reply_to=str(msg.get("In-Reply-To", "") or ""),
        references=str(msg.get("References", "") or ""),
        attachments=attachments,
    )
