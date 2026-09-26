"""Local SQLite storage: emails, jevfilter results, items and the review queue.

jevfilter is stateless, so everything that must survive between runs is
stored here: full results (so thresholds can be retuned without calling
Jev again), tracked items, and decisions made in the review queue.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    gmail_id     TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    sender       TEXT NOT NULL,
    subject      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    snippet      TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS emails_thread ON emails(thread_id);

CREATE TABLE IF NOT EXISTS results (
    gmail_id        TEXT NOT NULL,
    topic           TEXT NOT NULL,
    topic_version   TEXT NOT NULL,
    wording_version INTEGER NOT NULL,
    outcome         TEXT NOT NULL,
    p               REAL NOT NULL,
    category        TEXT,
    data            TEXT NOT NULL,   -- jevfilter TopicResult.to_dict()
    model           TEXT,
    cost_usd        REAL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (gmail_id, topic, topic_version)
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT NOT NULL,
    fields        TEXT NOT NULL,     -- JSON
    status        TEXT,
    last_stage    TEXT,              -- last pipeline (non-terminal) status
    stale         INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_email_at TEXT,
    notes         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL      -- 'email' | 'manual'
);
CREATE INDEX IF NOT EXISTS items_topic ON items(topic);

CREATE TABLE IF NOT EXISTS item_emails (
    item_id  INTEGER NOT NULL REFERENCES items(id),
    gmail_id TEXT NOT NULL,
    category TEXT,
    PRIMARY KEY (item_id, gmail_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id    TEXT NOT NULL,
    topic       TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- 'topic' (does it belong?) | 'item' (which item?)
    reasons     TEXT NOT NULL,       -- JSON list
    suggestion  TEXT NOT NULL,       -- JSON: the jevfilter result behind it
    resolved    INTEGER NOT NULL DEFAULT 0,
    decision    TEXT,                -- JSON
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Item:
    id: int
    topic: str
    fields: dict[str, Any]
    status: str | None
    last_stage: str | None
    stale: bool
    last_email_at: str | None
    source: str
    notes: str = ""


@dataclass(frozen=True)
class Review:
    id: int
    gmail_id: str
    topic: str
    kind: str
    reasons: list[str]
    suggestion: dict[str, Any]
    subject: str = ""
    sender: str = ""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # CLI commands are short-lived; don't leave the connection to the GC.
        try:
            self._db.close()
        except Exception:
            pass

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._db:
            yield

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- emails and results ------------------------------------------------

    def save_email(
        self,
        gmail_id: str,
        thread_id: str,
        sender: str,
        subject: str,
        received_at: str,
        snippet: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO emails VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(gmail_id) DO UPDATE "
            "SET processed_at = excluded.processed_at",
            (gmail_id, thread_id, sender, subject, received_at, snippet, now()),
        )

    def judged_versions(self, gmail_id: str) -> dict[str, set[str]]:
        """topic → the topic versions this email has been judged against."""
        rows = self._db.execute(
            "SELECT topic, topic_version FROM results WHERE gmail_id = ?", (gmail_id,)
        )
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(r["topic"], set()).add(r["topic_version"])
        return out

    def save_result(
        self,
        gmail_id: str,
        result: dict[str, Any],
        *,
        wording_version: int,
        model: str | None,
        cost_usd: float | None,
    ) -> None:
        category = (result.get("category") or {}).get("value")
        self._db.execute(
            "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gmail_id,
                result["topic"],
                result["topic_version"],
                wording_version,
                result["outcome"],
                result["p"],
                category,
                json.dumps(result),
                model,
                cost_usd,
                now(),
            ),
        )

    def matches(self, topic: str, version: str) -> list[tuple[str, dict[str, Any]]]:
        rows = self._db.execute(
            "SELECT gmail_id, data FROM results WHERE topic = ? AND topic_version = ? "
            "AND outcome = 'match'",
            (topic, version),
        )
        return [(r["gmail_id"], json.loads(r["data"])) for r in rows]

    def recent_emails(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest judged emails with each topic's latest outcome and category."""
        emails = self._db.execute(
            "SELECT * FROM emails ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for e in emails:
            rows = self._db.execute(
                "SELECT topic, outcome, category, p FROM results WHERE gmail_id = ? "
                "ORDER BY created_at",
                (e["gmail_id"],),
            )
            outcomes = {r["topic"]: (r["outcome"], r["category"], r["p"]) for r in rows}
            out.append({**dict(e), "outcomes": outcomes})
        return out

    def item_emails(self, item_id: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT e.subject, e.sender, e.received_at, ie.category FROM item_emails ie "
            "JOIN emails e ON e.gmail_id = ie.gmail_id WHERE ie.item_id = ? "
            "ORDER BY e.received_at DESC",
            (item_id,),
        )
        return [dict(r) for r in rows]

    def set_item_status(self, item_id: int, status: str | None, last_stage: str | None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE items SET status = ?, last_stage = ? WHERE id = ?",
                (status, last_stage, item_id),
            )

    def set_item_notes(self, item_id: int, notes: str) -> None:
        with self._db:
            self._db.execute("UPDATE items SET notes = ? WHERE id = ?", (notes, item_id))

    def topic_stats(self, topic: str) -> dict[str, Any]:
        """Matches, the latest matching email's date, and open reviews for a topic."""
        row = self._db.execute(
            "SELECT COUNT(DISTINCT r.gmail_id), MAX(e.received_at) FROM results r "
            "JOIN emails e ON e.gmail_id = r.gmail_id WHERE r.topic = ? AND r.outcome = 'match'",
            (topic,),
        ).fetchone()
        reviews = self._db.execute(
            "SELECT COUNT(*) FROM reviews WHERE topic = ? AND resolved = 0", (topic,)
        ).fetchone()[0]
        return {"matches": row[0] or 0, "last_match": row[1], "reviews": reviews}

    def email_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    def total_cost(self) -> float:
        return self._db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM results").fetchone()[0]

    # -- items ---------------------------------------------------------------

    def _item(self, row: sqlite3.Row) -> Item:
        return Item(
            row["id"],
            row["topic"],
            json.loads(row["fields"]),
            row["status"],
            row["last_stage"],
            bool(row["stale"]),
            row["last_email_at"],
            row["source"],
            row["notes"],
        )

    def items(self, topic: str) -> list[Item]:
        rows = self._db.execute("SELECT * FROM items WHERE topic = ? ORDER BY id", (topic,))
        return [self._item(r) for r in rows]

    def item(self, item_id: int) -> Item | None:
        row = self._db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def item_for_thread(self, topic: str, thread_id: str) -> Item | None:
        row = self._db.execute(
            "SELECT i.* FROM items i JOIN item_emails ie ON ie.item_id = i.id "
            "JOIN emails e ON e.gmail_id = ie.gmail_id "
            "WHERE i.topic = ? AND e.thread_id = ? ORDER BY i.id LIMIT 1",
            (topic, thread_id),
        ).fetchone()
        return self._item(row) if row else None

    def recent_subjects(self, item_id: int, limit: int = 3) -> list[str]:
        rows = self._db.execute(
            "SELECT e.subject FROM item_emails ie JOIN emails e ON e.gmail_id = ie.gmail_id "
            "WHERE ie.item_id = ? ORDER BY e.received_at DESC LIMIT ?",
            (item_id, limit),
        )
        return [r["subject"] for r in rows]

    def create_item(
        self,
        topic: str,
        fields: dict[str, Any],
        status: str | None,
        last_stage: str | None,
        source: str = "email",
        last_email_at: str | None = None,
        notes: str = "",
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO items(topic, fields, status, last_stage, created_at, last_email_at, "
            "notes, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (topic, json.dumps(fields), status, last_stage, now(), last_email_at, notes, source),
        )
        return int(cur.lastrowid)

    def update_item(
        self,
        item_id: int,
        *,
        status: str | None,
        last_stage: str | None,
        fields: dict[str, Any] | None = None,
        last_email_at: str | None = None,
    ) -> None:
        item = self.item(item_id)
        assert item is not None
        merged = {**item.fields, **(fields or {})}
        latest = max(filter(None, [item.last_email_at, last_email_at]), default=None)
        self._db.execute(
            "UPDATE items SET status = ?, last_stage = ?, fields = ?, last_email_at = ?, "
            "stale = 0 WHERE id = ?",
            (status, last_stage, json.dumps(merged), latest, item_id),
        )

    def link(self, item_id: int, gmail_id: str, category: str | None) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO item_emails VALUES (?, ?, ?)", (item_id, gmail_id, category)
        )

    def set_stale(self, item_id: int, stale: bool) -> None:
        self._db.execute("UPDATE items SET stale = ? WHERE id = ?", (int(stale), item_id))

    # -- reviews -------------------------------------------------------------

    def open_review(
        self, gmail_id: str, topic: str, kind: str, reasons: list[str], suggestion: dict[str, Any]
    ) -> int:
        existing = self._db.execute(
            "SELECT id FROM reviews WHERE gmail_id = ? AND topic = ? AND kind = ? AND resolved = 0",
            (gmail_id, topic, kind),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = self._db.execute(
            "INSERT INTO reviews(gmail_id, topic, kind, reasons, suggestion, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (gmail_id, topic, kind, json.dumps(reasons), json.dumps(suggestion), now()),
        )
        return int(cur.lastrowid)

    def forget(self, gmail_id: str) -> None:
        """Drop an email's results and open reviews so it's judged again."""
        with self._db:
            self._db.execute("DELETE FROM results WHERE gmail_id = ?", (gmail_id,))
            self._db.execute("DELETE FROM reviews WHERE gmail_id = ? AND resolved = 0", (gmail_id,))

    def reviews(self, *, include_resolved: bool = False) -> list[Review]:
        where = "" if include_resolved else "WHERE r.resolved = 0"
        rows = self._db.execute(
            "SELECT r.*, e.subject, e.sender FROM reviews r "
            f"LEFT JOIN emails e ON e.gmail_id = r.gmail_id {where} ORDER BY r.id"
        )
        return [
            Review(
                r["id"],
                r["gmail_id"],
                r["topic"],
                r["kind"],
                json.loads(r["reasons"]),
                json.loads(r["suggestion"]),
                r["subject"] or "",
                r["sender"] or "",
            )
            for r in rows
        ]

    def review(self, review_id: int) -> Review | None:
        return next((r for r in self.reviews(include_resolved=True) if r.id == review_id), None)

    def resolve_review(self, review_id: int, decision: dict[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "UPDATE reviews SET resolved = 1, decision = ?, resolved_at = ? WHERE id = ?",
                (json.dumps(decision), now(), review_id),
            )
