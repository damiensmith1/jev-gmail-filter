"""The Topics page: topic cards, and a form-based editor with "Try it".

The form maps to jevfilter's topic format through `topic_form`; raw YAML is
still there under Advanced. Nothing here writes a topic until Save, and a
topic is only saved if jevfilter accepts it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jevfilter as jf
import pandas as pd
import streamlit as st
import yaml

from . import candidates, onboarding
from . import topic_form as tf
from .config import Settings
from .db import Store
from .gmail import MailSource


@dataclass
class Ctx:
    """What the Topics page needs from the app."""

    settings: Callable[[], Settings]
    store: Callable[[], Store]
    source: Callable[[], MailSource]
    start_scan: Callable[..., None]
    scanning: Callable[[], bool]
    meta: Callable[[str, str], str]
    when: Callable[[str | None], str]


# -- list ---------------------------------------------------------------------------


def render(ctx: Ctx) -> None:
    edit = st.session_state.get("topic_edit")
    if edit is not None:
        editor(ctx, edit)
        return
    st.title("Topics")
    st.caption("A topic is a plain-English description of emails you care about.")
    rescan_offer(ctx)
    new_topic_picker(ctx)
    files = onboarding.topic_files(ctx.settings())
    if not files:
        st.info("No topics yet. Create one above.")
    with ctx.store() as s:
        for name, path in files.items():
            topic = jf.Topic.load(path)[name]
            stats = s.topic_stats(name)
            card(ctx, topic, path, stats)
    with st.expander("Rescan older mail"):
        st.caption(
            "Judge older email against topics it hasn't been judged against yet "
            "(new or edited topics). Already-current email is skipped."
        )
        days = st.number_input("Days back", 1, 365, 14, key="topics-rescan-days")
        if st.button("Rescan", disabled=ctx.scanning(), key="topics-rescan"):
            ctx.start_scan("Rescan", since=datetime.now(UTC) - timedelta(days=days))
            st.rerun()


def card(ctx: Ctx, topic: jf.Topic, path: Path, stats: dict[str, Any]) -> None:
    with st.container(border=True):
        left, right = st.columns([5, 2])
        left.markdown(f"### {topic.name}")
        desc = " ".join(str(topic.description).split())
        left.write(desc if len(desc) < 220 else desc[:220] + "…")
        badges = []
        if topic.categories:
            badges.append(f"{len(topic.categories)} categories")
        if topic.fields:
            badges.append("pulls out " + ", ".join(topic.fields))
        if topic.track:
            badges.append("tracked as items")
        label = (
            (topic.meta or {}).get("gmail_label", topic.name)
            if isinstance(topic.meta, dict)
            else topic.name
        )
        badges.append(f"Gmail label: {label}" if label else "no Gmail label")
        left.caption(" · ".join(badges))
        right.metric("Matched", stats["matches"])
        last = ctx.when(stats["last_match"]) if stats["last_match"] else "never"
        right.caption(
            f"Last match: {last}" + (f" · {stats['reviews']} to review" if stats["reviews"] else "")
        )
        edit_col, delete_col, _ = st.columns([1, 1, 5])
        if edit_col.button("Edit", key=f"edit-{topic.name}", type="primary"):
            start_editing(tf.from_topic(topic), path, topic.version)
        with delete_col.popover("Delete"):
            st.write(
                f"Delete **{topic.name}**? Past results stay in the database; "
                "no new emails will be judged against it."
            )
            if st.button("Delete topic", key=f"del-{topic.name}"):
                path.unlink()
                st.toast(f"Deleted {topic.name}")
                st.rerun()


def new_topic_picker(ctx: Ctx) -> None:
    with st.expander("➕ New topic"):
        examples = {f"Example: {e.name}": e.path for e in onboarding.examples()}
        options = ["Blank", *[f"Starter: {n}" for n in tf.STARTERS], *examples]
        choice = st.selectbox("Start from", options, key="new-topic-start")
        if st.button("Start", type="primary", key="new-topic-go"):
            if choice == "Blank":
                form = tf.TopicForm()
            elif choice.startswith("Starter: "):
                form = tf.from_dict(tf.STARTERS[choice.removeprefix("Starter: ")])
            else:
                form = tf.from_topic(next(iter(jf.Topic.load(examples[choice]).values())))
            start_editing(form, None, None)


def start_editing(form: tf.TopicForm, path: Path | None, version: str | None) -> None:
    n = st.session_state.get("topic_edit_n", 0) + 1
    st.session_state.topic_edit_n = n
    st.session_state.topic_edit = {"form": form, "path": path, "version": version, "n": n}
    st.rerun()


def rescan_offer(ctx: Ctx) -> None:
    changed = st.session_state.get("topic_changed")
    if not changed:
        return
    with st.container(border=True):
        st.success(f"Saved **{changed}**. New email is judged with it from now on.")
        st.write(
            "Emails already judged were judged against the old version. Rescan to "
            "judge them again with your changes (already-current ones are skipped)."
        )
        cols = st.columns([1, 1, 3])
        default = int(float(ctx.meta("backscan_days", "14")))
        days = cols[0].number_input("Days back", 1, 365, default, key="topic-rescan-days")
        if cols[1].button("Rescan", type="primary", disabled=ctx.scanning()):
            st.session_state.pop("topic_changed")
            ctx.start_scan("Rescan", since=datetime.now(UTC) - timedelta(days=days))
            st.rerun()
        if cols[2].button("Not now"):
            st.session_state.pop("topic_changed")
            st.rerun()


# -- editor ---------------------------------------------------------------------------


def editor(ctx: Ctx, edit: dict[str, Any]) -> None:
    form: tf.TopicForm = edit["form"]
    k = f"te{edit['n']}-"
    title = f"Edit {form.name}" if edit["path"] else "New topic"
    top_l, top_r = st.columns([6, 1])
    top_l.title(title)
    if top_r.button("← Back", key=k + "back"):
        st.session_state.pop("topic_edit")
        st.rerun()

    # Advanced YAML applied on the last run replaces the form's starting point.
    if (applied := st.session_state.pop(k + "yaml-applied", None)) is not None:
        form = edit["form"] = applied

    new = basics(form, k)
    new.categories = categories_table(form, k)
    new.fields = fields_table(form, k)
    new.flags = flags_table(form, k)
    new.track = tracking(form, new, k)
    new.gmail_label = gmail_label(form, new, k)
    new.thresholds, new.extra, new.extra_meta = advanced(form, new, k)

    topic, problems = tf.build(new)
    try_it(ctx, new, topic, problems, k)

    st.divider()
    if problems:
        st.error("Fix these before saving:\n" + "\n".join(f"- {p}" for p in problems))
    save_col, cancel_col, _ = st.columns([1, 1, 5])
    if save_col.button("Save", type="primary", disabled=bool(problems), key=k + "save"):
        save(ctx, edit, topic)
    if cancel_col.button("Cancel", key=k + "cancel"):
        st.session_state.pop("topic_edit")
        st.rerun()


def basics(form: tf.TopicForm, k: str) -> tf.TopicForm:
    new = tf.TopicForm()
    new.name = st.text_input(
        "Name",
        form.name,
        key=k + "name",
        placeholder="Travel",
        help="Shown in the app and used for the Gmail label unless you set one below.",
    )
    new.description = st.text_area(
        "What belongs",
        form.description,
        key=k + "desc",
        height=90,
        placeholder="Flight, hotel and train bookings for my trips, and changes to them.",
        help="Plain English. Be specific about what counts; Jev reads this literally.",
    )
    new.exclude = _lines(
        st.text_area(
            "Not this (one per line)",
            "\n".join(form.exclude),
            key=k + "excl",
            height=70,
            placeholder="Travel deals and newsletters",
            help="Things that look similar but shouldn't match.",
        )
    )
    left, right = st.columns(2)
    new.examples_match = _lines(
        left.text_area(
            "Examples that belong (optional, one per line)",
            "\n".join(form.examples_match),
            key=k + "exm",
            height=70,
            placeholder="Your booking is confirmed",
        )
    )
    new.examples_no = _lines(
        right.text_area(
            "Examples that don't (optional, one per line)",
            "\n".join(form.examples_no),
            key=k + "exn",
            height=70,
            placeholder="Summer sale: 30% off flights",
        )
    )
    return new


def categories_table(form: tf.TopicForm, k: str) -> list[tf.CategoryRow]:
    with st.expander(f"Categories ({len(form.categories)})", expanded=bool(form.categories)):
        st.caption(
            "Optional. Each matching email gets exactly one category. Add examples or "
            '"not this" to separate categories that get confused.'
        )
        if "categories" in form.extra:
            st.info("This topic has nested categories; edit them under Advanced → YAML.")
            return form.categories
        rows = [
            {
                "name": c.name,
                "description": c.description,
                "examples": "; ".join(c.examples),
                "not this": c.exclude,
            }
            for c in form.categories
        ]
        df = st.data_editor(
            pd.DataFrame(rows, columns=["name", "description", "examples", "not this"]),
            key=k + "cats",
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_config={
                "name": st.column_config.TextColumn("Name", help="Short, e.g. applied"),
                "description": st.column_config.TextColumn("What it means", width="large"),
                "examples": st.column_config.TextColumn("Examples (; between)"),
                "not this": st.column_config.TextColumn("Not this (; between)"),
            },
        )
        return [
            tf.CategoryRow(r["name"], r["description"], _split(r["examples"]), r["not this"])
            for r in _records(df)
            if r["name"]
        ]


def fields_table(form: tf.TopicForm, k: str) -> list[tf.FieldRow]:
    with st.expander(f"Details to pull out ({len(form.fields)})", expanded=bool(form.fields)):
        st.caption(
            'Optional. Values picked from the email, never made up. "org" finds '
            'company and shop names, "title" job titles, "email" addresses.'
        )
        rows = [
            {"name": f.name, "kind": f.kind, "about": f.about, "required": f.required}
            for f in form.fields
        ]
        df = st.data_editor(
            pd.DataFrame(rows, columns=["name", "kind", "about", "required"]),
            key=k + "fields",
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_config={
                "name": st.column_config.TextColumn("Name", help="e.g. company"),
                "kind": st.column_config.SelectboxColumn("Kind", options=tf.FIELD_KINDS),
                "about": st.column_config.TextColumn("What it is", width="large"),
                "required": st.column_config.CheckboxColumn(
                    "Required", help="If it can't be found, the email goes to Review."
                ),
            },
        )
        return [
            tf.FieldRow(r["name"], r["kind"] or "other", r["about"], bool(r["required"]))
            for r in _records(df)
            if r["name"]
        ]


def flags_table(form: tf.TopicForm, k: str) -> list[tf.FlagRow]:
    with st.expander(f"Yes/no flags ({len(form.flags)})", expanded=bool(form.flags)):
        st.caption("Optional. Extra yes/no questions for matching emails, e.g. needs_reply.")
        rows = [{"name": f.name, "statement": f.statement} for f in form.flags]
        df = st.data_editor(
            pd.DataFrame(rows, columns=["name", "statement"]),
            key=k + "flags",
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_config={
                "name": st.column_config.TextColumn("Name"),
                "statement": st.column_config.TextColumn(
                    "True when…", width="large", help="e.g. The sender asks me to reply."
                ),
            },
        )
        return [tf.FlagRow(r["name"], r["statement"]) for r in _records(df) if r["name"]]


def tracking(form: tf.TopicForm, new: tf.TopicForm, k: str) -> tf.TrackForm | None:
    with st.expander("Track as items", expanded=form.track is not None):
        st.caption(
            "Group matching emails into items (a job, an order) that move through "
            "statuses as emails arrive, and flag items that go quiet."
        )
        on = st.toggle("Track these as items", value=form.track is not None, key=k + "track")
        if not on:
            return None
        track = form.track or tf.TrackForm()
        names = [f.name for f in new.fields]
        match_on = st.multiselect(
            "Same item when these match",
            names,
            default=[m for m in track.match_on if m in names],
            key=k + "matchon",
            help="Emails with a different value here are never the same item.",
        )
        stale = st.number_input(
            "Gone quiet after (days, 0 = never)",
            0,
            365,
            int(track.stale_after_days or 0),
            key=k + "stale",
        )
        cats = [c.name for c in new.categories]
        st.caption(
            "Statuses in order. Each moves when an email with one of its categories "
            "arrives (" + (", ".join(cats) or "add categories first") + "). "
            "Closed statuses, like rejected, apply from any stage."
        )
        rows = [
            {"status": s.name, "moved by": ", ".join(s.categories), "closed": s.closed}
            for s in track.statuses
        ]
        df = st.data_editor(
            pd.DataFrame(rows, columns=["status", "moved by", "closed"]),
            key=k + "statuses",
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_config={
                "status": st.column_config.TextColumn("Status"),
                "moved by": st.column_config.TextColumn("Moved by categories (, between)"),
                "closed": st.column_config.CheckboxColumn("Closed"),
            },
        )
        statuses = [
            tf.StatusRow(r["status"], _split(r["moved by"], ","), bool(r["closed"]))
            for r in _records(df)
            if r["status"]
        ]
        return tf.TrackForm(match_on, statuses, int(stale) or None)


def gmail_label(form: tf.TopicForm, new: tf.TopicForm, k: str) -> str | None:
    with st.expander("Gmail label"):
        on = st.checkbox(
            "Label matching emails in Gmail", value=form.gmail_label != "", key=k + "labelon"
        )
        if not on:
            return ""
        default = form.gmail_label or new.name
        label = st.text_input(
            "Label",
            default,
            key=k + "label",
            help="Categories become sub-labels, e.g. Jobs/applied.",
        )
        return None if label.strip() == new.name.strip() and form.gmail_label is None else label


def advanced(
    form: tf.TopicForm, new: tf.TopicForm, k: str
) -> tuple[dict[str, float], dict[str, Any], dict[str, Any]]:
    with st.expander("Advanced"):
        st.caption('How sure Jev must be. Between "no" and "yes", emails go to Review.')
        c1, c2, c3 = st.columns(3)
        th = form.thresholds
        accept = c1.slider("Yes at or above", 0.0, 1.0, float(th["accept"]), 0.05, key=k + "acc")
        reject = c2.slider("No below", 0.0, 1.0, float(th["reject"]), 0.05, key=k + "rej")
        conf = c3.slider(
            "Category / detail confidence",
            0.0,
            1.0,
            float(th["min_confidence"]),
            0.05,
            key=k + "conf",
        )
        if form.advanced_keys:
            st.info(
                "This topic also uses "
                + ", ".join(form.advanced_keys)
                + ", which you can edit in YAML below."
            )
        preview = tf.TopicForm(
            **{
                **new.__dict__,
                "thresholds": {"accept": accept, "reject": reject, "min_confidence": conf},
                "extra": form.extra,
                "extra_meta": form.extra_meta,
            }
        )
        text = st.text_area(
            "YAML",
            yaml.safe_dump(tf.to_dict(preview), sort_keys=False, allow_unicode=True),
            height=260,
            key=k + "yaml",
        )
        if st.button("Apply YAML to the form", key=k + "yaml-apply"):
            try:
                data = yaml.safe_load(text)
                jf.Topic.from_dict(data)
            except (yaml.YAMLError, jf.TopicError, TypeError) as e:
                st.error(f"Not applied: {e}")
            else:
                st.session_state[k + "yaml-applied"] = tf.from_dict(data)
                _reset_widgets(k)
                st.rerun()
    return (
        {"accept": accept, "reject": reject, "min_confidence": conf},
        form.extra,
        form.extra_meta,
    )


def try_it(
    ctx: Ctx, form: tf.TopicForm, topic: jf.Topic | None, problems: list[str], k: str
) -> None:
    with st.expander("🧪 Try it before saving", expanded=True):
        st.caption(
            "Run this version of the topic on one email. Nothing is saved or labelled; "
            "costs a fraction of a cent."
        )
        mode = st.radio(
            "Email",
            ["A recent email", "Paste text"],
            horizontal=True,
            key=k + "trymode",
            label_visibility="collapsed",
        )
        email = None
        text = ""
        if mode == "A recent email":
            with ctx.store() as s:
                recent = s.recent_emails(50)
            if not recent:
                st.caption("No emails judged yet; paste one instead.")
            else:
                labels = {
                    r["gmail_id"]: f"{r['subject'] or '(no subject)'} — {r['sender']}"
                    for r in recent
                }
                email = st.selectbox(
                    "Pick an email", list(labels), format_func=labels.get, key=k + "trypick"
                )
        else:
            text = st.text_area("Email text (subject and body)", key=k + "trytext", height=120)
        if st.button("Test", key=k + "trygo", disabled=bool(problems) or not (email or text)):
            assert topic is not None
            try:
                result = run_try(ctx, topic, email, text)
            except (jf.JudgeError, jf.BudgetExceeded) as e:
                st.error(f"Jev: {e}")
                return
            show_try(result)


def run_try(ctx: Ctx, topic: jf.Topic, gmail_id: str | None, text: str) -> jf.Result:
    if gmail_id:
        mail = ctx.source().get(gmail_id)
        state = mail.state()
        with ctx.store() as s:
            known = {
                name: [i.fields.get(name) for i in s.items(topic.name)] for name in topic.fields
            }
        cands = {
            topic.name: {
                name: candidates.extract(mail, spec.kind, known[name])
                for name, spec in topic.fields.items()
            }
        }
    else:
        from .mail import Email

        subject, _, body = text.partition("\n")
        mail = Email("try", "try", "", subject.strip(), datetime.now(UTC), body.strip())
        state = mail.state()
        cands = {
            topic.name: {
                name: candidates.extract(mail, spec.kind) for name, spec in topic.fields.items()
            }
        }
    f = jf.Filter([topic], budget=jf.Budget(usd=0.01))
    return f.judge(jf.Content(state, candidates=cands))


def show_try(result: jf.Result) -> None:
    t = next(iter(result))
    verdict = {
        "match": "✅ Belongs",
        "review": "❓ Unsure (would go to Review)",
        "no": "❌ Doesn't belong",
    }[t.outcome]
    st.markdown(f"**{verdict}** · confidence {t.p:.0%}")
    if t.category is not None:
        st.write(f"Category: **{t.category.value}** ({t.category.confidence:.0%})")
    for name, fv in t.fields.items():
        st.write(f"{name}: **{fv.value or '— not found'}**")
    for name, p in t.flags.items():
        st.write(f"{name}: {'yes' if p >= 0.5 else 'no'} ({p:.0%})")
    if t.reasons and t.outcome != "match":
        st.caption("Why: " + ", ".join(t.reasons))
    st.caption(f"Cost ${result.cost_usd or 0:.5f}")


def save(ctx: Ctx, edit: dict[str, Any], topic: jf.Topic | None) -> None:
    assert topic is not None
    conf = ctx.settings()
    existing = onboarding.topic_files(conf)
    path: Path | None = edit["path"]
    clash = existing.get(topic.name)
    if clash is not None and clash != path:
        st.error(f"There's already a topic called {topic.name!r}.")
        return
    if path is None:
        path = conf.topics_dir / onboarding.topic_filename(topic.name)
        if path.exists():
            st.error(f"A file named {path.name} already exists; pick another name.")
            return
    path.write_text(topic.to_yaml())
    st.session_state.pop("topic_edit")
    if edit["version"] is not None and edit["version"] != topic.version:
        st.session_state.topic_changed = topic.name
    elif edit["version"] is None:
        st.session_state.topic_changed = topic.name
    st.toast(f"Saved {topic.name}")
    st.rerun()


# -- helpers -------------------------------------------------------------------------


def _records(df: Any) -> list[dict[str, Any]]:
    frame = df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
    frame = frame.astype(object).where(frame.notna(), None)
    out = []
    for row in frame.to_dict("records"):
        out.append(
            {
                key: ("" if v is None else v.strip() if isinstance(v, str) else v)
                for key, v in row.items()
            }
        )
    return out


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _split(text: str, sep: str = ";") -> list[str]:
    return [p.strip() for p in (text or "").split(sep) if p.strip()]


def _reset_widgets(k: str) -> None:
    for key in [key for key in st.session_state if str(key).startswith(k)]:
        if not key.endswith("yaml-applied"):
            del st.session_state[key]
