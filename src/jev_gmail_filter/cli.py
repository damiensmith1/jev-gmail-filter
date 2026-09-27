"""`jev-gmail-filter` command line.

ui       the web UI, with the same guided setup on first run
init     guided setup: API key, your Google OAuth client, sign-in, topics, backscan
sync     judge new Primary-inbox mail
watch    sync every few minutes
review   list or resolve emails that need a person's decision
items    tracked items and their status
labels   turn labels on and label everything already matched (after a dry run)
status   what's set up and what it has cost so far
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jevfilter as jf

from . import __version__, onboarding
from .config import Settings, load_settings
from .db import Store
from .gmail import MailSource, SetupError, check_client_file
from .pipeline import Pipeline, SyncReport

Ask = Callable[[str], str]
Connect = Callable[[Settings], MailSource]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings(args.data_dir)
    try:
        return args.run(args, settings)
    except SetupError as e:
        print(f"setup: {e}", file=sys.stderr)
        return 2
    except jf.TopicError as e:
        print("invalid topics:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (jf.JudgeError, jf.BudgetExceeded) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jev-gmail-filter", description="Filter Gmail with plain-English topics."
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--data-dir",
        help="where your settings, topics and database live (default ./data or $JGF_DATA_DIR)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="guided first-time setup").set_defaults(run=cmd_init)

    ui = sub.add_parser("ui", help="open the web UI (setup wizard on first run)")
    ui.add_argument("--port", type=int, default=8501)
    ui.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ui.set_defaults(run=cmd_ui)

    s = sub.add_parser("sync", help="judge new Primary-inbox mail")
    s.add_argument("--since-days", type=float, help="rescan this many days back")
    s.add_argument("--limit", type=int, help="process at most this many emails")
    s.add_argument("--max-usd", type=float, default=1.0, help="spend cap for this run ($1)")
    s.add_argument("--dry-run", action="store_true", help="judge and store, but don't label")
    s.set_defaults(run=cmd_sync)

    w = sub.add_parser("watch", help="sync every few minutes until stopped")
    w.add_argument("--interval", type=int, default=300, help="seconds between syncs (300)")
    w.add_argument("--max-usd", type=float, default=1.0, help="spend cap per sync ($1)")
    w.set_defaults(run=cmd_watch)

    r = sub.add_parser("review", help="list, or resolve: review <id> <yes|no|new|item id>")
    r.add_argument("id", nargs="?", type=int)
    r.add_argument("decision", nargs="?")
    r.add_argument("--recheck", action="store_true", help="judge everything in review again")
    r.set_defaults(run=cmd_review)

    i = sub.add_parser("items", help="tracked items")
    i.add_argument("topic", nargs="?")
    i.add_argument("--stale", action="store_true", help="only items that have gone quiet")
    i.set_defaults(run=cmd_items)

    lab = sub.add_parser("labels", help="turn labels on and label past matches")
    lab.add_argument("--off", action="store_true", help="stop writing labels")
    lab.set_defaults(run=cmd_labels)

    c = sub.add_parser("categories", help="show or set which Gmail categories to read")
    c.add_argument("categories", nargs="?", help="e.g. primary,updates (default: primary)")
    c.set_defaults(run=cmd_categories)

    sub.add_parser("status", help="setup and spend").set_defaults(run=cmd_status)
    return p


# -- init ----------------------------------------------------------------------------

BACKSCAN_CHOICES = {"1": 1.0, "2": 7.0, "3": 14.0, "4": 30.0}


def cmd_init(
    args: argparse.Namespace,
    settings: Settings,
    ask: Ask | None = None,
    connect: Connect | None = None,
) -> int:
    settings.ensure()
    prompt = ask or input
    print(f"Setting up jev-gmail-filter. Your files go in {settings.data_dir}")
    print("(Prefer a browser? `jev-gmail-filter ui` runs the same setup there.)\n")

    print("Step 1/5 - TypeSafe API key")
    if os.environ.get("TYPESAFE_API_KEY"):
        print("  found TYPESAFE_API_KEY\n")
    else:
        question = "  paste your TypeSafe API key: "
        key = ask(question) if ask else getpass.getpass(question + "(hidden) ")
        onboarding.save_api_key(settings, key)
        print(f"  saved to {settings.env_path.name} (gitignored)\n")

    print("Step 2/5 - Your Google OAuth client")
    if settings.credentials_path.exists():
        check_client_file(settings.credentials_path)
        print("  already set up\n")
    else:
        print("You'll create your own Google OAuth client. It takes about 10 minutes, once.")
        print("Everyone who uses this app does this, so no one else's app ever sees your mail.\n")
        for n, (title, detail) in enumerate(onboarding.GOOGLE_STEPS, 1):
            print(f"  {n}. {title}: {detail}")
        print(f"\n  {onboarding.TESTING_NOTE}")
        raw = prompt("\n  path to the JSON you downloaded in step 3: ").strip().strip("'\"")
        onboarding.install_client(settings, Path(raw))
        print(f"  copied to {settings.credentials_path} (gitignored)\n")

    print("Step 3/5 - Sign in to Gmail")
    source = (connect or onboarding.sign_in)(settings)
    print(f"  signed in as {source.address()}\n")

    print("Step 4/5 - Topics")
    topics = _choose_topics(settings, prompt)
    print(f"  topics: {', '.join(topics)} (edit them in {settings.topics_dir})\n")

    print("Step 5/5 - How far back to scan")
    days = _choose_backscan(prompt)
    est = onboarding.estimate(source, topics, days)
    print(f"  {len(est.ids)} emails in your Primary inbox from the last {days:g} days")
    print(f"  estimated Jev cost: ${est.low_usd:.4f} to ${est.high_usd:.4f}")
    dry = prompt("  dry run first (judge without writing Gmail labels)? [Y/n] ").strip().lower()
    labels_on = dry in ("n", "no")
    onboarding.finish(settings, labels_on=labels_on, backscan_days=days)
    if prompt("  start the scan now? [Y/n] ").strip().lower() in ("", "y", "yes"):
        pipeline = Pipeline(
            Store(settings.db_path),
            source,
            topics,
            budget=jf.Budget(usd=onboarding.budget_for(est)),
            write_labels=labels_on,
        )
        _print_report(pipeline.sync(since=est.since, progress=_progress), labels_on)
    print(
        "\nDone. Next: `jev-gmail-filter ui`, `jev-gmail-filter review`, "
        "or keep it running with `jev-gmail-filter watch`."
    )
    if not labels_on:
        print("When you're happy with the results, `jev-gmail-filter labels` turns labels on.")
    return 0


def _choose_topics(settings: Settings, ask: Ask) -> jf.Topics:
    if onboarding.has_topics(settings):
        return jf.Topic.load(settings.topics_dir)
    examples = onboarding.examples()
    for n, ex in enumerate(examples, 1):
        print(f"  {n}. {ex.name}: {ex.description}")
    raw = ask("  which examples to start with? (e.g. 1,2; Enter for all) ").strip()
    try:
        picks = examples if not raw else [examples[int(x) - 1] for x in raw.split(",")]
    except (ValueError, IndexError):
        raise ValueError(f"choose numbers from 1 to {len(examples)}") from None
    return onboarding.install_topics(settings, [ex.path for ex in picks])


def _choose_backscan(ask: Ask) -> float:
    raw = ask("  1) 1 day  2) 1 week  3) 2 weeks  4) 1 month  5) custom  [3]: ").strip() or "3"
    if raw in BACKSCAN_CHOICES:
        return BACKSCAN_CHOICES[raw]
    if raw == "5":
        days = float(ask("  how many days? ").strip())
        if days <= 0:
            raise ValueError("days must be positive")
        return days
    raise ValueError("choose 1 to 5")


def _connect(settings: Settings) -> MailSource:
    if not settings.token_path.exists():
        raise SetupError("not signed in yet; run `jev-gmail-filter init`")
    return onboarding.connect(settings)


def cmd_ui(args: argparse.Namespace, settings: Settings) -> int:
    """Start the web UI on this computer and open it in the browser."""
    import threading
    import urllib.request
    import webbrowser

    import uvicorn

    from .web.app import create_app

    settings.ensure()
    url = f"http://localhost:{args.port}"

    def open_when_ready() -> None:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/static/app.css", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        print(f"jev-gmail-filter is running at {url} (on this computer only).", flush=True)
        print("Press Ctrl-C here to stop it.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    threading.Thread(target=open_when_ready, daemon=True).start()
    app = create_app(settings)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        app.state.runtime.stop()
    return 0


# -- everyday commands -------------------------------------------------------------------


def _open(
    settings: Settings,
    connect: Connect | None = None,
    *,
    budget: jf.Budget | None = None,
    labels: bool | None = None,
) -> Pipeline:
    if not settings.db_path.exists():
        raise SetupError("not set up yet; run `jev-gmail-filter init`")
    topics = jf.Topic.load(settings.topics_dir)
    if not topics:
        raise SetupError(f"no topics in {settings.topics_dir}; add one or run init")
    store = Store(settings.db_path)
    write = store.get_meta("labels") == "on" if labels is None else labels
    source = (connect or _connect)(settings)
    return Pipeline(store, source, topics, budget=budget, write_labels=write)


def cmd_sync(args: argparse.Namespace, settings: Settings, connect: Connect | None = None) -> int:
    labels = False if args.dry_run else None
    p = _open(settings, connect, budget=jf.Budget(usd=args.max_usd), labels=labels)
    since = datetime.now(UTC) - timedelta(days=args.since_days) if args.since_days else None
    report = p.sync(since=since, limit=args.limit, progress=_progress)
    _print_report(report, p.write_labels)
    return 1 if report.stopped else 0


def cmd_watch(
    args: argparse.Namespace,
    settings: Settings,
    connect: Connect | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rounds: int | None = None,
) -> int:
    print(f"watching every {args.interval}s (Ctrl-C to stop)")
    n = 0
    while rounds is None or n < rounds:
        p = _open(settings, connect, budget=jf.Budget(usd=args.max_usd))
        report = p.sync()
        if report.judged or report.stopped or report.errors:
            print(f"[{datetime.now():%H:%M}] ", end="")
            _print_report(report, p.write_labels)
        n += 1
        if rounds is None or n < rounds:
            sleep(args.interval)
    return 0


def cmd_review(args: argparse.Namespace, settings: Settings, connect: Connect | None = None) -> int:
    if getattr(args, "recheck", False):
        p = _open(settings, connect, budget=jf.Budget(usd=1.0))
        _print_report(p.recheck_reviews(progress=_progress), p.write_labels)
        return 0
    if args.id is not None:
        if not args.decision:
            raise ValueError("give a decision: yes / no (topic) or an item id / new (item)")
        p = _open(settings, connect)
        print(f"#{args.id}: {p.resolve(args.id, args.decision)}")
        return 0
    store = Store(settings.db_path)
    reviews = store.reviews()
    if not reviews:
        print("nothing to review")
        return 0
    for r in reviews:
        print(f"#{r.id}  [{r.topic}] {r.subject or '(no subject)'}  - {r.sender}")
        if r.kind == "topic":
            cat = (r.suggestion.get("category") or {}).get("value")
            looks = f", looks like '{cat}'" if cat else ""
            print(
                f"      does it belong to {r.topic}? p={r.suggestion.get('p', 0):.2f}{looks}"
                f"  ({', '.join(r.reasons)})"
            )
            print(f"      -> jev-gmail-filter review {r.id} yes|no")
        else:
            match = r.suggestion.get("match", {})
            probs = sorted(match.get("probabilities", {}).items(), key=lambda kv: -kv[1])
            print("      which item is it about? " + ", ".join(f"{k} {v:.0%}" for k, v in probs))
            for item_id in match.get("candidates", []):
                item = store.item(int(item_id))
                if item:
                    print(f"        item {item.id}: {_fields(item.fields)} ({item.status})")
            print(f"      -> jev-gmail-filter review {r.id} <item id>|new")
    return 0


def cmd_items(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings.db_path)
    topics = jf.Topic.load(settings.topics_dir)
    names = [args.topic] if args.topic else [t for t in topics if topics[t].track]
    for name in names:
        items = [i for i in store.items(name) if i.stale or not args.stale]
        print(f"{name}: {len(items)} item(s)")
        for i in items:
            flag = "  STALE" if i.stale else ""
            print(
                f"  #{i.id:<4} {_fields(i.fields):<45} {i.status or '-':<14} "
                f"{(i.last_email_at or '')[:10]}{flag}"
            )
    return 0


def cmd_labels(args: argparse.Namespace, settings: Settings, connect: Connect | None = None) -> int:
    if args.off:
        Store(settings.db_path).set_meta("labels", "off")
        print("labels off: syncs will judge and store, but not label")
        return 0
    p = _open(settings, connect, labels=True)
    p.store.set_meta("labels", "on")
    print(f"labels on; labelled {p.apply_labels_to_matches().labelled} email(s) matched so far")
    return 0


def cmd_categories(args: argparse.Namespace, settings: Settings) -> int:
    from .gmail import CATEGORIES, parse_categories

    if not settings.db_path.exists():
        raise SetupError("not set up yet; run `jev-gmail-filter init`")
    with Store(settings.db_path) as store:
        if args.categories:
            chosen = parse_categories(args.categories)
            store.set_meta("categories", ",".join(chosen))
            print("reading: " + ", ".join(CATEGORIES[c][0] for c in chosen))
            print("older mail in newly added categories: `jev-gmail-filter sync --since-days N`")
        else:
            chosen = parse_categories(store.get_meta("categories"))
            print("reading: " + ", ".join(CATEGORIES[c][0] for c in chosen))
            print("available: " + ", ".join(CATEGORIES))
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    print(f"data folder:   {settings.data_dir}")
    print(f"API key:       {'set' if os.environ.get('TYPESAFE_API_KEY') else 'missing'}")
    print(f"OAuth client:  {'set' if settings.credentials_path.exists() else 'missing'}")
    print(f"signed in:     {'yes' if settings.token_path.exists() else 'no'}")
    if not settings.db_path.exists():
        print("not set up yet; run `jev-gmail-filter init`")
        return 0
    store = Store(settings.db_path)
    print(f"labels:        {store.get_meta('labels', 'off')}")
    print(f"last sync:     {store.get_meta('last_sync_at', 'never')}")
    print(f"emails judged: {store.email_count()}")
    print(f"to review:     {len(store.reviews())}")
    print(f"Jev spend:     ${store.total_cost():.4f}")
    return 0


# -- output ------------------------------------------------------------------------------


def _progress(n: int, total: int) -> None:
    if total >= 10 and (n % max(total // 10, 1) == 0 or n == total):
        print(f"  {n}/{total}", flush=True)


def _print_report(report: SyncReport, labels_on: bool) -> None:
    matched = ", ".join(f"{t} {n}" for t, n in report.matched.most_common()) or "none"
    print(
        f"judged {report.judged} new email(s) (skipped {report.skipped} already done); "
        f"matched: {matched}; {report.reviews} to review; items +{report.items_created} new, "
        f"{report.items_updated} updated; ${report.cost_usd:.4f}"
        + ("" if labels_on else "; labels off (dry run)")
    )
    for e in report.errors:
        print(f"  warning: {e}")
    if report.stopped:
        print(f"  stopped early: {report.stopped}")


def _fields(fields: dict[str, Any]) -> str:
    return " / ".join(str(v) for v in fields.values() if v) or "(no fields)"


if __name__ == "__main__":
    raise SystemExit(main())
