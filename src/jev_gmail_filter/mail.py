"""The email model and parsing Gmail API messages into it."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

MAX_BODY_CHARS = 6000
"""Bodies are trimmed before judging; jevfilter truncates further if needed."""


@dataclass(frozen=True)
class Email:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: datetime
    body: str
    snippet: str = ""
    label_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def sender_name(self) -> str:
        return parseaddr(self.sender)[0].strip().strip('"')

    @property
    def sender_email(self) -> str:
        return parseaddr(self.sender)[1].lower()

    def state(self) -> dict[str, Any]:
        """What Jev sees."""
        return {
            "from": self.sender,
            "subject": self.subject,
            "date": self.date.isoformat(),
            "body": self.body[:MAX_BODY_CHARS],
        }


def parse_message(msg: dict[str, Any]) -> Email:
    """Turn a Gmail API `messages.get(format="full")` response into an Email."""
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    millis = int(msg.get("internalDate", "0"))
    return Email(
        id=msg["id"],
        thread_id=msg.get("threadId", msg["id"]),
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        date=datetime.fromtimestamp(millis / 1000, tz=UTC),
        body=_body(payload) or html.unescape(msg.get("snippet", "")),
        snippet=html.unescape(msg.get("snippet", "")),
        label_ids=tuple(msg.get("labelIds", [])),
    )


def _body(part: dict[str, Any]) -> str:
    plain = _find(part, "text/plain")
    if plain:
        return _clean(plain)
    rich = _find(part, "text/html")
    return _clean(html_to_text(rich)) if rich else ""


def _find(part: dict[str, Any], mime: str) -> str:
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        return _decode(part["body"]["data"])
    for child in part.get("parts", []) or []:
        found = _find(child, mime)
        if found:
            return found
    return ""


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    return "".join(parser.parts)
