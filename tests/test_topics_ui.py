"""The Topics page: cards, the form editor, starters, Try it, save, delete."""

import jevfilter as jf

from jev_gmail_filter.config import load_settings

from .test_app import app, button, env, finish_setup, texts  # noqa: F401  (fixture)


def topics_page(tmp_path, inbox):
    finish_setup(tmp_path, inbox)
    at = app()
    at.sidebar.radio[0].set_value("Topics").run()
    return at


def edit(at, name):
    next(b for b in at.button if b.key == f"edit-{name}").click().run()
    return at


def text_input(at, label):
    return next(t for t in at.text_input if t.label == label)


def text_area(at, label):
    return next(t for t in at.text_area if t.label == label)


def test_cards_show_each_topic(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    assert {"### Jobs", "### Receipts"} <= {m.value for m in at.markdown}
    assert "tracked as items" in texts(at) and "Gmail label: Jobs" in texts(at)


def test_saving_without_changes_keeps_the_topic_and_offers_no_rescan(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    path = load_settings().topics_dir / "jobs.yaml"
    before = path.read_text()
    edit(at, "Jobs")
    assert at.title[0].value == "Edit Jobs"
    assert text_input(at, "Name").value == "Jobs"
    button(at, "Save").click().run()
    assert path.read_text() == jf.Topic.from_dict(jf.Topic.load(path)["Jobs"].to_dict()).to_yaml()
    assert (
        jf.Topic.load(path)["Jobs"].version
        == jf.Topic.load({"topics": [__import__("yaml").safe_load(before)]})["Jobs"].version
    )
    assert "Rescan to judge them again" not in texts(at)


def test_editing_a_description_saves_and_offers_a_rescan(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    edit(at, "Receipts")
    text_area(at, "What belongs").set_value("Receipts and invoices for things I bought.")
    button(at, "Save").click().run()
    saved = jf.Topic.load(load_settings().topics_dir)["Receipts"]
    assert saved.description == "Receipts and invoices for things I bought."
    assert "Saved **Receipts**" in texts(at)
    assert any(b.label == "Rescan" for b in at.button)


def test_new_topic_from_a_starter(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    at.selectbox(key="new-topic-start").set_value("Starter: Travel").run()
    button(at, "Start").click().run()
    assert at.title[0].value == "New topic" and text_input(at, "Name").value == "Travel"
    button(at, "Save").click().run()
    travel = jf.Topic.load(load_settings().topics_dir)["Travel"]
    assert set(travel.categories) == {"booking", "change", "check_in"}
    assert (load_settings().topics_dir / "travel.yaml").exists()


def test_blank_topic_explains_what_is_missing(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    button(at, "Start").click().run()  # Blank is the default
    assert "Give the topic a name." in texts(at)
    assert next(b for b in at.button if b.label == "Save").disabled
    text_input(at, "Name").set_value("Receipts").run()
    text_area(at, "What belongs").set_value("dup").run()
    button(at, "Save").click().run()
    assert "already a topic called 'Receipts'" in texts(at)


def test_try_it_on_pasted_text(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    edit(at, "Jobs")
    at.radio(key=next(r.key for r in at.radio if r.key.endswith("trymode"))).set_value(
        "Paste text"
    ).run()
    text_area(at, "Email text (subject and body)").set_value(
        "[jobs] Thanks for applying to Acme\ncat=applied Acme"
    ).run()
    button(at, "Test").click().run()
    assert "✅ Belongs" in texts(at) and "Category: **applied**" in texts(at)
    assert "company: **Acme**" in texts(at)


def test_delete(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    next(b for b in at.button if b.key == "del-Receipts").click().run()
    assert "Receipts" not in jf.Topic.load(load_settings().topics_dir)


def test_apply_yaml(env, tmp_path):  # noqa: F811
    at = topics_page(tmp_path, env)
    edit(at, "Receipts")
    yaml_box = text_area(at, "YAML")
    yaml_box.set_value(yaml_box.value.replace("Receipts, invoices", "Invoices")).run()
    button(at, "Apply YAML to the form").click().run()
    assert text_area(at, "What belongs").value.startswith("Invoices")
