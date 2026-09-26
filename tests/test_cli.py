import json
from types import SimpleNamespace

import jevfilter as jf
import pytest
from jevfilter import defaults

from jev_gmail_filter import cli
from jev_gmail_filter.db import Store

from .fakes import FakeMail, email, judge


@pytest.fixture(autouse=True)
def fake_jev():
    fake = judge()
    jf.configure(judge=fake)
    yield fake
    defaults.reset()


@pytest.fixture
def inbox():
    return FakeMail(
        [
            email("a", "[jobs] Thanks for applying to Acme", "cat=applied Acme", days_ago=3),
            email("b", "[unsure] Acme?", "Acme", days_ago=2),
            email("c", "Lunch", "hi", sender="Pat <pat@example.com>", days_ago=1),
        ]
    )


def answers(*values):
    it = iter(values)
    return lambda prompt: next(it)


def client_file(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}))
    return path


def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return cli.load_settings(tmp_path / "data")


def test_init_walks_through_everything(tmp_path, monkeypatch, capsys, inbox):
    settings = fresh(tmp_path, monkeypatch)
    ask = answers("tsk-key", str(client_file(tmp_path)), "", "3", "", "")
    code = cli.cmd_init(SimpleNamespace(), settings, ask=ask, connect=lambda s: inbox)
    out = capsys.readouterr().out
    assert code == 0
    assert "Step 1/5" in out and "console.cloud.google.com" in out and "Desktop app" in out
    assert "In production" in out and "signed in as me@example.com" in out
    assert "3 emails in your Primary inbox" in out and "estimated Jev cost" in out
    assert "labels off (dry run)" in out and "`jev-gmail-filter labels` turns labels on" in out
    assert (tmp_path / ".env").read_text() == "TYPESAFE_API_KEY=tsk-key\n"
    assert oct((tmp_path / ".env").stat().st_mode)[-3:] == "600"
    assert settings.credentials_path.exists()
    assert sorted(p.name for p in settings.topics_dir.iterdir()) == ["jobs.yaml", "receipts.yaml"]
    store = Store(settings.db_path)
    assert store.get_meta("labels") == "off" and store.email_count() == 3
    assert inbox.labels == {}


def test_init_rejects_a_web_client(tmp_path, monkeypatch, inbox):
    settings = fresh(tmp_path, monkeypatch)
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {}}))
    with pytest.raises(cli.SetupError, match="Desktop app"):
        cli.cmd_init(
            SimpleNamespace(), settings, ask=answers("k", str(web)), connect=lambda s: inbox
        )


def test_init_picks_some_topics_and_skips_scan(tmp_path, monkeypatch, capsys, inbox):
    settings = fresh(tmp_path, monkeypatch)
    monkeypatch.setenv("TYPESAFE_API_KEY", "already")
    ask = answers(str(client_file(tmp_path)), "2", "1", "n", "n")
    cli.cmd_init(SimpleNamespace(), settings, ask=ask, connect=lambda s: inbox)
    assert [p.name for p in settings.topics_dir.iterdir()] == ["receipts.yaml"]
    store = Store(settings.db_path)
    assert store.get_meta("labels") == "on" and store.email_count() == 0
    assert "found TYPESAFE_API_KEY" in capsys.readouterr().out


def test_init_bad_choices(tmp_path, monkeypatch, inbox):
    settings = fresh(tmp_path, monkeypatch)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    with pytest.raises(ValueError, match="choose numbers"):
        cli.cmd_init(
            SimpleNamespace(),
            settings,
            ask=answers(str(client_file(tmp_path)), "9"),
            connect=lambda s: inbox,
        )
    with pytest.raises(ValueError, match="choose 1 to 5"):
        cli.cmd_init(SimpleNamespace(), settings, ask=answers("", "7"), connect=lambda s: inbox)


def setup_done(settings, inbox, labels="on"):
    store = Store(settings.db_path)
    store.set_meta("labels", labels)
    store.close()
    return lambda s: inbox


def args(**kw):
    base = dict(since_days=None, limit=None, max_usd=1.0, dry_run=False)
    return SimpleNamespace(**{**base, **kw})


def test_sync_review_items_status(settings, inbox, capsys):
    connect = setup_done(settings, inbox)
    assert cli.cmd_sync(args(), settings, connect=connect) == 0
    out = capsys.readouterr().out
    assert "judged 3 new email(s)" in out and "matched: Jobs 1" in out and "1 to review" in out
    assert inbox.labels == {"a": ["Jobs", "Jobs/applied"]}

    cli.cmd_review(SimpleNamespace(id=None, decision=None), settings)
    out = capsys.readouterr().out
    assert "#1  [Jobs] [unsure] Acme?" in out and "review 1 yes|no" in out

    cli.cmd_review(SimpleNamespace(id=1, decision="yes"), settings, connect=connect)
    assert "#1: added to topic" in capsys.readouterr().out

    cli.cmd_items(SimpleNamespace(topic=None, stale=False), settings)
    out = capsys.readouterr().out
    assert "Jobs: 2 item(s)" in out and "Acme" in out  # fake judge: untagged means "new"

    cli.cmd_status(SimpleNamespace(), settings)
    out = capsys.readouterr().out
    assert "emails judged: 3" in out and "to review:     0" in out and "labels:        on" in out


def test_dry_run_then_labels(settings, inbox, capsys):
    connect = setup_done(settings, inbox, labels="on")
    cli.cmd_sync(args(dry_run=True), settings, connect=connect)
    assert inbox.labels == {}
    cli.cmd_labels(SimpleNamespace(off=False), settings, connect=connect)
    assert "labelled 1 email(s)" in capsys.readouterr().out
    assert inbox.labels == {"a": ["Jobs", "Jobs/applied"]}
    cli.cmd_labels(SimpleNamespace(off=True), settings)
    assert Store(settings.db_path).get_meta("labels") == "off"


def test_sync_budget_stop_exit_code(settings, inbox, capsys):
    connect = setup_done(settings, inbox)
    assert cli.cmd_sync(args(max_usd=0), settings, connect=connect) == 1
    assert "stopped early: spend cap" in capsys.readouterr().out


def test_watch_rounds(settings, inbox, capsys):
    connect = setup_done(settings, inbox)
    slept = []
    cli.cmd_watch(
        SimpleNamespace(interval=5, max_usd=1.0),
        settings,
        connect=connect,
        sleep=slept.append,
        rounds=2,
    )
    out = capsys.readouterr().out
    assert "watching every 5s" in out and out.count("judged 3") == 1 and slept == [5]


def test_commands_before_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["--data-dir", str(tmp_path / "nothing"), "sync"])
    assert code == 2 and "run `jev-gmail-filter init`" in capsys.readouterr().err
    cli.main(["--data-dir", str(tmp_path / "nothing"), "status"])
    assert "not set up yet" in capsys.readouterr().out


def test_review_needs_a_decision(settings, inbox, capsys):
    setup_done(settings, inbox)
    code = cli.main(["--data-dir", str(settings.data_dir), "review", "1"])
    assert code == 2 and "give a decision" in capsys.readouterr().err


def test_nothing_to_review(settings, inbox, capsys):
    setup_done(settings, inbox)
    cli.cmd_review(SimpleNamespace(id=None, decision=None), settings)
    assert "nothing to review" in capsys.readouterr().out
