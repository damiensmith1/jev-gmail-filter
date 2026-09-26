"""Fake Gmail and a scripted Jev judge for tests. No network."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from jevfilter.judges import FakeJudge

from jev_gmail_filter.gmail import HistoryExpired
from jev_gmail_filter.mail import Email

NOW = datetime.now(UTC)


def email(
    id, subject, body="", *, sender="Acme Careers <jobs@acme.example>", thread=None, days_ago=1.0
) -> Email:
    return Email(
        id,
        thread or f"t-{id}",
        sender,
        subject,
        NOW - timedelta(days=days_ago),
        body,
        snippet=body[:50],
    )


class FakeMail:
    def __init__(self, emails=(), address="me@example.com"):
        self.emails = {e.id: e for e in emails}
        self.addr = address
        self.history = 100
        self.pending: list[str] = []
        self.labels: dict[str, list[str]] = {}
        self.expired = False
        self.fetched: list[str] = []

    def address(self):
        return self.addr

    def history_id(self):
        return str(self.history)

    def search(self, query):
        m = re.search(r"after:(\d+)", query)
        after = datetime.fromtimestamp(int(m.group(1)), tz=UTC) if m else None
        found = [e for e in self.emails.values() if after is None or e.date > after]
        return iter(e.id for e in sorted(found, key=lambda e: e.date, reverse=True))

    def get(self, message_id):
        self.fetched.append(message_id)
        return self.emails[message_id]

    def new_since(self, history_id):
        if self.expired:
            raise HistoryExpired()
        ids, self.pending = self.pending, []
        return ids, str(self.history)

    def add_labels(self, message_id, names):
        self.labels.setdefault(message_id, []).extend(names)

    def deliver(self, e: Email) -> None:
        self.emails[e.id] = e
        self.pending.append(e.id)
        self.history += 1


def _body(state):
    return state["content"]["subject"] + " " + state["content"]["body"]


def _tag(state, name, default=None):
    m = re.search(rf"\b{name}=(\S+)", _body(state))
    return m.group(1) if m else default


def judge() -> FakeJudge:
    """Scripted Jev. Tags in the email text drive the answers:

    [jobs] / [receipt] / [unsure] → membership; cat=<category>; item=<id|new>;
    the company is the first candidate that appears in the text.
    """

    def jobs(state, q):
        text = _body(state)
        return 0.95 if "[jobs]" in text else 0.5 if "[unsure]" in text else 0.02

    def company(state, q):
        text = _body(state)
        return next((c for c in q.criteria if c != "none of these" and c in text), "none of these")

    def item(state, q):
        chosen = _tag(state, "item", "new")
        return "new" if chosen == "new" else f"item {chosen}"

    return FakeJudge(
        {
            "Jobs/membership": jobs,
            "Receipts/membership": lambda s, q: 0.9 if "[receipt]" in _body(s) else 0.01,
            "Jobs/categories": lambda s, q: _tag(s, "cat", "applied"),
            "Jobs/fields/company": company,
            "Jobs/fields/role": "none of these",
            "Jobs/item": item,
        }
    )
