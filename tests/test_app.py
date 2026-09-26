"""Drive the Streamlit UI headlessly: the setup wizard, then every page."""

import json
from pathlib import Path

import jevfilter as jf
import pytest
from jevfilter import defaults
from streamlit.testing.v1 import AppTest

from jev_gmail_filter import onboarding
from jev_gmail_filter.config import load_settings
from jev_gmail_filter.db import Store

from .fakes import FakeMail, email, judge

APP = str(Path(__file__).parents[1] / "src" / "jev_gmail_filter" / "app.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("JGF_DATA_DIR", str(tmp_path / "data"))
    inbox = FakeMail(
        [
            email("a", "[jobs] Thanks for applying to Acme", "cat=applied Acme", days_ago=3),
            email("b", "[unsure] Acme?", "Acme", days_ago=2),
            email("c", "Lunch", "hi", sender="Pat <pat@example.com>", days_ago=1),
        ]
    )

    def sign_in(s):
        s.token_path.write_text("{}")  # as the real browser sign-in does
        return inbox

    monkeypatch.setattr(onboarding, "sign_in", sign_in)
    monkeypatch.setattr(onboarding, "connect", lambda s: inbox)
    jf.configure(judge=judge())
    yield inbox
    defaults.reset()


def app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    return at.run()


def wait_for_scan(at: AppTest, timeout: float = 20.0) -> AppTest:
    """Scans run on a background thread; rerun the page until the result shows."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        at.run()
        if any(b.label == "Dismiss" for b in at.button):
            return at
        time.sleep(0.1)
    raise AssertionError("scan didn't finish")


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [e.label for e in at.expander]
    parts += [x.value for x in [*at.success, *at.info, *at.warning, *at.error, *at.caption]]
    return "\n".join(str(p) for p in parts)


def client_bytes() -> bytes:
    return json.dumps({"installed": {"client_id": "id", "project_id": "my-proj"}}).encode()


def finish_setup(tmp_path, inbox):
    s = load_settings()
    onboarding.save_api_key(s, "k")
    onboarding.install_client(s, client_bytes())
    s.token_path.write_text("{}")
    onboarding.install_topics(s, [ex.path for ex in onboarding.examples()])
    onboarding.finish(s, labels_on=True, backscan_days=14)
    with Store(s.db_path) as st:
        st.set_meta("account", "me@example.com")
    return s


def test_setup_wizard_end_to_end(env, tmp_path):
    at = app()
    assert at.title[0].value.startswith("📬 Set up")
    assert "👉 1. TypeSafe API key" in texts(at)

    at.text_input[0].set_value("tsk-123").run()
    button(at, "Save key").click().run()
    assert (tmp_path / ".env").read_text() == "TYPESAFE_API_KEY=tsk-123\n"
    assert "👉 2. Your Google OAuth client" in texts(at)
    assert "Desktop app" in texts(at) and "every 7 days" in texts(at)

    # AppTest can't drive a file uploader; install the client as the upload would.
    onboarding.install_client(load_settings(), client_bytes())
    at.run()
    assert "my-proj" in texts(at) and "👉 3. Sign in to Gmail" in texts(at)

    button(at, "Sign in with Google").click().run()
    assert "me@example.com" in texts(at) and "👉 4. Pick your topics" in texts(at)

    button(at, "Use these topics").click().run()
    assert "👉 5. First scan" in texts(at)

    button(at, "Check how much mail that is").click().run()
    assert [m.value for m in at.metric][0] == "3"

    button(at, "Start scan").click().run()
    wait_for_scan(at)
    assert at.title[0].value == "Overview"
    assert "First scan done" in [h.value for h in at.subheader][0]
    assert "Judged **3**" in texts(at) and "Labels are off (dry run)" in texts(at)
    assert env.labels == {}


def test_bad_client_file_is_explained(env, tmp_path):
    s = load_settings()
    s.ensure()
    with pytest.raises(onboarding.SetupError, match="Desktop app"):
        onboarding.install_client(s, json.dumps({"web": {}}).encode())
    assert not s.credentials_path.exists()


def test_pages_after_setup(env, tmp_path):
    finish_setup(tmp_path, env)
    at = app()
    assert at.title[0].value == "Overview"
    button(at, "Sync now").click().run()
    wait_for_scan(at)
    assert "Judged **3**" in texts(at)
    assert env.labels == {"a": ["Jobs", "Jobs/applied"]}
    button(at, "Dismiss").click().run()
    assert "Judged **3**" not in texts(at)

    at.sidebar.radio[0].set_value("Review").run()
    assert "Does this belong to **Jobs**?" in texts(at)
    button(at, "Yes").click().run()
    assert "Nothing to review." in texts(at)

    at.sidebar.radio[0].set_value("Items").run()
    assert any("Acme" in m.value for m in at.markdown)

    at.sidebar.radio[0].set_value("Emails").run()
    assert at.dataframe[0].value.shape[0] == 3

    at.sidebar.radio[0].set_value("Topics").run()
    assert {e.label for e in at.expander} >= {"Jobs", "Receipts", "➕ New topic"}

    at.sidebar.radio[0].set_value("Settings").run()
    assert "me@example.com" in texts(at) and "my-proj" in texts(at)


def test_create_topic_validates(env, tmp_path):
    s = finish_setup(tmp_path, env)
    at = app()
    at.sidebar.radio[0].set_value("Topics").run()
    form_inputs = [t for t in at.text_input if t.label == "Name"]
    form_inputs[0].set_value("Travel")
    [a for a in at.text_area if a.label.startswith("What belongs")][0].set_value(
        "Flight and hotel bookings."
    )
    [a for a in at.text_area if a.label.startswith("Categories")][0].set_value(
        "booking: Confirms a booking.\nchange"
    )
    next(b for b in at.button if b.label == "Create").click().run()
    topic = jf.Topic.load(s.topics_dir)["Travel"]
    assert topic.categories["booking"].description == "Confirms a booking."
    assert topic.categories["change"].description == "change"


def test_labels_toggle_labels_past_matches(env, tmp_path):
    s = finish_setup(tmp_path, env)
    with Store(s.db_path) as st:
        st.set_meta("labels", "off")
    at = app()
    button(at, "Sync now").click().run()
    wait_for_scan(at)
    assert env.labels == {}
    at.sidebar.radio[0].set_value("Settings").run()
    at.toggle[0].set_value(True).run()
    assert env.labels == {"a": ["Jobs", "Jobs/applied"]}


def test_scan_survives_a_refresh(env, tmp_path):
    finish_setup(tmp_path, env)
    first = app()
    button(first, "Sync now").click().run()
    refreshed = wait_for_scan(app())  # a new session, like reloading the page
    assert "Judged **3**" in texts(refreshed)


def test_recheck_button(env, tmp_path, monkeypatch):
    finish_setup(tmp_path, env)
    at = app()
    button(at, "Sync now").click().run()
    wait_for_scan(at)
    at.sidebar.radio[0].set_value("Review").run()
    button(at, "Re-check all 1 with the latest topics").click().run()
    wait_for_scan(at)
    assert "Judged **1**" in texts(at)
