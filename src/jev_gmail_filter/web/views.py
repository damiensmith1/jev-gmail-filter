"""What each page shows, as plain data for the templates. No HTTP here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any

import jevfilter as jf

from ..db import Item, Store
from ..gmail import CATEGORIES, parse_categories
from ..pipeline import label_for

DEFAULT_THRESHOLDS = {"accept": 0.7, "reject": 0.3, "min_confidence": 0.5}


# -- formatting ------------------------------------------------------------------------


def parse(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


def local(iso: str | None) -> datetime | None:
    dt = parse(iso)
    return dt.astimezone() if dt else None


def clock(iso: str | None, now: datetime | None = None) -> str:
    """11:02 today, Thu this week, Sep 12 before that."""
    dt = local(iso)
    if dt is None:
        return ""
    now = (now or datetime.now(UTC)).astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if (now.date() - dt.date()).days < 7:
        return dt.strftime("%a")
    return dt.strftime("%b %-d")


def ago(iso: str | None, now: datetime | None = None) -> str:
    dt = parse(iso)
    if dt is None:
        return "never"
    seconds = ((now or datetime.now(UTC)) - dt).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def days_since(iso: str | None, now: datetime | None = None) -> int:
    dt = parse(iso)
    return 0 if dt is None else max(((now or datetime.now(UTC)) - dt).days, 0)


def sender_name(sender: str) -> str:
    name, address = parseaddr(sender or "")
    return (name or address or sender or "").strip().strip('"')


def fields_text(fields: dict[str, Any]) -> str:
    return " · ".join(str(v) for v in fields.values() if v) or "(no details yet)"


def iso_ago(days: float, now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()


def gmail_link(thread_id: str | None) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else "#"


# -- the side nav ----------------------------------------------------------------------


def nav(store: Store, topics: jf.Topics, active: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    tracked = [t for t in topics.values() if t.track]
    categories = parse_categories(store.get_meta("categories"))
    return {
        "active": active,
        "needs": len(store.reviews()),
        "emails": store.email_count(),
        "item_count": sum(len(store.items(t.name)) for t in tracked),
        "has_items": bool(tracked),
        "topics": [
            {"name": name, "matches": store.topic_stats(name)["matches"]} for name in topics
        ],
        "synced": ago(store.get_meta("last_sync_at"), now),
        "reading": " + ".join(CATEGORIES[c][0] for c in categories),
        "spend_month": store.spend_since(month_start),
        "labels_on": store.get_meta("labels") == "on",
    }


# -- overview ----------------------------------------------------------------------------


def overview(store: Store, topics: jf.Topics, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    week, day = iso_ago(7, now), iso_ago(1, now)
    tracked = [t for t in topics.values() if t.track]
    items = [(t, i) for t in tracked for i in store.items(t.name)]
    reviews = store.reviews()
    events = store.events_since(week)

    hour = now.astimezone().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"

    cards = []
    days = [(now - timedelta(days=13 - i)).astimezone().strftime("%Y-%m-%d") for i in range(14)]
    for t in topics.values():
        per_day = store.match_days(t.name, iso_ago(14, now))
        week_count = sum(per_day.get(d, 0) for d in days[7:])
        peak = max([per_day.get(d, 0) for d in days] + [1])
        if t.track:
            its = store.items(t.name)
            quiet = sum(i.stale for i in its)
            summary = f"{len(its)} tracked" + (f" · {quiet} gone quiet" if quiet else "")
        else:
            latest = next((m for m in store.recent_matches(50) if m["topic"] == t.name), None)
            summary = f"Latest: {latest['subject']}" if latest else "No matches yet"
        cards.append(
            {
                "name": t.name,
                "week": week_count,
                "summary": summary,
                "bars": [round(per_day.get(d, 0) / peak * 100) for d in days],
            }
        )

    return {
        "greeting": greeting,
        "new_since_yesterday": store.count_emails(since=day),
        "stats": {
            "needs": len(reviews),
            "judged_week": store.count_emails(since=week),
            "tracked": len(items),
            "moved": len({e["item_id"] for e in events}),
            "quiet": sum(i.stale for _, i in items),
        },
        "reviews": [review_row(r, store) for r in reviews[:3]],
        "cards": cards,
        "latest": [
            {
                "from": sender_name(m["sender"]),
                "subject": m["subject"],
                "tag": m["topic"] + (f" · {m['category']}" if m["category"] else ""),
                "time": clock(m["received_at"], now),
                "gmail_id": m["gmail_id"],
            }
            for m in store.recent_matches(5)
        ],
        "today": now.astimezone().strftime("%A, %b %-d"),
        "owed": [
            {"who": sender_name(m["sender"]), "what": m["subject"], "gmail_id": m["gmail_id"]}
            for m in store.flagged_matches("reply", iso_ago(14, now))[:4]
        ],
        "has_reply_flag": any("reply" in f for t in topics.values() for f in t.flags),
        "quiet": [
            {
                "who": fields_text(i.fields),
                "status": i.status or "",
                "days": days_since(i.last_email_at, now),
                "id": i.id,
                "topic": t.name,
            }
            for t, i in items
            if i.stale
        ][:5],
        "moved": [
            {
                "day": clock(e["at"], now),
                "who": fields_text(e["fields"]),
                "to": e["status"] or "",
                "id": e["item_id"],
                "topic": e["topic"],
            }
            for e in events[:6]
        ],
        "has_tracking": bool(tracked),
    }


def review_row(r: Any, store: Store | None = None) -> dict:
    """A review as a one-line question with two quick answers."""
    if r.kind == "topic":
        cat = (r.suggestion.get("category") or {}).get("value")
        return {
            "id": r.id,
            "from": sender_name(r.sender),
            "subject": r.subject,
            "topic": r.topic,
            "kind": "topic",
            "question": f"About {r.topic}?" + (f" Looks like {cat}." if cat else ""),
            "confidence": r.suggestion.get("p", 0.0),
            "answers": [("yes", f"Yes, {r.topic}"), ("no", "No")],
            "gmail_id": r.gmail_id,
        }
    match = r.suggestion.get("match", {})
    probs = match.get("probabilities", {})
    best = max(
        ((k, v) for k, v in probs.items() if k != "new"), key=lambda kv: kv[1], default=(None, 0.0)
    )
    answers = []
    if best[0] is not None:
        item = store.item(int(best[0])) if store is not None and best[0].isdigit() else None
        answers.append((best[0], fields_text(item.fields) if item else f"Item #{best[0]}"))
    answers.append(("new", "A new one"))
    return {
        "id": r.id,
        "from": sender_name(r.sender),
        "subject": r.subject,
        "topic": r.topic,
        "kind": "item",
        "question": f"Which {r.topic} item is this about?",
        "confidence": best[1],
        "answers": answers,
        "gmail_id": r.gmail_id,
    }


# -- needs you ------------------------------------------------------------------------------


def needs(store: Store, topics: jf.Topics, selected: int | None) -> dict:
    reviews = store.reviews()
    rows = [review_row(r, store) for r in reviews]
    chosen = next((r for r in reviews if r.id == selected), reviews[0] if reviews else None)
    return {
        "rows": rows,
        "selected": chosen.id if chosen else None,
        "detail": review_detail(store, topics, chosen) if chosen else None,
    }


def review_detail(store: Store, topics: jf.Topics, r: Any) -> dict:
    topic = topics.get(r.topic)
    th = {**DEFAULT_THRESHOLDS, **(topic.thresholds if topic else {})}
    email = store.email(r.gmail_id) or {}
    out: dict[str, Any] = {
        "id": r.id,
        "kind": r.kind,
        "topic": r.topic,
        "gmail_id": r.gmail_id,
        "subject": r.subject,
        "sender": r.sender,
        "when": clock(email.get("received_at")),
        "thread": gmail_link(email.get("thread_id")),
        "snippet": email.get("snippet", ""),
        "reasons": r.reasons,
        "accept": th["accept"],
        "reject": th["reject"],
    }
    if r.kind == "topic":
        s = r.suggestion
        out["p"] = s.get("p", 0.0)
        out["category"] = (s.get("category") or {}).get("value")
        out["fields"] = {
            k: v.get("value") for k, v in (s.get("fields") or {}).items() if v.get("value")
        }
        out["missing"] = [x.split(":", 1)[1] for x in r.reasons if x.startswith("field_missing:")]
        # Every field of the topic, editable: Jev's value (if any) prefilled,
        # the candidates it weighed offered as suggestions.
        found = s.get("fields") or {}
        out["edit"] = [
            {
                "name": name,
                "value": (found.get(name) or {}).get("value") or "",
                "missing": name in out["missing"],
                "suggest": [
                    c
                    for c, _ in sorted(
                        ((found.get(name) or {}).get("probabilities") or {}).items(),
                        key=lambda kv: -kv[1],
                    )
                    if c != "none of these"
                ][:8],
            }
            for name in (topic.fields if topic else {})
        ]
    else:
        match = r.suggestion.get("match", {})
        probs = match.get("probabilities", {})
        options = []
        for item_id in match.get("candidates", []):
            item = store.item(int(item_id))
            if item:
                options.append(
                    {
                        "value": str(item_id),
                        "label": fields_text(item.fields),
                        "status": item.status,
                        "p": probs.get(str(item_id), 0.0),
                    }
                )
        options.append(
            {
                "value": "new",
                "label": f"A new {r.topic} item",
                "status": "",
                "p": probs.get("new", 0.0),
            }
        )
        out["options"] = options
    return out


# -- everything ----------------------------------------------------------------------------


def everything(
    store: Store,
    topics: jf.Topics,
    *,
    kind: str,
    topic: str | None,
    search: str,
    selected: str | None,
    offset: int,
    page: int = 100,
) -> dict:
    rows = store.email_page(kind=kind, topic=topic, search=search, limit=page + 1, offset=offset)
    more = len(rows) > page
    rows = rows[:page]
    out_rows = []
    for e in rows:
        matched = [(t, o) for t, o in e["outcomes"].items() if o["outcome"] == "match"]
        if e["in_review"]:
            tag, tone = "Needs you", "warn"
        elif matched:
            t, o = matched[0]
            tag, tone = t + (f" · {o['category']}" if o["category"] else ""), "match"
        else:
            tag, tone = "not in a topic", "none"
        out_rows.append(
            {
                "gmail_id": e["gmail_id"],
                "from": sender_name(e["sender"]),
                "subject": e["subject"],
                "tag": tag,
                "tone": tone,
                "time": clock(e["received_at"]),
            }
        )
    chosen = selected or (out_rows[0]["gmail_id"] if out_rows else None)
    counts = {
        "all": store.email_count(),
        "matched": store.count_matched(),
        "review": len(store.reviews()),
    }
    return {
        "rows": out_rows,
        "more": more,
        "offset": offset,
        "page": page,
        "kind": kind,
        "topic": topic or "",
        "search": search,
        "counts": counts,
        "topic_names": list(topics),
        "selected": chosen,
        "detail": email_detail(store, topics, chosen) if chosen else None,
    }


def email_detail(store: Store, topics: jf.Topics, gmail_id: str) -> dict | None:
    email = store.email(gmail_id)
    if email is None:
        return None
    results = store.results_for(gmail_id)
    matched = [r for r in results if r["outcome"] == "match"]
    others = [r for r in results if r["outcome"] != "match"]
    verdicts = []
    for r in matched:
        topic = topics.get(r["topic"])
        labels = []
        if topic is not None:
            base = label_for(topic)
            if base:
                cat = (r.get("category") or {}).get("value")
                labels = [base] + ([f"{base}/{cat}"] if cat else [])
        verdicts.append(
            {
                "topic": r["topic"],
                "p": r["p"],
                "category": (r.get("category") or {}).get("value"),
                "fields": {
                    k: v.get("value") for k, v in (r.get("fields") or {}).items() if v.get("value")
                },
                "flags": {k: p for k, p in (r.get("flags") or {}).items()},
                "labels": labels,
            }
        )
    return {
        "gmail_id": gmail_id,
        "subject": email["subject"],
        "sender": email["sender"],
        "from": sender_name(email["sender"]),
        "when": clock(email["received_at"]),
        "snippet": email["snippet"],
        "thread": gmail_link(email["thread_id"]),
        "verdicts": verdicts,
        "others": [{"topic": r["topic"], "outcome": r["outcome"], "p": r["p"]} for r in others],
        "corrected": [r["topic"] for r in others if "user_corrected" in (r.get("reasons") or [])],
    }


# -- items -----------------------------------------------------------------------------------


def items(
    store: Store,
    topics: jf.Topics,
    *,
    topic: str | None,
    view: str,
    selected: int | None,
    closed: bool,
    now: datetime | None = None,
) -> dict:
    tracked = [t for t in topics.values() if t.track]
    if not tracked:
        return {"tracked": [], "topic": None}
    t = next((x for x in tracked if x.name == topic), tracked[0])
    assert t.track is not None
    all_items = store.items(t.name)
    pipeline = list(t.track.statuses)
    terminal = list(t.track.terminal)

    def card(i: Item) -> dict:
        return {
            "id": i.id,
            "title": next(iter(v for v in i.fields.values() if v), "(no details)"),
            "sub": " · ".join(str(v) for v in list(i.fields.values())[1:] if v),
            "status": i.status,
            "stale": i.stale,
            "when": clock(i.last_email_at, now) or "—",
            "days": days_since(i.last_email_at, now),
        }

    columns = [
        {"name": s, "cards": [card(i) for i in all_items if i.status == s]} for s in pipeline
    ]
    closed_cols = [
        {"name": s, "cards": [card(i) for i in all_items if i.status == s]} for s in terminal
    ]
    unsorted = [card(i) for i in all_items if i.status not in pipeline + terminal]
    if unsorted:
        columns.insert(0, {"name": "no status", "cards": unsorted})
    chosen = next((i for i in all_items if i.id == selected), None)
    return {
        "tracked": [x.name for x in tracked],
        "topic": t.name,
        "view": view,
        "columns": columns,
        "closed_cols": closed_cols,
        "show_closed": closed,
        "closed_count": sum(len(c["cards"]) for c in closed_cols),
        "count": len(all_items),
        "quiet": sum(i.stale for i in all_items),
        "statuses": pipeline + terminal,
        "fields": list(t.fields),
        "rows": sorted((card(i) for i in all_items), key=lambda c: c["days"]),
        "selected": item_detail(store, t, chosen, now) if chosen else None,
    }


def item_detail(store: Store, topic: jf.Topic, item: Item, now: datetime | None = None) -> dict:
    assert topic.track is not None
    pipeline = list(topic.track.statuses)
    reached = pipeline.index(item.last_stage) if item.last_stage in pipeline else -1
    if item.status in pipeline:
        reached = pipeline.index(item.status)
    steps = [
        {"name": s, "reached": i <= reached, "current": s == item.status}
        for i, s in enumerate(pipeline)
    ]
    return {
        "id": item.id,
        "topic": topic.name,
        "title": fields_text(item.fields),
        "fields": item.fields,
        "status": item.status,
        "stale": item.stale,
        "closed": item.status in topic.track.terminal,
        "notes": item.notes,
        "steps": steps,
        "statuses": pipeline + list(topic.track.terminal),
        "emails": [
            {
                "date": clock(e["received_at"], now),
                "gmail_id": e["gmail_id"],
                "thread": gmail_link(e["thread_id"]),
                "subject": e["subject"],
                "category": e["category"] or "",
            }
            for e in store.item_emails(item.id)
        ],
        "events": [
            {"date": clock(e["at"], now), "status": e["status"], "kind": e["kind"]}
            for e in store.item_events(item.id)
        ],
        "source": item.source,
    }
