"""The web UI: `jev-gmail-filter ui` (Streamlit, local only).

First run shows the setup wizard (the same steps as `init`); after that,
pages for the overview, the review queue, tracked items, recent emails,
topics and settings. Everything runs on this machine against the local
data folder; Gmail access goes through `onboarding.connect`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jevfilter as jf
import streamlit as st
import yaml

from jev_gmail_filter import onboarding
from jev_gmail_filter.config import load_settings
from jev_gmail_filter.db import Store
from jev_gmail_filter.gmail import (
    CATEGORIES,
    MailSource,
    SetupError,
    inbox_query,
    parse_categories,
)
from jev_gmail_filter.pipeline import Pipeline, SyncReport

PAGES = ["Overview", "Review", "Items", "Emails", "Topics", "Settings"]


# -- plumbing -----------------------------------------------------------------------


def settings():
    s = load_settings()
    s.ensure()
    return s


def store() -> Store:
    return Store(settings().db_path)


def source() -> MailSource:
    if "source" not in st.session_state:
        st.session_state.source = onboarding.connect(settings())
    return st.session_state.source


def topics() -> jf.Topics:
    return jf.Topic.load(settings().topics_dir)


def meta(key: str, default: str) -> str:
    with store() as s:
        return s.get_meta(key, default) or default


def set_meta(key: str, value: str) -> None:
    with store() as s:
        s.set_meta(key, value)


def pipeline(*, budget: float | None = None, labels: bool | None = None) -> Pipeline:
    cap = budget if budget is not None else float(meta("max_usd", "1.0"))
    write = (meta("labels", "off") == "on") if labels is None else labels
    return Pipeline(store(), source(), topics(), budget=jf.Budget(usd=cap), write_labels=write)


# -- background scans --------------------------------------------------------------
#
# A scan runs on its own thread, so refreshing the page or switching pages
# doesn't stop it. Pages poll it for progress. One scan at a time.


@dataclass
class ScanJob:
    kind: str  # "First scan", "Sync", "Rescan", "Re-check"
    labels_on: bool
    n: int = 0
    total: int = 0
    report: SyncReport | None = None
    error: str | None = None
    finished: bool = False
    dismissed: bool = False

    def progress(self, n: int, total: int) -> None:
        self.n, self.total = n, total


@st.cache_resource
def _jobs() -> dict[str, ScanJob | None]:
    return {"current": None}


def current_job() -> ScanJob | None:
    return _jobs()["current"]


def scanning() -> bool:
    job = current_job()
    return job is not None and not job.finished


def start_scan(
    kind: str,
    *,
    since: datetime | None = None,
    budget: float | None = None,
    labels: bool | None = None,
) -> None:
    if scanning():
        return
    cap = budget if budget is not None else float(meta("max_usd", "1.0"))
    write = (meta("labels", "off") == "on") if labels is None else labels
    conf, topic_set = settings(), topics()
    job = ScanJob(kind, write)

    def work() -> None:
        try:
            src = onboarding.connect(conf)
            with Store(conf.db_path) as s:
                p = Pipeline(s, src, topic_set, budget=jf.Budget(usd=cap), write_labels=write)
                if kind == "Re-check":
                    job.report = p.recheck_reviews(progress=job.progress)
                else:
                    job.report = p.sync(since=since, progress=job.progress)
        except Exception as e:  # shown on the page; the scan resumes next time
            job.error = str(e)
        finally:
            job.finished = True

    _jobs()["current"] = job
    threading.Thread(target=work, name="jgf-scan", daemon=True).start()


def scan_banner() -> None:
    job = current_job()
    if job is None or job.dismissed:
        return
    if not job.finished:
        live_progress()
        return
    if job.error:
        st.error(f"{job.kind} failed: {job.error}. Try again; already-judged mail is skipped.")
    elif job.report is not None:
        if job.kind == "First scan":
            st.subheader("First scan done 🎉")
        show_report(job.report, job.labels_on)
    if st.button("Dismiss", key="dismiss-scan"):
        job.dismissed = True
        st.rerun()


@st.fragment(run_every=1.0)
def live_progress() -> None:
    job = current_job()
    if job is None or job.finished:
        st.rerun()  # redraw the whole page with the result
        return
    done = job.n / job.total if job.total else 0.0
    text = (
        f"{job.kind}: judged {job.n} of {job.total} emails…"
        if job.total
        else f"{job.kind}: checking your inbox…"
    )
    st.progress(done, text=text + " You can switch pages or refresh; it keeps going.")


def show_report(report: SyncReport, labels_on: bool) -> None:
    matched = ", ".join(f"{t}: {n}" for t, n in report.matched.most_common()) or "nothing"
    msg = (
        f"Judged **{report.judged}** new email(s). Matched {matched}. "
        f"{report.reviews} need your review. Items: {report.items_created} new, "
        f"{report.items_updated} updated. Cost ${report.cost_usd:.4f}."
    )
    if not labels_on:
        msg += " Labels are off (dry run)."
    (st.warning if report.stopped else st.success)(msg)
    if report.stopped:
        st.warning(f"Stopped early: {report.stopped}")
    for e in report.errors:
        st.warning(e)


def fields_text(fields: dict[str, Any]) -> str:
    return " · ".join(str(v) for v in fields.values() if v) or "(no details yet)"


def when(iso: str | None) -> str:
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    days = (datetime.now(UTC) - dt).days
    return dt.strftime("%b %d") + (f" · {days}d ago" if days else " · today")


# -- setup wizard ---------------------------------------------------------------------


def setup_wizard(state: onboarding.SetupState) -> None:
    st.title("📬 Set up jev-gmail-filter")
    st.caption(
        f"About 15 minutes, once. Everything stays on this computer, in `{settings().data_dir}`."
    )
    steps = [
        ("TypeSafe API key", state.api_key, step_api_key),
        ("Your Google OAuth client", state.client, step_client),
        ("Sign in to Gmail", state.signed_in, step_sign_in),
        ("Pick your topics", state.topics, step_topics),
        ("First scan", state.finished, step_scan),
    ]
    current = next(i for i, (_, done, _) in enumerate(steps) if not done)
    st.progress(current / len(steps), text=f"Step {current + 1} of {len(steps)}")
    for i, (title, done, render) in enumerate(steps):
        icon = "✅" if done else ("👉" if i == current else "⬜")
        with st.expander(f"{icon} {i + 1}. {title}", expanded=i == current):
            if i > current:
                st.caption("Finish the step above first.")
            else:
                render(done)


def step_api_key(done: bool) -> None:
    if done:
        st.success("Found your TypeSafe API key.")
        return
    st.write(
        "jevfilter uses TypeSafe's Jev to judge your email. Paste your API key; "
        "it's saved to `.env` in this folder, which git ignores."
    )
    key = st.text_input("TypeSafe API key", type="password")
    if st.button("Save key", type="primary", disabled=not key):
        onboarding.save_api_key(settings(), key)
        st.rerun()


def step_client(done: bool) -> None:
    s = settings()
    if done:
        st.success(f"Using the OAuth client from **{onboarding.client_summary(s)}**.")
        if st.button("Use a different client file"):
            s.credentials_path.unlink(missing_ok=True)
            onboarding.sign_out(s)
            st.rerun()
        return
    st.write(
        "You'll create **your own** Google OAuth client. Everyone who uses this app does, "
        "so no one else's app ever sees your mail. Google classes Gmail access as "
        "restricted: a shared client would need Google's review, your own doesn't."
    )
    st.link_button("Open Google Cloud Console ↗", "https://console.cloud.google.com")
    for n, (title, detail) in enumerate(onboarding.GOOGLE_STEPS[:-1], 1):
        st.markdown(f"**{n}. {title}.** {detail}")
    st.caption(onboarding.TESTING_NOTE)
    upload = st.file_uploader("Upload the JSON you downloaded in step 3", type=["json"])
    if upload is not None:
        try:
            onboarding.install_client(s, upload.getvalue())
        except SetupError as e:
            st.error(str(e))
        else:
            st.rerun()


def step_sign_in(done: bool) -> None:
    s = settings()
    if done:
        st.success(f"Signed in as **{meta('account', 'your Google account')}**.")
        return
    title, detail = onboarding.GOOGLE_STEPS[-1]
    st.write("A Google sign-in page opens in a new browser tab.")
    st.info(detail)
    if st.button("Sign in with Google", type="primary"):
        with st.spinner("Finish signing in in the browser tab that just opened…"):
            try:
                src = onboarding.sign_in(s)
            except SetupError as e:
                st.error(str(e))
                return
        st.session_state.source = src
        set_meta("account", src.address())
        st.rerun()


def step_topics(done: bool) -> None:
    s = settings()
    if done:
        st.success("Topics: " + ", ".join(topics()) + ". Edit them anytime on the Topics page.")
        return
    st.write(
        "A topic is a plain-English description of emails you care about. "
        "Start from these examples and edit them later."
    )
    chosen = []
    for ex in onboarding.examples():
        if st.checkbox(f"**{ex.name}**: {ex.description}", value=True, key=f"ex-{ex.name}"):
            chosen.append(ex.path)
    if st.button("Use these topics", type="primary", disabled=not chosen):
        onboarding.install_topics(s, chosen)
        st.rerun()


def step_scan(done: bool) -> None:
    s = settings()
    st.write(
        "How far back should the first scan go? Only your Primary tab is read; you can "
        "add Updates, Promotions and the other Gmail categories later in Settings."
    )
    choice = st.radio(
        "Scan back",
        [*onboarding.BACKSCAN_DAYS, "Custom"],
        index=2,
        horizontal=True,
        label_visibility="collapsed",
    )
    days = (
        st.number_input("Days", min_value=1, max_value=365, value=60)
        if choice == "Custom"
        else onboarding.BACKSCAN_DAYS[choice]
    )
    est = st.session_state.get("estimate")
    if est is None or st.session_state.get("estimate_days") != days:
        if st.button("Check how much mail that is"):
            with st.spinner("Counting emails…"):
                st.session_state.estimate = onboarding.estimate(source(), topics(), days)
                st.session_state.estimate_days = days
            st.rerun()
        return
    a, b = st.columns(2)
    a.metric("Emails in that window", len(est.ids))
    b.metric("Estimated cost", f"${est.low_usd:.4f} – ${est.high_usd:.4f}")
    dry = st.checkbox("Dry run first: judge without writing Gmail labels", value=True)
    go, skip = st.columns([1, 4])
    if go.button("Start scan", type="primary"):
        onboarding.finish(s, labels_on=not dry, backscan_days=days)
        start_scan("First scan", since=est.since, budget=onboarding.budget_for(est), labels=not dry)
        st.rerun()
    if skip.button("Skip for now"):
        onboarding.finish(s, labels_on=not dry, backscan_days=days)
        st.rerun()


# -- pages ------------------------------------------------------------------------------


def page_overview() -> None:
    st.title("Overview")
    with store() as s:
        reviews = len(s.reviews())
        items = [i for t in topics().values() if t.track for i in s.items(t.name)]
        cols = st.columns(5)
        cols[0].metric("Emails judged", s.email_count())
        cols[1].metric("To review", reviews)
        cols[2].metric("Tracked items", len(items))
        cols[3].metric("Gone quiet", sum(i.stale for i in items))
        cols[4].metric("Jev spend", f"${s.total_cost():.4f}")
        last = s.get_meta("last_sync_at")
    st.caption(f"Last sync: {when(last) if last else 'never'}")
    if meta("labels", "off") != "on":
        st.info("Labels are off (dry run). When the results look right, turn them on in Settings.")
    if st.button("Sync now", type="primary", disabled=scanning()):
        start_scan("Sync")
        st.rerun()
    auto_sync()


def auto_sync() -> None:
    minutes = int(meta("auto_sync_minutes", "0"))
    if not minutes:
        st.caption("Auto-sync is off (turn it on in Settings, or run `jev-gmail-filter watch`).")
        return

    @st.fragment(run_every=minutes * 60)
    def tick() -> None:
        if st.session_state.get("auto_ticks", 0) and not scanning():
            start_scan("Sync")
        st.session_state.auto_ticks = st.session_state.get("auto_ticks", 0) + 1
        st.caption(f"Auto-sync every {minutes} min while this page is open.")

    tick()


def page_review() -> None:
    st.title("Review")
    with store() as s:
        reviews = s.reviews()
        if not reviews:
            st.success("Nothing to review.")
            return
        st.caption(
            "Emails jevfilter wasn't sure about. Your answer is applied like a match "
            "(labels, items) and kept for tuning later."
        )
        if st.button(
            f"Re-check all {len(reviews)} with the latest topics",
            disabled=scanning(),
            help="Judge these emails again, e.g. after editing a topic or updating the app.",
        ):
            start_scan("Re-check")
            st.rerun()
        for r in reviews:
            with st.container(border=True):
                st.markdown(f"**{r.subject or '(no subject)'}**  \n{r.sender}")
                if r.kind == "topic":
                    cat = (r.suggestion.get("category") or {}).get("value")
                    st.write(
                        f"Does this belong to **{r.topic}**?"
                        + (f" It looks like *{cat}*." if cat else "")
                        + f" (confidence {r.suggestion.get('p', 0):.0%})"
                    )
                    yes, no, _ = st.columns([1, 1, 6])
                    if yes.button("Yes", key=f"y{r.id}", type="primary"):
                        resolve(r.id, "yes")
                    if no.button("No", key=f"n{r.id}"):
                        resolve(r.id, "no")
                else:
                    match = r.suggestion.get("match", {})
                    probs = match.get("probabilities", {})
                    options = {"new": f"A new {r.topic} item ({probs.get('new', 0):.0%})"}
                    for item_id in match.get("candidates", []):
                        item = s.item(int(item_id))
                        if item:
                            p = probs.get(str(item_id), 0)
                            options[str(item_id)] = (
                                f"{fields_text(item.fields)} — {item.status} ({p:.0%})"
                            )
                    st.write(f"Which **{r.topic}** item is this about?")
                    pick = st.radio(
                        "Item",
                        list(options),
                        format_func=options.get,
                        key=f"pick{r.id}",
                        label_visibility="collapsed",
                    )
                    if st.button("Confirm", key=f"c{r.id}", type="primary"):
                        resolve(r.id, pick)


def resolve(review_id: int, decision: str) -> None:
    try:
        st.toast(pipeline().resolve(review_id, decision))
    except ValueError as e:
        st.error(str(e))
        return
    st.rerun()


def page_items() -> None:
    st.title("Items")
    tracked = [t for t in topics().values() if t.track]
    if not tracked:
        st.info("None of your topics track items. Add a `track:` section to a topic.")
        return
    topic = (
        tracked[0]
        if len(tracked) == 1
        else next(t for t in tracked if t.name == st.selectbox("Topic", [t.name for t in tracked]))
    )
    assert topic.track is not None
    statuses = [*topic.track.statuses, *topic.track.terminal]
    show_stale = st.toggle("Only items that have gone quiet")
    with store() as s:
        items = [i for i in s.items(topic.name) if i.stale or not show_stale]
        cols = st.columns(len(statuses))
        for col, status in zip(cols, statuses, strict=True):
            here = [i for i in items if i.status == status]
            col.markdown(f"**{status}** ({len(here)})")
            for item in here:
                with col.container(border=True):
                    st.markdown(f"**{fields_text(item.fields)}**")
                    st.caption(
                        when(item.last_email_at)
                        + ("  \n:orange[● gone quiet]" if item.stale else "")
                    )
                    with st.popover("Details"):
                        item_details(s, topic, item, statuses)
    with st.expander("Add an item by hand"):
        add_item_form(topic, statuses)


def item_details(s: Store, topic: jf.Topic, item: Any, statuses: list[str]) -> None:
    for e in s.item_emails(item.id):
        st.markdown(
            f"- {e['subject']} · {when(e['received_at'])}"
            + (f" · *{e['category']}*" if e["category"] else "")
        )
    new = st.selectbox(
        "Status",
        statuses,
        index=statuses.index(item.status) if item.status in statuses else 0,
        key=f"st{item.id}",
    )
    notes = st.text_area("Notes", item.notes, key=f"no{item.id}")
    if st.button("Save", key=f"sv{item.id}"):
        assert topic.track is not None
        last = new if new in topic.track.statuses else item.last_stage
        s.set_item_status(item.id, new, last)
        s.set_item_notes(item.id, notes)
        st.rerun()


def add_item_form(topic: jf.Topic, statuses: list[str]) -> None:
    with st.form(f"add-{topic.name}"):
        values = {name: st.text_input(name.capitalize()) for name in topic.fields}
        status = st.selectbox("Status", statuses)
        if st.form_submit_button("Add"):
            assert topic.track is not None
            with store() as s:
                s.create_item(
                    topic.name,
                    {k: v for k, v in values.items() if v},
                    status,
                    status if status in topic.track.statuses else None,
                    source="manual",
                    last_email_at=datetime.now(UTC).isoformat(),
                )
            st.rerun()


def page_emails() -> None:
    st.title("Recent emails")
    with store() as s:
        rows = s.recent_emails(200)
    if not rows:
        st.info("Nothing judged yet. Run a sync from the Overview.")
        return
    icon = {"match": "✅", "review": "❓"}
    table = []
    for r in rows:
        found = [
            f"{icon[o]} {t}" + (f" / {c}" if c else "")
            for t, (o, c, _) in r["outcomes"].items()
            if o in icon
        ]
        table.append(
            {
                "When": when(r["received_at"]),
                "From": r["sender"],
                "Subject": r["subject"],
                "Topics": ", ".join(found) or "—",
            }
        )
    only = st.toggle("Only emails that matched a topic")
    st.dataframe(
        [t for t in table if t["Topics"] != "—" or not only], hide_index=True, width="stretch"
    )


def page_topics() -> None:
    st.title("Topics")
    s = settings()
    st.caption(
        f"Stored as YAML in `{s.topics_dir}`. Editing a topic makes older mail "
        "eligible to be judged again (Rescan below)."
    )
    files = onboarding.topic_files(s)
    for name, path in files.items():
        with st.expander(name):
            text = st.text_area("Definition", path.read_text(), height=300, key=f"yaml-{name}")
            save, delete = st.columns([1, 1])
            if save.button("Save", key=f"save-{name}", type="primary"):
                save_topic(path, text)
            if delete.checkbox("Delete this topic", key=f"del-{name}") and delete.button(
                "Confirm delete", key=f"delok-{name}"
            ):
                path.unlink()
                st.rerun()
    with st.expander("➕ New topic"):
        with st.form("new-topic"):
            name = st.text_input("Name", placeholder="Travel")
            desc = st.text_area(
                "What belongs, in plain English",
                placeholder="Flight, hotel and train bookings for my trips.",
            )
            cats = st.text_area(
                "Categories (optional, one per line as `name: description`)",
                placeholder="booking: Confirms a booking.\nchange: A change or cancellation.",
            )
            if st.form_submit_button("Create", type="primary"):
                create_topic(name, desc, cats)
    rescan()


def save_topic(path: Any, text: str) -> None:
    try:
        data = yaml.safe_load(text)
        jf.Topic.load(data)
    except (yaml.YAMLError, jf.TopicError, TypeError) as e:
        st.error(f"Not saved: {e}")
        return
    path.write_text(text)
    st.toast("Saved")
    st.rerun()


def create_topic(name: str, desc: str, cats: str) -> None:
    data: dict[str, Any] = {"name": name.strip(), "description": desc.strip()}
    categories = {}
    for line in cats.splitlines():
        if line.strip():
            key, _, text = line.partition(":")
            categories[key.strip()] = text.strip() or key.strip()
    if categories:
        data["categories"] = categories
    try:
        topic = jf.Topic.from_dict(data)
    except jf.TopicError as e:
        st.error("Not created:\n" + "\n".join(f"- {p}" for p in e.problems))
        return
    path = settings().topics_dir / onboarding.topic_filename(topic.name)
    if path.exists() or topic.name in onboarding.topic_files(settings()):
        st.error(f"A topic called {topic.name!r} already exists.")
        return
    path.write_text(topic.to_yaml())
    st.toast(f"Created {topic.name}")
    st.rerun()


def rescan() -> None:
    st.subheader("Rescan")
    st.caption(
        "Judge older mail against topics it hasn't been judged against "
        "(new or edited topics). Mail already judged is skipped."
    )
    days = st.number_input("Days back", min_value=1, max_value=365, value=14)
    if st.button("Rescan", disabled=scanning()):
        start_scan("Rescan", since=datetime.now(UTC) - timedelta(days=days))
        st.rerun()


CATEGORY_HELP = {
    "primary": "Personal and important mail, the default.",
    "updates": "Confirmations, receipts, bills, statements; some job platforms land here.",
    "promotions": "Marketing and deals.",
    "social": "Social networks and dating sites.",
    "forums": "Mailing lists and discussion groups.",
}


def gmail_categories() -> None:
    st.subheader("Which Gmail categories to read")
    st.caption(
        "Gmail sorts your inbox into these tabs, even if you've turned the tabs off. "
        "Only the ones ticked here are judged; more categories cost more Jev."
    )
    current = parse_categories(meta("categories", "primary"))
    counts = st.session_state.get("category_counts", {})
    chosen = []
    for key, (name, _) in CATEGORIES.items():
        label = f"**{name}**: {CATEGORY_HELP[key]}"
        if key in counts:
            label += f" ({counts[key]} in the last 14 days)"
        if st.checkbox(label, value=key in current, key=f"cat-{key}"):
            chosen.append(key)
    save, count = st.columns([1, 3])
    if count.button("Count emails per category (last 14 days)"):
        since = datetime.now(UTC) - timedelta(days=14)
        with st.spinner("Counting…"):
            st.session_state.category_counts = {
                key: sum(1 for _ in source().search(inbox_query((key,), since)))
                for key in CATEGORIES
            }
        st.rerun()
    if save.button("Save categories", type="primary", disabled=not chosen):
        set_meta("categories", ",".join(chosen))
        st.session_state.categories_added = sorted(set(chosen) - set(current))
        st.rerun()
    added = st.session_state.get("categories_added")
    if added:
        names = ", ".join(CATEGORIES[k][0] for k in added)
        st.info(
            f"Saved. New mail in {names} is read from now on. To include older mail "
            "from those categories, rescan:"
        )
        default = int(float(meta("backscan_days", "14")))
        days = st.number_input("Days back", 1, 365, default, key="cat-rescan-days")
        if st.button("Rescan now", disabled=scanning()):
            st.session_state.pop("categories_added")
            start_scan("Rescan", since=datetime.now(UTC) - timedelta(days=days))
            st.rerun()


def page_settings() -> None:
    st.title("Settings")
    s = settings()
    labels_on = meta("labels", "off") == "on"
    new = st.toggle(
        "Write Gmail labels",
        value=labels_on,
        help="Off is a dry run: emails are judged and stored but not labelled.",
    )
    if new != labels_on:
        set_meta("labels", "on" if new else "off")
        if new:
            with st.spinner("Labelling everything matched so far…"):
                n = pipeline(labels=True).apply_labels_to_matches()
            st.toast(f"Labels on; labelled {n} email(s)")
        st.rerun()
    cap = st.number_input(
        "Spend cap per sync (USD)", min_value=0.01, step=0.25, value=float(meta("max_usd", "1.0"))
    )
    minutes = st.number_input(
        "Auto-sync every N minutes while the app is open (0 = off)",
        min_value=0,
        max_value=1440,
        value=int(meta("auto_sync_minutes", "0")),
    )
    if st.button("Save settings"):
        set_meta("max_usd", f"{cap:g}")
        set_meta("auto_sync_minutes", str(int(minutes)))
        st.toast("Saved")
    st.divider()
    gmail_categories()
    st.divider()
    st.markdown(
        f"**Google account:** {meta('account', 'unknown')} "
        f"(client from {onboarding.client_summary(s)})"
    )
    if st.button("Sign out"):
        onboarding.sign_out(s)
        st.session_state.pop("source", None)
        st.rerun()
    st.caption(
        f"Data folder: `{s.data_dir}` · Revoke access anytime at "
        "https://myaccount.google.com/permissions"
    )


# -- entry ------------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="jev-gmail-filter", page_icon="📬", layout="wide")
    state = onboarding.state(settings())
    if not state.complete:
        setup_wizard(state)
        return
    with store() as s:
        pending = len(s.reviews())
    scan_banner()
    page = st.sidebar.radio("Go to", PAGES)
    if pending:
        st.sidebar.warning(f"{pending} email(s) to review")
    st.sidebar.caption(meta("account", ""))
    try:
        {
            "Overview": page_overview,
            "Review": page_review,
            "Items": page_items,
            "Emails": page_emails,
            "Topics": page_topics,
            "Settings": page_settings,
        }[page]()
    except SetupError as e:
        st.error(str(e))
        if st.button("Sign in again"):
            onboarding.sign_out(settings())
            st.session_state.pop("source", None)
            st.rerun()
    except (jf.JudgeError, jf.BudgetExceeded) as e:
        st.error(f"Jev: {e}")


main()
