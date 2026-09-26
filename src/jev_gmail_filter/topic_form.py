"""The topic editor's model: a jevfilter topic as plain form fields and back.

The UI edits a `TopicForm`; `to_dict` turns it into the jevfilter topic
format and `build` validates it. Anything the form doesn't cover (scores,
composites, `when`, custom facets, other `meta` keys) is kept as-is, so a
round trip through the form never loses part of a hand-written topic.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import jevfilter as jf

DEFAULT_THRESHOLDS = {"accept": 0.7, "reject": 0.3, "min_confidence": 0.5}
FIELD_KINDS = ["org", "title", "email", "other"]
FORM_KEYS = {
    "name",
    "description",
    "exclude",
    "examples",
    "categories",
    "fields",
    "flags",
    "track",
    "thresholds",
    "meta",
}


@dataclass
class CategoryRow:
    name: str
    description: str = ""
    examples: list[str] = field(default_factory=list)
    exclude: str = ""


@dataclass
class FieldRow:
    name: str
    kind: str = "other"
    about: str = ""
    required: bool = False


@dataclass
class FlagRow:
    name: str
    statement: str = ""


@dataclass
class StatusRow:
    name: str
    categories: list[str] = field(default_factory=list)
    closed: bool = False  # a terminal status (e.g. rejected), reachable from anywhere


@dataclass
class TrackForm:
    match_on: list[str] = field(default_factory=list)
    statuses: list[StatusRow] = field(default_factory=list)
    stale_after_days: int | None = None


@dataclass
class TopicForm:
    name: str = ""
    description: str = ""
    exclude: list[str] = field(default_factory=list)
    examples_match: list[str] = field(default_factory=list)
    examples_no: list[str] = field(default_factory=list)
    categories: list[CategoryRow] = field(default_factory=list)
    fields: list[FieldRow] = field(default_factory=list)
    flags: list[FlagRow] = field(default_factory=list)
    track: TrackForm | None = None
    gmail_label: str | None = None  # None: use the topic name; "" : no label
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    extra: dict[str, Any] = field(default_factory=dict)  # keys the form doesn't edit
    extra_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def advanced_keys(self) -> list[str]:
        """Parts of the topic only editable as YAML."""
        return sorted(self.extra)


# -- topic → form -----------------------------------------------------------------


def from_dict(data: dict[str, Any]) -> TopicForm:
    data = copy.deepcopy(data)
    form = TopicForm(
        name=str(data.get("name", "")),
        description=_text(data.get("description"))
        if isinstance(data.get("description"), str | None)
        else "",
        exclude=_lines(data.get("exclude")),
        examples_match=list((data.get("examples") or {}).get("match", [])),
        examples_no=list((data.get("examples") or {}).get("no", [])),
    )
    for name, value in (data.get("categories") or {}).items():
        if isinstance(value, dict):
            form.categories.append(
                CategoryRow(
                    name,
                    _text(value.get("description")),
                    list(value.get("examples", [])),
                    "; ".join(_lines(value.get("exclude"))),
                )
            )
            if "children" in value:  # nested categories: YAML only
                form.extra.setdefault("categories", data["categories"])
        else:
            form.categories.append(CategoryRow(name, _text(value)))
    for name, value in (data.get("fields") or {}).items():
        value = value or {}
        kind = value.get("kind") or "other"
        form.fields.append(
            FieldRow(name, kind, _text(value.get("about")), bool(value.get("required")))
        )
    for name, statement in (data.get("flags") or {}).items():
        form.flags.append(FlagRow(name, _text(statement)))
    if data.get("track"):
        t = data["track"]
        statuses = [StatusRow(s, _as_list(c)) for s, c in (t.get("statuses") or {}).items()]
        statuses += [StatusRow(s, _as_list(c), True) for s, c in (t.get("terminal") or {}).items()]
        form.track = TrackForm(list(t.get("match_on", [])), statuses, t.get("stale_after_days"))
    form.thresholds = {**DEFAULT_THRESHOLDS, **(data.get("thresholds") or {})}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if "gmail_label" in meta:
        label = meta.pop("gmail_label")
        form.gmail_label = "" if label is False else str(label)
    form.extra_meta = meta
    if data.get("meta") is not None and not isinstance(data.get("meta"), dict):
        form.extra["meta"] = data["meta"]
    for key, value in data.items():
        if key not in FORM_KEYS:
            form.extra[key] = value
    if data.get("description") is not None and not isinstance(data["description"], str):
        form.extra["description"] = data["description"]  # structured: YAML only
    return form


def from_topic(topic: jf.Topic) -> TopicForm:
    return from_dict(topic.to_dict())


# -- form → topic -------------------------------------------------------------------


def to_dict(form: TopicForm) -> dict[str, Any]:
    """The jevfilter topic format. Empty sections are left out."""
    # Text is kept exactly as typed (not stripped): re-saving an unchanged topic must not
    # change its version, or every save would ask for a rescan.
    description = form.description if form.description.strip() else ""
    data: dict[str, Any] = {"name": form.name.strip(), "description": description}
    exclude = _clean(form.exclude)
    if exclude:
        data["exclude"] = exclude[0] if len(exclude) == 1 else exclude
    examples = {
        k: v
        for k, v in (("match", _clean(form.examples_match)), ("no", _clean(form.examples_no)))
        if v
    }
    if examples:
        data["examples"] = examples

    if "categories" in form.extra:
        data["categories"] = form.extra["categories"]  # nested: edited as YAML
    else:
        cats: dict[str, Any] = {}
        for row in form.categories:
            if not row.name.strip():
                continue
            examples_ = _clean(row.examples)
            excl = _clean(row.exclude.split(";"))
            if examples_ or excl:
                entry: dict[str, Any] = {"description": row.description}
                if examples_:
                    entry["examples"] = examples_
                if excl:
                    entry["exclude"] = excl[0] if len(excl) == 1 else excl
                cats[row.name.strip()] = entry
            else:
                cats[row.name.strip()] = row.description
        if cats:
            data["categories"] = cats

    fields: dict[str, Any] = {}
    for row in form.fields:
        if not row.name.strip():
            continue
        entry = {}
        if row.kind and row.kind != "other":
            entry["kind"] = row.kind
        if row.about.strip():
            entry["about"] = row.about
        if row.required:
            entry["required"] = True
        fields[row.name.strip()] = entry
    if fields:
        data["fields"] = fields

    flags = {r.name.strip(): r.statement for r in form.flags if r.name.strip()}
    if flags:
        data["flags"] = flags

    if form.track is not None:
        track: dict[str, Any] = {}
        match_on = [f for f in form.track.match_on if f in fields]
        if match_on:
            track["match_on"] = match_on
        pipeline = {
            r.name.strip(): _clean(r.categories)
            for r in form.track.statuses
            if r.name.strip() and not r.closed
        }
        closed = {
            r.name.strip(): _clean(r.categories)
            for r in form.track.statuses
            if r.name.strip() and r.closed
        }
        if pipeline:
            track["statuses"] = pipeline
        if closed:
            track["terminal"] = closed
        if form.track.stale_after_days:
            track["stale_after_days"] = int(form.track.stale_after_days)
        data["track"] = track

    changed = {
        k: round(float(v), 2)
        for k, v in form.thresholds.items()
        if abs(float(v) - DEFAULT_THRESHOLDS[k]) > 1e-9
    }
    if changed:
        data["thresholds"] = changed

    for key, value in form.extra.items():
        if key not in ("categories", "meta"):
            data[key] = value
    meta = dict(form.extra_meta)
    if form.gmail_label is not None:
        meta["gmail_label"] = form.gmail_label.strip() or False
    if "meta" in form.extra:
        data["meta"] = form.extra["meta"]
    elif meta:
        data["meta"] = meta
    return data


def build(form: TopicForm) -> tuple[jf.Topic | None, list[str]]:
    """Validate: the topic, or plain-language problems."""
    problems = []
    if form.track is not None:
        names = {c.name.strip() for c in form.categories if c.name.strip()}
        for row in form.track.statuses:
            unknown = [c for c in _clean(row.categories) if c not in names]
            if row.name.strip() and unknown and "categories" not in form.extra:
                problems.append(
                    f"Status '{row.name}' is moved by {', '.join(unknown)}, which "
                    f"{'is not a category' if len(unknown) == 1 else 'are not categories'}."
                )
        if not form.track.statuses:
            problems.append("Tracking needs at least one status (e.g. applied ← applied).")
    if problems:
        return None, problems
    try:
        return jf.Topic.from_dict(to_dict(form)), []
    except jf.TopicError as e:
        return None, [_friendly(p, form.name) for p in e.problems]


def _friendly(problem: str, name: str) -> str:
    text = problem.removeprefix(f"Topic {name!r}: ").removeprefix("Topic '?': ")
    replacements = {
        "`description` is required: say in plain English what belongs": (
            "Describe what belongs in this topic."
        ),
        "`name` is required and must be text": "Give the topic a name.",
        "`track` needs `categories` to move items between statuses": (
            "Tracking needs categories: statuses move when an email's category arrives."
        ),
    }
    return replacements.get(text, text.replace("`", ""))


# -- starters --------------------------------------------------------------------------

STARTERS: dict[str, dict[str, Any]] = {
    "Travel": {
        "name": "Travel",
        "description": "Flight, hotel, train and car bookings for my trips, and changes to them.",
        "exclude": "Travel deals, newsletters and loyalty-program marketing.",
        "categories": {
            "booking": "Confirms a booking or reservation.",
            "change": "A change, delay or cancellation.",
            "check_in": "Check-in open, boarding pass or itinerary reminder.",
        },
        "fields": {"company": {"kind": "org", "about": "The airline, hotel or travel company."}},
    },
    "Bills": {
        "name": "Bills",
        "description": "Bills, invoices and statements I need to pay or keep.",
        "exclude": "Marketing, offers and receipts for things already paid.",
        "categories": {
            "due": "A bill or invoice with an amount due.",
            "statement": "A statement or summary, nothing due now.",
            "overdue": "A reminder that a payment is late.",
        },
        "fields": {"company": {"kind": "org", "about": "Who the bill is from."}},
        "flags": {"action_needed": "I need to pay or respond by a date."},
    },
    "School": {
        "name": "School",
        "description": "Messages from my kids' school or teachers about classes, events or forms.",
        "exclude": "Fundraising marketing and general newsletters.",
        "flags": {"needs_reply": "The school asks me to reply, sign or send something."},
    },
    "Orders": {
        "name": "Orders",
        "description": "Online orders I placed: confirmations, shipping and delivery updates.",
        "exclude": "Marketing, sales and abandoned-cart reminders.",
        "categories": {
            "ordered": "Confirms an order or payment.",
            "shipped": "Shipped or out for delivery.",
            "delivered": "Delivered.",
            "problem": "A delay, cancellation, return or refund.",
        },
        "fields": {"merchant": {"kind": "org", "about": "The shop I bought from."}},
        "track": {
            "match_on": ["merchant"],
            "statuses": {
                "ordered": ["ordered"],
                "shipped": ["shipped"],
                "delivered": ["delivered"],
            },
            "stale_after_days": 14,
        },
    },
}


# -- helpers ------------------------------------------------------------------------------


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _lines(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _as_list(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value or [])


def _clean(values: list[str]) -> list[str]:
    return [v.strip() for v in values if v and v.strip()]
