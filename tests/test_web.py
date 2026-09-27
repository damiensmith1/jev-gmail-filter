"""The web UI end to end: every page renders, every action does what it says.

Uses FastAPI's test client with a fake Gmail and a scripted Jev judge.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from jev_gmail_filter import onboarding
from jev_gmail_filter.config import Settings
from jev_gmail_filter.db import Store
from jev_gmail_filter.web.app import create_app

from .fakes import FakeMail, email, judge

HEADERS = {"host": "localhost:8501"}


@pytest.fixture
def inbox():
    return FakeMail(
        [
            email(
                "a",
                "[jobs] Thanks for applying to Acme",
                "cat=applied Acme",
                days_ago=3,
                thread="ta",
            ),
            email("b", "[unsure] Acme?", "Acme", days_ago=2),
            email("c", "Lunch", "hi", sender="Pat <pat@example.com>", days_ago=1),
            email(
                "d",
                "[receipt] Order shipped",
                "cat=shipping",
                days_ago=0.5,
                sender="Shop <o@shop.example>",
            ),
        ]
    )


def make(tmp_path, monkeypatch, inbox, *, done=True):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    settings = Settings(tmp_path / "data")
    settings.ensure()
    if done:
        onboarding.save_api_key(settings, "k")
        onboarding.install_client(settings, json.dumps({"installed": {"project_id": "p"}}).encode())
        settings.token_path.write_text("{}")
        onboarding.install_topics(settings, [e.path for e in onboarding.examples()])
        onboarding.finish(settings, labels_on=True, backscan_days=14)
        with Store(settings.db_path) as s:
            s.set_meta("account", "me@example.com")

    def sign_in(s):
        s.token_path.write_text("{}")
        return inbox

    app = create_app(
        settings, connect=lambda s: inbox, sign_in=sign_in, judge=judge(), auto_sync=False
    )
    client = TestClient(app, headers=HEADERS)
    return client, app.state.runtime, settings


def synced(tmp_path, monkeypatch, inbox):
    client, rt, settings = make(tmp_path, monkeypatch, inbox)
    client.post("/sync")
    rt.wait()
    return client, rt, settings


# -- security -------------------------------------------------------------------------------


def test_only_localhost_and_same_origin(tmp_path, monkeypatch, inbox):
    client, _, _ = make(tmp_path, monkeypatch, inbox)
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    r = client.post("/sync", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert (
        client.post(
            "/sync", headers={"origin": "http://localhost:8501"}, follow_redirects=False
        ).status_code
        == 303
    )


# -- setup --------------------------------------------------------------------------------


def test_setup_wizard(tmp_path, monkeypatch, inbox):
    client, rt, settings = make(tmp_path, monkeypatch, inbox, done=False)
    r = client.get("/")
    assert r.url.path == "/setup" and "Your TypeSafe API key" in r.text

    r = client.post("/setup/key", data={"key": ""})
    assert "paste your TypeSafe API key" in r.text
    r = client.post("/setup/key", data={"key": "tsk-1"})
    assert "Create your own Google client" in r.text and "Desktop client" in r.text
    assert (tmp_path / ".env").read_text() == "TYPESAFE_API_KEY=tsk-1\n"

    bad = client.post("/setup/client", files={"client": ("c.json", b'{"web": {}}')})
    assert "Desktop app" in bad.text
    good = json.dumps({"installed": {"project_id": "p"}}).encode()
    r = client.post("/setup/client", files={"client": ("c.json", good)})
    assert "Sign in with Google" in r.text

    client.post("/setup/signin")
    for _ in range(200):
        if rt.signin.state != "waiting":
            break
        __import__("time").sleep(0.01)
    r = client.get("/setup")
    assert "Pick your topics" in r.text and "me@example.com" in r.text

    r = client.post("/setup/topics", data={"ex-Jobs": "on", "ex-Receipts": "on"})
    assert "First scan" in r.text
    r = client.get("/setup", params={"days": 14})
    assert "Emails in that window" in r.text and ">4<" in r.text.replace(" ", "")

    r = client.post("/setup/start", data={"days": 14, "high": 0.01, "dry": "on"})
    rt.wait()
    assert r.url.path == "/"
    r = client.get("/")
    assert "First scan done" in r.text and "labels off (dry run)" in r.text
    assert inbox.labels == {}


def test_setup_skip_scan(tmp_path, monkeypatch, inbox):
    client, rt, settings = make(tmp_path, monkeypatch, inbox, done=False)
    onboarding.save_api_key(settings, "k")
    onboarding.install_client(settings, json.dumps({"installed": {}}).encode())
    settings.token_path.write_text("{}")
    onboarding.install_topics(settings, [e.path for e in onboarding.examples()])
    r = client.post("/setup/start", data={"days": 7, "skip": "1"})
    assert r.url.path == "/" and rt.job is None


# -- pages ------------------------------------------------------------------------------------


def test_overview(tmp_path, monkeypatch, inbox):
    client, _, _ = synced(tmp_path, monkeypatch, inbox)
    r = client.get("/")
    assert r.status_code == 200
    for text in [
        "Good ",
        "Needs you",
        "New this week",
        "Tracked items",
        "Gone quiet",
        "Latest matches",
        "Jobs",
        "Receipts",
        "Sync done",
        "Moved this week",
    ]:
        assert text in r.text, text
    assert "Thanks for applying to Acme" in r.text


def test_needs_you_and_answering(tmp_path, monkeypatch, inbox):
    client, _, _ = synced(tmp_path, monkeypatch, inbox)
    r = client.get("/needs")
    assert "[unsure] Acme?" in r.text and "Is this Jobs?" in r.text
    review_id = next(rv.id for rv in Store(tmp_path / "data" / "jev-gmail-filter.sqlite").reviews())
    r = client.post(
        f"/reviews/{review_id}",
        data={"decision": "yes"},
        headers={"referer": "http://localhost:8501/needs"},
    )
    assert "added to topic" in r.text and "Nothing needs you" in r.text
    assert inbox.labels["b"] == ["Jobs", "Jobs/applied"]


def test_everything_filters_search_and_correction(tmp_path, monkeypatch, inbox):
    client, _, settings = synced(tmp_path, monkeypatch, inbox)
    r = client.get("/emails")
    assert all(s in r.text for s in ["Thanks for applying", "Lunch", "Order shipped"])
    assert "not in a topic" in r.text
    r = client.get("/emails", params={"kind": "matched", "topic": "Receipts"})
    assert "Order shipped" in r.text and "Lunch" not in r.text
    r = client.get("/emails", params={"q": "lunch"})
    assert "Lunch" in r.text and "Order shipped" not in r.text
    r = client.get("/emails", params={"selected": "a"})
    assert "Not Jobs? Remove it" in r.text and "Jobs, Jobs/applied" in r.text

    r = client.post(
        "/emails/a/wrong",
        data={"topic": "Jobs"},
        headers={"referer": "http://localhost:8501/emails?selected=a"},
    )
    assert "removed from Jobs" in r.text and "You removed this from Jobs" in r.text
    assert "a" not in inbox.labels
    with Store(settings.db_path) as s:
        assert s.item_emails(1) == [] or all(
            e["subject"] != "[jobs] Thanks for applying to Acme" for e in s.item_emails(1)
        )


def test_items_board_list_and_editing(tmp_path, monkeypatch, inbox):
    client, _, settings = synced(tmp_path, monkeypatch, inbox)
    r = client.get("/items")
    assert "Board" in r.text and "applied" in r.text and "Acme" in r.text
    assert "Add an item by hand" in r.text
    r = client.get("/items", params={"view": "list"})
    assert "Acme" in r.text
    r = client.get("/items", params={"selected": 1})
    assert "item #1" in r.text and "Thanks for applying to Acme" in r.text

    client.post(
        "/items/1/status",
        data={"status": "interviewing"},
        headers={"referer": "http://localhost:8501/items?selected=1"},
    )
    client.post("/items/1/notes", data={"notes": "Ask about on-call"})
    with Store(settings.db_path) as s:
        item = s.item(1)
        assert item.status == "interviewing" and item.notes == "Ask about on-call"
        assert [e["kind"] for e in s.item_events(1)] == ["created", "manual"]

    r = client.post(
        "/items", data={"topic": "Jobs", "field-company": "Globex", "status": "applied"}
    )
    assert "Globex" in r.text and "added by hand" in r.text


def test_topics_list_and_editor_round_trip(tmp_path, monkeypatch, inbox):
    client, _, settings = synced(tmp_path, monkeypatch, inbox)
    r = client.get("/topics")
    assert "Receipts" in r.text and "tracked as items" in r.text
    r = client.get("/topics/Receipts/edit")
    assert 'name="description"' in r.text and "Try it before saving" in r.text
    before = (settings.topics_dir / "receipts.yaml").read_text()

    form = _form_fields(r.text)
    r = client.post("/topics/edit", data={**form, "action": "save"})
    assert "No changes" in r.text and "rescan to judge them again" not in r.text
    assert (settings.topics_dir / "receipts.yaml").read_text() == before

    form["description"] = "Receipts and invoices for things I bought."
    r = client.post("/topics/edit", data={**form, "action": "save"})
    assert "Saved Receipts." in r.text and "rescan to judge them again" in r.text


def test_topic_editor_rows_try_and_problems(tmp_path, monkeypatch, inbox):
    client, _, settings = synced(tmp_path, monkeypatch, inbox)
    r = client.get("/topics/new", params={"start": "Starter: Travel"})
    assert 'value="Travel"' in r.text
    form = _form_fields(r.text)
    r = client.post("/topics/edit", data={**form, "action": "add-cat"})
    assert r.text.count('aria-label="Category name"') == 4
    r = client.post("/topics/edit", data={**form, "action": "del-cat-0"})
    assert r.text.count('aria-label="Category name"') == 2

    r = client.post("/topics/edit", data={**form, "name": "", "action": "refresh"})
    assert "Give the topic a name." in r.text

    jobs = _form_fields(client.get("/topics/Jobs/edit").text)
    r = client.post(
        "/topics/edit",
        data={
            **jobs,
            "action": "try",
            "try_email": "",
            "try_text": "[jobs] Thanks for applying to Acme\ncat=applied Acme",
        },
    )
    assert "Belongs" in r.text and "applied" in r.text

    r = client.post("/topics/edit", data={**form, "action": "save"})
    assert (settings.topics_dir / "travel.yaml").exists()


def test_topic_delete_and_yaml(tmp_path, monkeypatch, inbox):
    client, _, settings = synced(tmp_path, monkeypatch, inbox)
    form = _form_fields(client.get("/topics/Receipts/edit").text)
    yaml_text = form["yaml"].replace("Receipts, invoices", "Invoices")
    r = client.post("/topics/edit", data={**form, "yaml": yaml_text, "action": "apply-yaml"})
    assert "Invoices and order" in r.text
    r = client.post("/topics/edit", data={**form, "yaml": "name: [", "action": "apply-yaml"})
    assert "YAML not applied" in r.text
    r = client.post("/topics/Receipts/delete")
    assert "Deleted Receipts" in r.text and not (settings.topics_dir / "receipts.yaml").exists()


def test_settings(tmp_path, monkeypatch, inbox):
    client, rt, settings = make(tmp_path, monkeypatch, inbox)
    with Store(settings.db_path) as s:
        s.set_meta("labels", "off")
    client.post("/sync")
    rt.wait()
    assert inbox.labels == {}
    r = client.get("/settings")
    assert "Write Gmail labels" in r.text and "me@example.com" in r.text
    assert "Also label emails already matched" in r.text
    r = client.post(
        "/settings",
        data={"labels": "on", "label_past": "on", "max_usd": "0.5", "auto_minutes": "10"},
    )
    assert "labelling emails matched so far" in r.text
    rt.wait()
    assert len(inbox.labels) == 2
    r = client.get("/settings")
    assert "Labelling done" in r.text and "Labelled 2 emails" in r.text
    assert "Also label emails already matched" not in r.text
    with Store(settings.db_path) as s:
        assert (s.get_meta("max_usd"), s.get_meta("auto_sync_minutes")) == ("0.5", "10")
    r = client.post("/settings/categories", data={"cat-primary": "on", "cat-updates": "on"})
    assert "Rescan to include older Updates mail" in r.text
    r = client.get("/settings", params={"counts": 1})
    assert r.status_code == 200
    r = client.post("/signout")
    assert r.url.path == "/setup" and not settings.token_path.exists()


def test_scan_status_and_dismiss(tmp_path, monkeypatch, inbox):
    client, rt, _ = synced(tmp_path, monkeypatch, inbox)
    assert client.get("/scan/status").json()["finished"] is True
    r = client.get("/")
    assert "Sync done" in r.text
    client.post("/scan/dismiss")
    assert "Sync done" not in client.get("/").text


def test_auto_sync_tick(tmp_path, monkeypatch, inbox):
    _, rt, settings = make(tmp_path, monkeypatch, inbox)
    assert rt.tick() is False  # off by default
    with Store(settings.db_path) as s:
        s.set_meta("auto_sync_minutes", "5")
    assert rt.tick() is True
    rt.wait()
    assert rt.tick() is False  # just synced


def _form_fields(html: str) -> dict:
    """Pull the editor's current field values out of the page, like a browser would."""
    import re
    from html import unescape

    data = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', html):
        tag, name = m.group(0), m.group(1)
        if 'type="checkbox"' in tag:
            if "checked" in tag:
                value = re.search(r'value="([^"]*)"', tag)
                if name == "match_on":
                    data.setdefault("match_on", []).append(value.group(1))
                else:
                    data[name] = value.group(1) if value else "on"
            continue
        if 'type="file"' in tag or 'type="password"' in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        data[name] = unescape(value.group(1)) if value else ""
    for m in re.finditer(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
        data[m.group(1)] = unescape(m.group(2))
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        chosen = re.search(r"<option([^>]*)selected[^>]*>(.*?)</option>", m.group(2))
        if chosen:
            value = re.search(r'value="([^"]*)"', chosen.group(1))
            data[m.group(1)] = unescape(value.group(1) if value else chosen.group(2))
    data.pop("action", None)
    return data


def test_every_link_on_every_page_works(tmp_path, monkeypatch, inbox):
    """Crawl the app like a person clicking around: no errors, no leaked Python."""
    import re

    client, _, _ = synced(tmp_path, monkeypatch, inbox)
    seen, queue = set(), ["/", "/needs", "/emails", "/items", "/topics", "/settings"]
    while queue and len(seen) < 150:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:200])
        for leak in ("built-in method", "bound method", " object at 0x", "Undefined"):
            assert leak not in r.text, (url, leak)
        for href in re.findall(r'href="(/[^"#]*)"', r.text):
            href = href.replace("&amp;", "&")
            if not href.startswith("/static") and href not in seen:
                queue.append(href)
    assert any("view=list" in u for u in seen) and any("selected=" in u for u in seen)


def test_empty_query_values_are_fine(tmp_path, monkeypatch, inbox):
    client, _, _ = synced(tmp_path, monkeypatch, inbox)
    for url in [
        "/items?selected=&view=list&closed=",
        "/needs?selected=",
        "/needs?selected=x",
        "/emails?offset=&selected=",
        "/settings?counts=",
        "/items?selected=abc",
    ]:
        assert client.get(url).status_code == 200, url


def test_nav_shows_item_count(tmp_path, monkeypatch, inbox):
    client, _, _ = synced(tmp_path, monkeypatch, inbox)
    r = client.get("/")
    assert re.search(r"Items</span><span class=\"meta\">\d+</span>", r.text)


def test_gmail_links_open_the_thread_in_the_connected_account():
    from jev_gmail_filter.web.views import gmail_link

    assert gmail_link("abc", "sam@example.com") == (
        "https://mail.google.com/mail/?authuser=sam%40example.com#all/abc"
    )
    assert gmail_link("abc") == "https://mail.google.com/mail/u/0/#all/abc"
    assert gmail_link(None, "sam@example.com") == "#"


def test_labels_on_without_past_leaves_old_mail_alone(tmp_path, monkeypatch, inbox):
    client, rt, settings = make(tmp_path, monkeypatch, inbox)
    with Store(settings.db_path) as s:
        s.set_meta("labels", "off")
    client.post("/sync")
    rt.wait()
    r = client.post("/settings", data={"labels": "on", "max_usd": "1", "auto_minutes": "0"})
    assert "Settings saved" in r.text and inbox.labels == {}
    with Store(settings.db_path) as s:
        assert s.get_meta("labels") == "on"
