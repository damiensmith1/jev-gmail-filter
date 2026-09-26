"""The web UI (FastAPI + server-rendered HTML): `jev-gmail-filter ui`.

Pages are plain HTML forms and links; each action POSTs and redirects back
(no client framework). Long work (scans, sign-in) runs in `Runtime` threads
and pages poll a small JSON endpoint for progress.

It listens on 127.0.0.1 only and has access to your Gmail, so every request
must be addressed to localhost (blocks DNS-rebinding) and every POST must come
from this app's own pages (blocks other websites posting to it).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import jevfilter as jf
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .. import candidates, onboarding
from .. import topic_form as tf
from ..config import Settings
from ..gmail import CATEGORIES, SetupError, inbox_query, parse_categories
from ..mail import Email
from . import views
from .runtime import Connect, Runtime

HERE = Path(__file__).parent
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1", "testserver"}
CATEGORY_HELP = {
    "primary": "Personal and important mail. The default.",
    "updates": "Confirmations, receipts, bills and statements; some job platforms land here.",
    "promotions": "Marketing and deals.",
    "social": "Social networks.",
    "forums": "Mailing lists and discussion groups.",
}


class LocalOnly(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in LOCAL_HOSTS and not host.startswith("["):
            return Response("This app only answers on localhost.", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return Response("Cross-site request refused.", status_code=403)
        return await call_next(request)


def create_app(
    settings: Settings,
    *,
    connect: Connect | None = None,
    sign_in: Connect | None = None,
    judge: Any = None,
    auto_sync: bool = True,
) -> FastAPI:
    rt = Runtime(settings, connect=connect, sign_in=sign_in, judge=judge, auto_sync=auto_sync)
    app = FastAPI(title="jev-gmail-filter", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = rt
    app.add_middleware(LocalOnly)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(pct=lambda x: f"{float(x or 0):.0%}", money=_money)

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        job = rt.job
        ctx.setdefault("scan", job.as_dict() if job and not job.dismissed else None)
        ctx.setdefault(
            "scan_report", _report(job) if job and job.finished and not job.dismissed else None
        )
        return templates.TemplateResponse(request, name, ctx)

    def page(request: Request, name: str, active: str, **ctx: Any) -> HTMLResponse:
        with rt.store() as s:
            ctx["nav"] = views.nav(s, rt.topics(), active)
        ctx.setdefault("flash", request.query_params.get("flash"))
        return render(request, name, **ctx)

    def back(request: Request, fallback: str = "/", **params: str | None) -> RedirectResponse:
        """Redirect to the page the form was on. `params` override its query string; a
        None value removes that key."""
        from urllib.parse import parse_qsl

        target = request.headers.get("referer") or fallback
        parsed = urlparse(target)
        path = parsed.path or fallback
        query = {k: v for k, v in parse_qsl(parsed.query) if k != "flash"}
        for k, v in params.items():
            if v is None:
                query.pop(k, None)
            else:
                query[k] = v
        return RedirectResponse(path + ("?" + urlencode(query) if query else ""), 303)

    def needs_setup() -> bool:
        return not onboarding.state(settings).complete

    # -- overview --------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> Response:
        if needs_setup():
            return RedirectResponse("/setup", 303)
        with rt.store() as s:
            data = views.overview(s, rt.topics())
        return page(request, "overview.html", "overview", o=data)

    # -- needs you ----------------------------------------------------------------------

    @app.get("/needs", response_class=HTMLResponse)
    def needs(request: Request, selected: str = "") -> Response:
        with rt.store() as s:
            data = views.needs(s, rt.topics(), _int(selected))
        body = rt.email(data["detail"]["gmail_id"]) if data["detail"] else None
        return page(request, "needs.html", "needs", d=data, body=_body(body))

    @app.post("/reviews/{review_id}")
    async def resolve(request: Request, review_id: int) -> Response:
        form = await request.form()
        decision = str(form.get("decision", ""))
        fields = {
            k.removeprefix("field-"): str(v) for k, v in form.items() if k.startswith("field-")
        }
        with rt.store() as s:
            try:
                message = rt.pipeline(s).resolve(review_id, decision, fields)
            except (ValueError, SetupError) as e:
                message = str(e)
        return back(request, "/needs", flash=message, selected=None)

    @app.post("/reviews/recheck")
    def recheck(request: Request) -> Response:
        rt.start_scan("Re-check")
        return back(request, "/needs")

    # -- everything -----------------------------------------------------------------------

    @app.get("/emails", response_class=HTMLResponse)
    def emails(
        request: Request,
        kind: str = "all",
        topic: str = "",
        q: str = "",
        selected: str = "",
        offset: str = "",
    ) -> Response:
        kind = kind if kind in ("all", "matched", "review", "other") else "all"
        with rt.store() as s:
            data = views.everything(
                s,
                rt.topics(),
                kind=kind,
                topic=topic or None,
                search=q,
                selected=selected or None,
                offset=_int(offset) or 0,
            )
        return page(request, "everything.html", "everything", d=data)

    @app.post("/emails/{gmail_id}/wrong")
    async def wrong(request: Request, gmail_id: str) -> Response:
        form = await request.form()
        with rt.store() as s:
            try:
                message = rt.pipeline(s).mark_wrong(gmail_id, str(form.get("topic", "")))
            except (ValueError, SetupError) as e:
                message = str(e)
        return back(request, "/emails", flash=message)

    # -- items ----------------------------------------------------------------------------------

    @app.get("/items", response_class=HTMLResponse)
    def items(
        request: Request,
        topic: str = "",
        view: str = "board",
        selected: str = "",
        closed: str = "",
    ) -> Response:
        with rt.store() as s:
            data = views.items(
                s,
                rt.topics(),
                topic=topic or None,
                view="list" if view == "list" else "board",
                selected=_int(selected),
                closed=closed not in ("", "0"),
            )
        return page(request, "items.html", "items", d=data)

    @app.post("/items/{item_id}/status")
    async def item_status(request: Request, item_id: int) -> Response:
        form = await request.form()
        with rt.store() as s:
            p = rt.pipeline(s)
            try:
                p.set_item_status(item_id, str(form.get("status", "")))
                message = "Status updated"
            except ValueError as e:
                message = str(e)
        return back(request, "/items", flash=message)

    @app.post("/items/{item_id}/notes")
    async def item_notes(request: Request, item_id: int) -> Response:
        form = await request.form()
        with rt.store() as s:
            s.set_item_notes(item_id, str(form.get("notes", "")))
        return back(request, "/items", flash="Notes saved")

    @app.post("/items")
    async def add_item(request: Request) -> Response:
        form = await request.form()
        topic = rt.topics().get(str(form.get("topic", "")))
        if topic is None or topic.track is None:
            return back(request, "/items", flash="That topic doesn't track items")
        fields = {f: str(form.get(f"field-{f}", "")).strip() for f in topic.fields}
        status = str(form.get("status", "")) or next(iter(topic.track.statuses), None)
        with rt.store() as s, s.transaction():
            item_id = s.create_item(
                topic.name,
                {k: v for k, v in fields.items() if v},
                status,
                status if status in topic.track.statuses else None,
                source="manual",
                last_email_at=datetime.now(UTC).isoformat(),
            )
            s.add_event(item_id, "manual", status)
        return RedirectResponse(f"/items?topic={topic.name}&selected={item_id}", 303)

    # -- topics ----------------------------------------------------------------------------------

    @app.get("/topics", response_class=HTMLResponse)
    def topics(request: Request, saved: str = "") -> Response:
        files = onboarding.topic_files(settings)
        cards = []
        with rt.store() as s:
            for name, path in files.items():
                t = jf.Topic.load(path)[name]
                stats = s.topic_stats(name)
                cards.append(
                    {
                        "name": name,
                        "description": " ".join(str(t.description).split()),
                        "categories": len(t.categories),
                        "fields": list(t.fields),
                        "tracked": t.track is not None,
                        "label": views.label_for(t) or "",
                        "matches": stats["matches"],
                        "last": views.clock(stats["last_match"])
                        if stats["last_match"]
                        else "never",
                        "reviews": stats["reviews"],
                    }
                )
        starters = [
            "Blank",
            *[f"Starter: {n}" for n in tf.STARTERS],
            *[f"Example: {e.name}" for e in onboarding.examples()],
        ]
        with rt.store() as s:
            days = int(float(s.get_meta("backscan_days") or 14))
        return page(
            request, "topics.html", "topics", cards=cards, starters=starters, saved=saved, days=days
        )

    @app.get("/topics/new", response_class=HTMLResponse)
    def new_topic(request: Request, start: str = "Blank") -> Response:
        if start.startswith("Starter: ") and start[9:] in tf.STARTERS:
            form = tf.from_dict(tf.STARTERS[start[9:]])
        elif start.startswith("Example: "):
            ex = next((e for e in onboarding.examples() if e.name == start[9:]), None)
            form = (
                tf.from_topic(next(iter(jf.Topic.load(ex.path).values()))) if ex else tf.TopicForm()
            )
        else:
            form = tf.TopicForm()
        return editor(request, form, path="", version="", explicit=form.gmail_label is not None)

    @app.get("/topics/{name}/edit", response_class=HTMLResponse)
    def edit_topic(request: Request, name: str) -> Response:
        path = onboarding.topic_files(settings).get(name)
        if path is None:
            return RedirectResponse("/topics", 303)
        topic = jf.Topic.load(path)[name]
        form = tf.from_topic(topic)
        return editor(
            request,
            form,
            path=str(path),
            version=topic.version,
            explicit=form.gmail_label is not None,
        )

    @app.post("/topics/edit", response_class=HTMLResponse)
    async def edit_post(request: Request) -> Response:
        data = await request.form()
        form, action = _parse_topic_form(data)
        path, version = str(data.get("path", "")), str(data.get("version", ""))
        explicit = data.get("label_explicit") == "1"
        try_result = None
        error = None
        if action == "apply-yaml":
            try:
                loaded = yaml.safe_load(str(data.get("yaml", "")))
                jf.Topic.from_dict(loaded)
                form = tf.from_dict(loaded)
                explicit = form.gmail_label is not None
            except (yaml.YAMLError, jf.TopicError, TypeError, AttributeError) as e:
                error = f"YAML not applied: {e}"
        elif action.startswith("add-"):
            section = action[4:]
            _add_row(form, section)
        elif action.startswith("del-"):
            _, section, index = action.split("-", 2)
            _del_row(form, section, int(index))
        topic, problems = tf.build(form)
        if action == "try" and topic is not None:
            try:
                try_result = _try_topic(
                    rt, topic, str(data.get("try_email", "")), str(data.get("try_text", ""))
                )
            except (jf.JudgeError, jf.BudgetExceeded, SetupError) as e:
                error = f"Jev: {e}"
        if action == "save" and topic is not None:
            existing = onboarding.topic_files(settings)
            clash = existing.get(topic.name)
            target = (
                Path(path) if path else settings.topics_dir / onboarding.topic_filename(topic.name)
            )
            if clash is not None and str(clash) != path:
                error = f"There's already a topic called {topic.name!r}."
            elif not path and target.exists():
                error = f"A file named {target.name} already exists; pick another name."
            else:
                old = next(iter(jf.Topic.load(target).values()), None) if target.exists() else None
                if old is not None and old.to_dict() == topic.to_dict():
                    # Nothing changed: leave the file (and any hand formatting) untouched.
                    return RedirectResponse("/topics?" + urlencode({"flash": "No changes"}), 303)
                target.write_text(topic.to_yaml())
                changed = topic.version != version
                return RedirectResponse(
                    "/topics?"
                    + urlencode(
                        {"saved": topic.name if changed else "", "flash": f"Saved {topic.name}"}
                    ),
                    303,
                )
        return editor(
            request,
            form,
            path=path,
            version=version,
            explicit=explicit,
            problems=problems,
            try_result=try_result,
            error=error,
            try_email=str(data.get("try_email", "")),
            try_text=str(data.get("try_text", "")),
        )

    def editor(
        request: Request,
        form: tf.TopicForm,
        *,
        path: str,
        version: str,
        explicit: bool,
        problems: list[str] | None = None,
        try_result: Any = None,
        error: str | None = None,
        try_email: str = "",
        try_text: str = "",
    ) -> HTMLResponse:
        if problems is None:
            problems = tf.build(form)[1]
        with rt.store() as s:
            recent = [
                {
                    "id": e["gmail_id"],
                    "label": f"{e['subject'] or '(no subject)'} — {views.sender_name(e['sender'])}",
                }
                for e in s.recent_emails(50)
            ]
        preview = yaml.safe_dump(tf.to_dict(form), sort_keys=False, allow_unicode=True)
        return page(
            request,
            "topic_edit.html",
            "topic",
            f=form,
            path=path,
            version=version,
            explicit=explicit,
            problems=problems,
            try_result=try_result,
            error=error,
            recent=recent,
            try_email=try_email,
            try_text=try_text,
            kinds=tf.FIELD_KINDS,
            yaml_text=preview,
            extra_json=json.dumps({"extra": form.extra, "meta": form.extra_meta}),
            editing=form.name if path else "",
        )

    @app.post("/topics/{name}/delete")
    def delete_topic(request: Request, name: str) -> Response:
        path = onboarding.topic_files(settings).get(name)
        if path is not None:
            path.unlink()
        return RedirectResponse("/topics?" + urlencode({"flash": f"Deleted {name}"}), 303)

    # -- settings -------------------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, counts: str = "") -> Response:
        with rt.store() as s:
            chosen = parse_categories(s.get_meta("categories"))
            ctx = {
                "labels_on": s.get_meta("labels") == "on",
                "max_usd": float(s.get_meta("max_usd") or 1.0),
                "auto_minutes": int(s.get_meta("auto_sync_minutes") or 0),
                "account": s.get_meta("account") or "",
                "total": s.total_cost(),
            }
        category_counts: dict[str, int] = {}
        if counts:
            since = datetime.now(UTC) - timedelta(days=14)
            try:
                category_counts = {
                    k: sum(1 for _ in rt.source().search(inbox_query((k,), since)))
                    for k in CATEGORIES
                }
            except Exception as e:
                ctx["flash"] = f"Couldn't count: {e}"
        cats = [
            {
                "key": k,
                "name": n,
                "help": CATEGORY_HELP[k],
                "on": k in chosen,
                "count": category_counts.get(k),
            }
            for k, (n, _) in CATEGORIES.items()
        ]
        return page(
            request,
            "settings.html",
            "settings",
            cats=cats,
            project=onboarding.client_summary(settings),
            data_dir=str(settings.data_dir),
            **ctx,
        )

    @app.post("/settings")
    async def save_settings(request: Request) -> Response:
        form = await request.form()
        with rt.store() as s:
            was_on = s.get_meta("labels") == "on"
            labels_on = form.get("labels") == "on"
            s.set_meta("labels", "on" if labels_on else "off")
            s.set_meta("max_usd", f"{max(float(form.get('max_usd') or 1), 0.01):g}")
            s.set_meta("auto_sync_minutes", str(max(int(form.get("auto_minutes") or 0), 0)))
            message = "Settings saved"
            if labels_on and not was_on:
                n = rt.pipeline(s, labels=True).apply_labels_to_matches()
                message = f"Labels on; labelled {n} email(s) matched so far"
        return RedirectResponse("/settings?" + urlencode({"flash": message}), 303)

    @app.post("/settings/categories")
    async def save_categories(request: Request) -> Response:
        form = await request.form()
        chosen = [k for k in CATEGORIES if form.get(f"cat-{k}") == "on"] or ["primary"]
        with rt.store() as s:
            before = set(parse_categories(s.get_meta("categories")))
            s.set_meta("categories", ",".join(chosen))
        added = [CATEGORIES[k][0] for k in chosen if k not in before]
        message = "Saved" + (f". Rescan to include older {', '.join(added)} mail." if added else "")
        return RedirectResponse("/settings?" + urlencode({"flash": message}), 303)

    @app.post("/signout")
    def sign_out(request: Request) -> Response:
        onboarding.sign_out(settings)
        rt.forget_source()
        return RedirectResponse("/setup", 303)

    # -- scans ------------------------------------------------------------------------------------

    @app.post("/sync")
    def sync(request: Request) -> Response:
        rt.start_scan("Sync")
        return back(request)

    @app.post("/rescan")
    async def rescan(request: Request) -> Response:
        form = await request.form()
        days = max(float(form.get("days") or 14), 1)
        rt.start_scan("Rescan", since=datetime.now(UTC) - timedelta(days=days))
        return back(request)

    @app.get("/scan/status")
    def scan_status() -> JSONResponse:
        job = rt.job
        return JSONResponse(job.as_dict() if job else {"finished": True})

    @app.post("/scan/dismiss")
    def scan_dismiss(request: Request) -> Response:
        if rt.job is not None and rt.job.finished:
            rt.job.dismissed = True
        return back(request)

    # -- setup ------------------------------------------------------------------------------------

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, days: str = "") -> Response:
        state = onboarding.state(settings)
        if state.complete:
            return RedirectResponse("/", 303)
        steps = [
            ("TypeSafe API key", state.api_key),
            ("Your Google client", state.client),
            ("Sign in to Gmail", state.signed_in),
            ("Pick your topics", state.topics),
            ("First scan", state.finished),
        ]
        current = next(i for i, (_, done) in enumerate(steps) if not done)
        estimate = None
        days_n = _float(days)
        if current == 4 and days_n:
            try:
                est = onboarding.estimate(rt.source(), rt.topics(), days_n)
                estimate = {
                    "count": len(est.ids),
                    "low": est.low_usd,
                    "high": est.high_usd,
                    "days": days_n,
                }
            except Exception as e:
                estimate = {"error": str(e), "days": days_n}
        with rt.store() as s:
            account = s.get_meta("account") or ""
        return render(
            request,
            "setup.html",
            steps=steps,
            current=current,
            google_steps=onboarding.GOOGLE_STEPS[:-1],
            signin_note=onboarding.GOOGLE_STEPS[-1][1],
            testing_note=onboarding.TESTING_NOTE,
            signin=rt.signin,
            examples=onboarding.examples(),
            choices=onboarding.BACKSCAN_DAYS,
            estimate=estimate,
            account=account,
            flash=request.query_params.get("flash"),
        )

    @app.post("/setup/key")
    async def setup_key(request: Request) -> Response:
        form = await request.form()
        try:
            onboarding.save_api_key(settings, str(form.get("key", "")))
            return RedirectResponse("/setup", 303)
        except SetupError as e:
            return RedirectResponse("/setup?" + urlencode({"flash": str(e)}), 303)

    @app.post("/setup/client")
    async def setup_client(request: Request) -> Response:
        form = await request.form()
        upload = form.get("client")
        try:
            if upload is None or not hasattr(upload, "read"):
                raise SetupError("choose the JSON file you downloaded")
            onboarding.install_client(settings, await upload.read())
            return RedirectResponse("/setup", 303)
        except SetupError as e:
            return RedirectResponse("/setup?" + urlencode({"flash": str(e)}), 303)

    @app.post("/setup/signin")
    def setup_signin() -> Response:
        rt.start_sign_in()
        return RedirectResponse("/setup", 303)

    @app.get("/setup/signin/status")
    def signin_status() -> JSONResponse:
        return JSONResponse({"state": rt.signin.state, "error": rt.signin.error})

    @app.post("/setup/topics")
    async def setup_topics(request: Request) -> Response:
        form = await request.form()
        chosen = [e.path for e in onboarding.examples() if form.get(f"ex-{e.name}") == "on"]
        try:
            onboarding.install_topics(settings, chosen)
            return RedirectResponse("/setup", 303)
        except ValueError as e:
            return RedirectResponse("/setup?" + urlencode({"flash": str(e)}), 303)

    @app.post("/setup/start")
    async def setup_start(request: Request) -> Response:
        form = await request.form()
        days = max(float(form.get("days") or 14), 1)
        dry = form.get("dry") == "on"
        onboarding.finish(settings, labels_on=not dry, backscan_days=days)
        if form.get("skip") != "1":
            est = onboarding.Estimate(
                datetime.now(UTC) - timedelta(days=days), [], 0.0, float(form.get("high") or 0)
            )
            rt.start_scan(
                "First scan", since=est.since, budget=onboarding.budget_for(est), labels=not dry
            )
        return RedirectResponse("/", 303)

    return app


# -- helpers -----------------------------------------------------------------------------------


def _int(value: str | None) -> int | None:
    """A query-string number, or None when empty or not a number."""
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _money(x: float | None) -> str:
    x = float(x or 0)
    return f"${x:.4f}" if x < 1 else f"${x:.2f}"


def _report(job: Any) -> dict | None:
    if job.error:
        return {"kind": job.kind, "error": job.error}
    r = job.report
    if r is None:
        return None
    return {
        "kind": job.kind,
        "judged": r.judged,
        "skipped": r.skipped,
        "matched": dict(r.matched),
        "reviews": r.reviews,
        "created": r.items_created,
        "updated": r.items_updated,
        "cost": r.cost_usd,
        "stopped": r.stopped,
        "errors": r.errors,
        "labels_on": job.labels_on,
    }


def _body(mail: Email | None) -> str | None:
    if mail is None:
        return None
    text = mail.body.strip()
    return text[:3000] + ("…" if len(text) > 3000 else "")


def _rows(data: Any, prefix: str, keys: list[str]) -> list[dict[str, str]]:
    indices = sorted(
        {
            int(k.split("-")[1])
            for k in data.keys()
            if k.startswith(prefix + "-") and k.split("-")[1].isdigit()
        }
    )
    return [{key: str(data.get(f"{prefix}-{i}-{key}", "")) for key in keys} for i in indices]


def _parse_topic_form(data: Any) -> tuple[tf.TopicForm, str]:
    extra = json.loads(str(data.get("extra_json") or "{}") or "{}")
    lines = lambda text: [x.strip() for x in str(text).splitlines() if x.strip()]  # noqa: E731
    split = lambda text, sep: [x.strip() for x in str(text).split(sep) if x.strip()]  # noqa: E731
    form = tf.TopicForm(
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        exclude=lines(data.get("exclude", "")),
        examples_match=lines(data.get("ex_match", "")),
        examples_no=lines(data.get("ex_no", "")),
        categories=[
            tf.CategoryRow(r["name"], r["desc"], split(r["examples"], ";"), r["not"])
            for r in _rows(data, "cat", ["name", "desc", "examples", "not"])
        ],
        fields=[
            tf.FieldRow(r["name"], r["kind"] or "other", r["about"], r["required"] == "on")
            for r in _rows(data, "field", ["name", "kind", "about", "required"])
        ],
        flags=[
            tf.FlagRow(r["name"], r["statement"])
            for r in _rows(data, "flag", ["name", "statement"])
        ],
        thresholds={
            "accept": float(data.get("th_accept") or 0.7),
            "reject": float(data.get("th_reject") or 0.3),
            "min_confidence": float(data.get("th_conf") or 0.5),
        },
        extra=extra.get("extra", {}),
        extra_meta=extra.get("meta", {}),
    )
    if data.get("track") == "on":
        form.track = tf.TrackForm(
            match_on=[str(x) for x in data.getlist("match_on")],
            statuses=[
                tf.StatusRow(r["name"], split(r["moved"], ","), r["closed"] == "on")
                for r in _rows(data, "status", ["name", "moved", "closed"])
            ],
            stale_after_days=int(float(data.get("stale") or 0)) or None,
        )
    label = str(data.get("label", "")).strip()
    if data.get("label_on") != "on":
        form.gmail_label = ""
    elif data.get("label_explicit") == "1" or (label and label != form.name.strip()):
        form.gmail_label = label or None
    else:
        form.gmail_label = None
    return form, str(data.get("action", ""))


def _add_row(form: tf.TopicForm, section: str) -> None:
    if section == "cat":
        form.categories.append(tf.CategoryRow(""))
    elif section == "field":
        form.fields.append(tf.FieldRow(""))
    elif section == "flag":
        form.flags.append(tf.FlagRow(""))
    elif section == "status":
        form.track = form.track or tf.TrackForm()
        form.track.statuses.append(tf.StatusRow(""))


def _del_row(form: tf.TopicForm, section: str, index: int) -> None:
    rows = {
        "cat": form.categories,
        "field": form.fields,
        "flag": form.flags,
        "status": form.track.statuses if form.track else [],
    }.get(section, [])
    if 0 <= index < len(rows):
        rows.pop(index)


def _try_topic(rt: Runtime, topic: jf.Topic, gmail_id: str, text: str) -> dict:
    if gmail_id:
        mail = rt.email(gmail_id)
        if mail is None:
            raise SetupError("couldn't fetch that email from Gmail")
        with rt.store() as s:
            known = {f: [i.fields.get(f) for i in s.items(topic.name)] for f in topic.fields}
    else:
        subject, _, body = text.partition("\n")
        mail = Email("try", "try", "", subject.strip(), datetime.now(UTC), body.strip())
        known = {f: [] for f in topic.fields}
    cands = {
        topic.name: {
            f: candidates.extract(mail, spec.kind, known[f]) for f, spec in topic.fields.items()
        }
    }
    f = jf.Filter([topic], judge=rt.judge, budget=jf.Budget(usd=0.01))
    r = f.judge(jf.Content(mail.state(), candidates=cands))
    t = next(iter(r))
    return {
        "outcome": t.outcome,
        "p": t.p,
        "reasons": list(t.reasons),
        "category": t.category.value if t.category else None,
        "category_p": t.category.confidence if t.category else None,
        "fields": {k: v.value for k, v in t.fields.items()},
        "flags": dict(t.flags),
        "cost": r.cost_usd or 0.0,
    }
