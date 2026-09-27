"""The core loop: new email → jevfilter → items and statuses → storage and labels.

All judging goes through jevfilter; this module decides what to do with
the answers. Each email is judged once per topic version, so editing a
topic (which changes its version) makes old mail eligible for a rescan.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jevfilter as jf
from jevfilter import track

from . import candidates
from .db import Item, Store
from .gmail import MailSource, inbox_query, parse_categories
from .mail import Email

DEFAULT_BACKSCAN = timedelta(days=14)
SYNC_OVERLAP = timedelta(days=1)
"""Each sync re-lists mail from a day before the last one started, so nothing slips
through (mail arriving mid-sync, clock skew); already-judged mail is skipped unfetched."""


@dataclass
class SyncReport:
    scanned: int = 0
    judged: int = 0
    skipped: int = 0
    matched: Counter[str] = field(default_factory=Counter)
    reviews: int = 0
    items_created: int = 0
    items_updated: int = 0
    cost_usd: float = 0.0
    labelled: int = 0  # emails labelled by apply_labels_to_matches
    stopped: str | None = None  # why the sync stopped early (e.g. budget)
    errors: list[str] = field(default_factory=list)


def label_for(topic: jf.Topic) -> str | None:
    """The topic's Gmail label: `meta.gmail_label`, its name, or None if disabled."""
    meta = topic.meta if isinstance(topic.meta, dict) else {}
    label = meta.get("gmail_label", topic.name)
    return None if label is False or label is None else str(label)


class Pipeline:
    def __init__(
        self,
        store: Store,
        source: MailSource,
        topics: jf.Topics,
        *,
        judge: Any = None,
        budget: jf.Budget | None = None,
        write_labels: bool = True,
    ):
        self.store = store
        self.source = source
        self.topics = topics
        self.judge = judge
        self.budget = budget
        self.write_labels = write_labels
        self._filters: dict[tuple[str, ...], jf.Filter] = {}

    def _filter(self, topics: list[jf.Topic]) -> jf.Filter:
        key = tuple(t.name for t in topics)
        if key not in self._filters:
            # Staged: most mail matches no topic, so ask membership first.
            self._filters[key] = jf.Filter(
                topics, judge=self.judge, budget=self.budget, speculative=False
            )
        return self._filters[key]

    # -- one email -------------------------------------------------------------

    def process(self, email: Email, report: SyncReport | None = None) -> bool:
        """Judge `email` against every topic it hasn't been judged against yet.

        Returns False if there was nothing to do.
        """
        report = report if report is not None else SyncReport()
        judged = self.store.judged_versions(email.id)
        pending = [t for t in self.topics.values() if t.version not in judged.get(t.name, set())]
        if not pending:
            report.skipped += 1
            return False

        content = jf.Content(email.state(), candidates=self._candidates(email, pending))
        f = self._filter(pending)
        result = f.judge(content)
        report.judged += 1
        report.cost_usd += result.cost_usd or 0.0

        labels: list[str] = []
        with self.store.transaction():
            self.store.save_email(
                email.id,
                email.thread_id,
                email.sender,
                email.subject,
                email.date.isoformat(),
                email.snippet,
            )
            for tr in result:
                self.store.save_result(
                    email.id,
                    tr.to_dict(),
                    model=result.model,
                    wording_version=result.wording_version,
                    cost_usd=(result.cost_usd or 0.0) / max(len(result), 1),
                )
                topic = self.topics[tr.topic]
                if tr.outcome == "review":
                    self.store.open_review(
                        email.id, topic.name, "topic", list(tr.reasons), tr.to_dict()
                    )
                    report.reviews += 1
                elif tr.outcome == "match":
                    report.matched[topic.name] += 1
                    labels += self._labels(topic, tr)
                    if topic.track is not None:
                        self._track(f, topic, tr, email, content, report)
        self._apply_labels(email.id, labels, report)
        return True

    def _candidates(self, email: Email, topics: list[jf.Topic]) -> dict[str, dict[str, list[str]]]:
        out: dict[str, dict[str, list[str]]] = {}
        for t in topics:
            if not t.fields:
                continue
            items = self.store.items(t.name)
            out[t.name] = {
                name: candidates.extract(
                    email, spec.kind, known=[i.fields.get(name) for i in items]
                )
                for name, spec in t.fields.items()
            }
        return out

    def _labels(self, topic: jf.Topic, tr: jf.TopicResult) -> list[str]:
        base = label_for(topic)
        if base is None:
            return []
        return [base] + ([f"{base}/{tr.category.value}"] if tr.category else [])

    def _apply_labels(self, gmail_id: str, labels: list[str], report: SyncReport) -> None:
        if not labels or not self.write_labels:
            return
        try:
            self.source.add_labels(gmail_id, list(dict.fromkeys(labels)))
        except Exception as e:  # a label failure shouldn't lose the judgment
            report.errors.append(f"labelling {gmail_id}: {e}")

    # -- tracked items -----------------------------------------------------------

    def _track(
        self,
        f: jf.Filter,
        topic: jf.Topic,
        tr: jf.TopicResult,
        email: Email,
        content: jf.Content,
        report: SyncReport,
        *,
        forced: int | str | None = None,
        edits: dict[str, str] | None = None,
    ) -> None:
        """Link the email to an item (existing or new) and move its status.

        `forced` is a decision from the review queue: an item id, or "new".
        `edits` are field values a person typed in, overriding Jev's.
        """
        edits = {k: v for k, v in (edits or {}).items() if v}
        item: Item | None = None
        if forced is not None:
            item = None if forced == "new" else self.store.item(int(forced))
        else:
            item = self.store.item_for_thread(topic.name, email.thread_id)
            # Without the value items are matched on (say, no company), the
            # other fields alone (a common job title) would merge unrelated
            # items, so such an email starts its own.
            if item is None and not self._missing_match_on(topic, tr, edits):
                existing = self.store.items(topic.name)
                if existing:
                    m = f.match_item(
                        content, topic, self._item_views(existing), result=tr, fields=edits
                    )
                    report.cost_usd += m.cost_usd or 0.0
                    if m.outcome == "review":
                        self.store.open_review(
                            email.id,
                            topic.name,
                            "item",
                            list(m.reasons),
                            {"topic_result": tr.to_dict(), "match": m.to_dict()},
                        )
                        report.reviews += 1
                        return
                    item = self.store.item(m.item_id) if m.item_id is not None else None

        category = tr.category.value if tr.category else None
        values = {k: v.value for k, v in tr.fields.items() if v.value} | edits
        pipeline_statuses = set(topic.track.statuses) if topic.track else set()
        when = email.date.isoformat()
        if item is None:
            status = track.initial_status(topic, category)
            item_id = self.store.create_item(
                topic.name,
                values,
                status,
                status if status in pipeline_statuses else None,
                last_email_at=when,
            )
            self.store.add_event(item_id, "created", status, email.id, at=when)
            report.items_created += 1
        else:
            status = track.next_status(topic, item.status, category, last_stage=item.last_stage)
            last_stage = status if status in pipeline_statuses else item.last_stage
            missing = {k: v for k, v in values.items() if not item.fields.get(k)}
            self.store.update_item(
                item.id, status=status, last_stage=last_stage, fields=missing, last_email_at=when
            )
            if status != item.status:
                self.store.add_event(item.id, "status", status, email.id, at=when)
            item_id = item.id
            report.items_updated += 1
        self.store.link(item_id, email.id, category)

    def _item_views(self, items: list[Item]) -> list[dict[str, Any]]:
        return [
            {
                "id": i.id,
                "fields": i.fields,
                "status": i.status,
                "summary": {"recent_subjects": self.store.recent_subjects(i.id)},
            }
            for i in items
        ]

    # -- sync ----------------------------------------------------------------------

    def sync(
        self, *, since: datetime | None = None, limit: int | None = None, progress: Any = None
    ) -> SyncReport:
        """Process new inbox mail in the chosen Gmail categories.

        Always a Gmail search, so "Primary" means exactly what Gmail's own
        `category:primary` means for this account (Gmail's change feed labels
        mail differently: on the first real run it tagged most of what Gmail
        shows as Primary `CATEGORY_UPDATES`, so a label filter missed it).

        First run (or `since=`): the backscan window chosen at setup. After
        that: everything since a day before the last sync started. Already
        judged mail is skipped before it's fetched, so the overlap is cheap.
        `last_sync_at` only advances when a sync finishes, so an interrupted
        run is simply resumed.
        """
        report = SyncReport()
        started = datetime.now(UTC)
        last = self.store.get_meta("last_sync_at")
        if since is not None:
            after = since
        elif last:
            after = datetime.fromisoformat(last) - SYNC_OVERLAP
        else:
            chosen = self.store.get_meta("backscan_days")  # the window picked at setup
            after = started - (timedelta(days=float(chosen)) if chosen else DEFAULT_BACKSCAN)
        found = list(reversed(list(self.source.search(inbox_query(self.categories(), after)))))
        ids = []
        for gmail_id in found:  # oldest first, so item statuses move forward in order
            if self._judged(gmail_id):
                report.skipped += 1
            else:
                ids.append(gmail_id)

        truncated = limit is not None and len(ids) > limit
        if truncated:
            ids = ids[:limit]
        for n, gmail_id in enumerate(ids, 1):
            report.scanned += 1
            try:
                email = self.source.get(gmail_id)
            except Exception as e:  # e.g. Gmail's rate limit, after its own retries
                report.stopped = (
                    f"Gmail refused a request ({_short(e)}); run sync again to continue"
                )
                break
            try:
                self.process(email, report)
            except jf.BudgetExceeded as e:
                report.stopped = f"spend cap reached ({e}); run sync again to continue"
                break
            except jf.JudgeError as e:
                report.stopped = f"Jev failed ({e}); run sync again to continue"
                break
            if progress:
                progress(n, len(ids))

        if report.stopped is None and not truncated:
            self.store.set_meta("last_sync_at", started.isoformat())
        self.refresh_stale()
        return report

    @staticmethod
    def _missing_match_on(topic: jf.Topic, tr: jf.TopicResult, edits: dict[str, str]) -> bool:
        keys = topic.track.match_on if topic.track else ()
        return any(not (edits.get(k) or (tr.fields.get(k) and tr.fields[k].value)) for k in keys)

    def _judged(self, gmail_id: str) -> bool:
        """Already judged against every current topic version (no need to fetch it)."""
        judged = self.store.judged_versions(gmail_id)
        return all(t.version in judged.get(t.name, set()) for t in self.topics.values())

    def categories(self) -> tuple[str, ...]:
        """The Gmail inbox categories this app reads (Settings; default Primary only)."""
        return parse_categories(self.store.get_meta("categories"))

    def refresh_stale(self, now: datetime | None = None) -> int:
        """Recompute stale flags for tracked items. Returns how many are stale."""
        now = now or datetime.now(UTC)
        count = 0
        with self.store.transaction():
            for topic in self.topics.values():
                if topic.track is None:
                    continue
                for item in self.store.items(topic.name):
                    stale = bool(item.last_email_at) and track.is_stale(
                        topic, datetime.fromisoformat(item.last_email_at), now, status=item.status
                    )
                    self.store.set_stale(item.id, stale)
                    count += stale
        return count

    # -- review queue ------------------------------------------------------------------

    def resolve(self, review_id: int, decision: str, fields: dict[str, str] | None = None) -> str:
        """Apply a person's decision. Topic reviews: "yes" / "no". Item reviews:
        an item id or "new". `fields` are values the person filled in or
        corrected (e.g. a company Jev couldn't find). Returns a short
        description of what happened."""
        fields = {k: v.strip() for k, v in (fields or {}).items() if v and v.strip()}
        review = self.store.review(review_id)
        if review is None:
            raise ValueError(f"no review #{review_id}")
        topic = self.topics.get(review.topic)
        if topic is None:
            raise ValueError(f"topic {review.topic!r} no longer exists")
        report = SyncReport()

        if review.kind == "topic":
            if decision not in ("yes", "no"):
                raise ValueError("a topic review takes 'yes' or 'no'")
            outcome = "not in topic"
            if decision == "yes":
                tr = _as_match(review.suggestion)
                self._apply_labels(review.gmail_id, self._labels(topic, tr), report)
                outcome = "added to topic"
                if topic.track is not None:
                    email = self.source.get(review.gmail_id)
                    content = jf.Content(email.state(), candidates=self._candidates(email, [topic]))
                    with self.store.transaction():
                        self._track(
                            self._filter([topic]), topic, tr, email, content, report, edits=fields
                        )
                    if report.reviews:
                        outcome += "; which item it belongs to needs a decision too"
        else:
            if decision != "new" and not decision.isdigit():
                raise ValueError("an item review takes an item id or 'new'")
            if decision.isdigit() and self.store.item(int(decision)) is None:
                raise ValueError(f"no item #{decision}")
            tr = jf.TopicResult.from_dict(review.suggestion["topic_result"])
            email = self.source.get(review.gmail_id)
            content = jf.Content(email.state())
            with self.store.transaction():
                self._track(
                    self._filter([topic]), topic, tr, email, content, report, forced=decision
                )
            outcome = "new item created" if decision == "new" else f"linked to item #{decision}"
        self.store.resolve_review(
            review_id, {"decision": decision} | ({"fields": fields} if fields else {})
        )
        if report.errors:
            outcome += f" (warning: {report.errors[0]})"
        return outcome

    def recheck_reviews(self, progress: Any = None) -> SyncReport:
        """Judge every email in the review queue again, with the current code and
        topics (e.g. after a fix or a topic edit). Answers you already gave stay."""
        report = SyncReport()
        ids = list(dict.fromkeys(r.gmail_id for r in self.store.reviews()))
        for n, gmail_id in enumerate(ids, 1):
            report.scanned += 1
            try:
                email = self.source.get(gmail_id)
            except Exception as e:
                report.stopped = f"Gmail refused a request ({_short(e)}); try again"
                break
            self.store.forget(gmail_id)
            try:
                self.process(email, report)
            except (jf.BudgetExceeded, jf.JudgeError) as e:
                report.stopped = f"{e}; try again"
                break
            if progress:
                progress(n, len(ids))
        return report

    def mark_wrong(self, gmail_id: str, topic_name: str) -> str:
        """A person says a match is wrong: take the labels off, detach the email from
        its item, and record the correction (kept for tuning)."""
        topic = self.topics.get(topic_name)
        if topic is None:
            raise ValueError(f"no topic {topic_name!r}")
        stored = next(
            (r for r in self.store.results_for(gmail_id) if r["topic"] == topic_name), None
        )
        labels = self._labels(topic, jf.TopicResult.from_dict(stored)) if stored else []
        if labels and self.write_labels:
            try:
                self.source.remove_labels(gmail_id, labels)
            except Exception as e:  # keep the correction even if Gmail refuses
                return f"corrected, but Gmail kept the labels ({_short(e)})"
        with self.store.transaction():
            self.store.unlink(gmail_id, topic_name)
            self.store.set_outcome(gmail_id, topic_name, "no", "user_corrected")
            for r in self.store.reviews():
                if r.gmail_id == gmail_id and r.topic == topic_name:
                    self.store.resolve_review(r.id, {"decision": "no", "via": "correction"})
        return f"removed from {topic_name}"

    def set_item_status(self, item_id: int, status: str) -> None:
        """A person moves an item by hand (recorded in its history)."""
        item = self.store.item(item_id)
        if item is None:
            raise ValueError(f"no item #{item_id}")
        topic = self.topics.get(item.topic)
        pipeline_statuses = set(topic.track.statuses) if topic and topic.track else set()
        last_stage = status if status in pipeline_statuses else item.last_stage
        with self.store.transaction():
            self.store.set_item_status(item_id, status, last_stage)
            self.store.add_event(item_id, "manual", status)

    def apply_labels_to_matches(self, progress: Any = None) -> SyncReport:
        """Label every email already judged a match (e.g. after a dry run)."""
        report = SyncReport()
        todo = []
        for topic in self.topics.values():
            for gmail_id, data in self.store.matches(topic.name, topic.version):
                labels = self._labels(topic, jf.TopicResult.from_dict(data))
                if labels:
                    todo.append((gmail_id, labels))
        for i, (gmail_id, labels) in enumerate(todo):
            if progress:
                progress(i, len(todo))
            self._apply_labels(gmail_id, labels, report)
            report.labelled += 1
        return report


def _as_match(suggestion: dict[str, Any]) -> jf.TopicResult:
    tr = jf.TopicResult.from_dict(suggestion)
    tr.outcome = "match"
    return tr


def _short(error: Exception) -> str:
    text = str(error)
    if "Quota exceeded" in text or "rateLimitExceeded" in text:
        return "rate limit reached; wait a minute"
    return text[:160]
