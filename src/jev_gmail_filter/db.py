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

CREATE TABLE IF NOT EXISTS item_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id  INTEGER NOT NULL REFERENCES items(id),
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,       -- 'created' | 'status' | 'manual'
    status   TEXT,
    gmail_id TEXT
);
CREATE INDEX IF NOT EXISTS item_events_at ON item_events(at);

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

    # -- queries for the web UI ----------------------------------------------

    def email(self, gmail_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM emails WHERE gmail_id = ?", (gmail_id,)).fetchone()
        return dict(row) if row else None

    def results_for(self, gmail_id: str) -> list[dict[str, Any]]:
        """The latest result per topic for one email (jevfilter TopicResult dicts)."""
        rows = self._db.execute(
            "SELECT topic, data FROM results WHERE gmail_id = ? ORDER BY created_at",
            (gmail_id,),
        )
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            latest[r["topic"]] = json.loads(r["data"])
        return list(latest.values())

    def email_page(
        self,
        *,
        kind: str = "all",
        topic: str | None = None,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Emails newest first, each with its per-topic outcomes. `kind`: all | matched |
        review | other (judged, no topic)."""
        where, args = [], []
        if kind == "matched":
            where.append(
                "EXISTS (SELECT 1 FROM results r WHERE r.gmail_id = e.gmail_id "
                "AND r.outcome = 'match')"
            )
        elif kind == "review":
            where.append(
                "EXISTS (SELECT 1 FROM reviews v WHERE v.gmail_id = e.gmail_id AND v.resolved = 0)"
            )
        elif kind == "other":
            where.append(
                "NOT EXISTS (SELECT 1 FROM results r WHERE r.gmail_id = e.gmail_id "
                "AND r.outcome != 'no')"
            )
        if topic:
            where.append(
                "EXISTS (SELECT 1 FROM results r WHERE r.gmail_id = e.gmail_id "
                "AND r.topic = ? AND r.outcome = 'match')"
            )
            args.append(topic)
        if search.strip():
            where.append("(e.subject LIKE ? OR e.sender LIKE ?)")
            args += [f"%{search.strip()}%"] * 2
        sql = "SELECT e.* FROM emails e"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.received_at DESC LIMIT ? OFFSET ?"
        out = []
        for e in self._db.execute(sql, (*args, limit, offset)).fetchall():
            outcomes = {}
            for r in self._db.execute(
                "SELECT topic, outcome, category, p FROM results WHERE gmail_id = ? "
                "ORDER BY created_at",
                (e["gmail_id"],),
            ):
                outcomes[r["topic"]] = {
                    "outcome": r["outcome"],
                    "category": r["category"],
                    "p": r["p"],
                }
            review = self._db.execute(
                "SELECT COUNT(*) FROM reviews WHERE gmail_id = ? AND resolved = 0",
                (e["gmail_id"],),
            ).fetchone()[0]
            out.append({**dict(e), "outcomes": outcomes, "in_review": bool(review)})
        return out

    def count_matched(self) -> int:
        return self._db.execute(
            "SELECT COUNT(DISTINCT gmail_id) FROM results WHERE outcome = 'match'"
        ).fetchone()[0]

    def count_emails(self, *, since: str | None = None) -> int:
        if since is None:
            return self.email_count()
        return self._db.execute(
            "SELECT COUNT(*) FROM emails WHERE received_at >= ?", (since,)
        ).fetchone()[0]

    def spend_since(self, since: str) -> float:
        return self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM results WHERE created_at >= ?", (since,)
        ).fetchone()[0]

    def match_days(self, topic: str, since: str) -> dict[str, int]:
        """Matches per day (YYYY-MM-DD, by received date) for a topic since a date."""
        rows = self._db.execute(
            "SELECT substr(e.received_at, 1, 10) AS day, COUNT(DISTINCT e.gmail_id) AS n "
            "FROM results r JOIN emails e ON e.gmail_id = r.gmail_id "
            "WHERE r.topic = ? AND r.outcome = 'match' AND e.received_at >= ? GROUP BY day",
            (topic, since),
        )
        return {r["day"]: r["n"] for r in rows}

    def recent_matches(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT e.*, r.topic, r.category, r.data FROM results r "
            "JOIN emails e ON e.gmail_id = r.gmail_id WHERE r.outcome = 'match' "
            "ORDER BY e.received_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {**{k: r[k] for k in r.keys() if k != "data"}, "result": json.loads(r["data"])}
            for r in rows
        ]

    def flagged_matches(self, flag_hint: str, since: str) -> list[dict[str, Any]]:
        """Recent matched emails where a flag whose name contains `flag_hint` is yes."""
        out = []
        for m in self._db.execute(
            "SELECT e.*, r.topic, r.data FROM results r JOIN emails e ON e.gmail_id = r.gmail_id "
            "WHERE r.outcome = 'match' AND e.received_at >= ? ORDER BY e.received_at DESC",
            (since,),
        ):
            flags = json.loads(m["data"]).get("flags") or {}
            if any(flag_hint in name and p >= 0.5 for name, p in flags.items()):
                out.append({k: m[k] for k in m.keys() if k != "data"})
        return out

    def add_event(
        self,
        item_id: int,
        kind: str,
        status: str | None,
        gmail_id: str | None = None,
        at: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO item_events(item_id, at, kind, status, gmail_id) VALUES (?, ?, ?, ?, ?)",
            (item_id, at or now(), kind, status, gmail_id),
        )

    def item_events(self, item_id: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM item_events WHERE item_id = ? ORDER BY at, id", (item_id,)
        )
        return [dict(r) for r in rows]

    def events_since(self, since: str) -> list[dict[str, Any]]:
        """Status changes since a date, newest first, with the item's topic and fields."""
        rows = self._db.execute(
            "SELECT ev.*, i.topic, i.fields FROM item_events ev JOIN items i ON i.id = ev.item_id "
            "WHERE ev.at >= ? AND ev.kind IN ('status', 'manual', 'created') "
            "ORDER BY ev.at DESC, ev.id DESC",
            (since,),
        )
        return [{**dict(r), "fields": json.loads(r["fields"])} for r in rows]

    def unlink(self, gmail_id: str, topic: str) -> None:
        """Detach an email from its items in a topic (after a correction)."""
        self._db.execute(
            "DELETE FROM item_emails WHERE gmail_id = ? AND item_id IN "
            "(SELECT id FROM items WHERE topic = ?)",
            (gmail_id, topic),
        )

    def set_outcome(self, gmail_id: str, topic: str, outcome: str, reason: str) -> None:
        """Record a person's correction on the stored result."""
        for r in self._db.execute(
            "SELECT topic_version, data FROM results WHERE gmail_id = ? AND topic = ?",
            (gmail_id, topic),
        ).fetchall():
            data = json.loads(r["data"])
            data["outcome"] = outcome
            data["reasons"] = [reason]
            self._db.execute(
                "UPDATE results SET outcome = ?, data = ? WHERE gmail_id = ? AND topic = ? "
                "AND topic_version = ?",
                (outcome, json.dumps(data), gmail_id, topic, r["topic_version"]),
            )

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
