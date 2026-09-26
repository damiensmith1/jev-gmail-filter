import jevfilter as jf
import pytest

from jev_gmail_filter import topic_form as tf
from jev_gmail_filter.config import EXAMPLE_TOPICS


@pytest.mark.parametrize("name", ["Jobs", "Receipts"])
def test_example_topics_round_trip_exactly(name):
    topic = jf.Topic.load(EXAMPLE_TOPICS)[name]
    form = tf.from_topic(topic)
    assert tf.to_dict(form) == topic.to_dict()
    built, problems = tf.build(form)
    assert problems == [] and built.version == topic.version


def test_jobs_form_fields():
    form = tf.from_topic(jf.Topic.load(EXAMPLE_TOPICS)["Jobs"])
    assert form.gmail_label == "Jobs"
    applied = next(c for c in form.categories if c.name == "applied")
    assert applied.examples and applied.description.startswith("Confirms")
    recruiter = next(c for c in form.categories if c.name == "recruiter")
    assert recruiter.exclude.startswith("Automatic confirmations")
    company = next(f for f in form.fields if f.name == "company")
    assert (company.kind, company.required) == ("org", True)
    assert form.track.match_on == ["company"] and form.track.stale_after_days == 21
    rejected = next(s for s in form.track.statuses if s.name == "rejected")
    assert rejected.closed and rejected.categories == ["rejection"]


def test_build_a_topic_from_scratch():
    form = tf.TopicForm(
        name="Travel",
        description="Flight and hotel bookings.",
        exclude=["Deals", "Newsletters"],
        examples_match=["Your booking is confirmed"],
        categories=[
            tf.CategoryRow("booking", "Confirms a booking.", ["Booking confirmed"]),
            tf.CategoryRow("change", "A change.", [], "Marketing; Surveys"),
            tf.CategoryRow("  ", "blank rows are ignored"),
        ],
        fields=[tf.FieldRow("company", "org", "The airline."), tf.FieldRow("ref")],
        flags=[tf.FlagRow("urgent", "Departs within a day.")],
        track=tf.TrackForm(
            ["company"],
            [
                tf.StatusRow("booked", ["booking"]),
                tf.StatusRow("cancelled", ["change"], closed=True),
            ],
            30,
        ),
        gmail_label="Trips",
        thresholds={"accept": 0.8, "reject": 0.3, "min_confidence": 0.5},
    )
    topic, problems = tf.build(form)
    assert problems == []
    d = topic.to_dict()
    assert d["exclude"] == ["Deals", "Newsletters"]
    assert d["categories"]["booking"] == {
        "description": "Confirms a booking.",
        "examples": ["Booking confirmed"],
    }
    assert d["categories"]["change"]["exclude"] == ["Marketing", "Surveys"]
    assert d["fields"] == {"company": {"kind": "org", "about": "The airline."}, "ref": {}}
    assert d["track"] == {
        "match_on": ["company"],
        "statuses": {"booked": ["booking"]},
        "terminal": {"cancelled": ["change"]},
        "stale_after_days": 30,
    }
    assert d["thresholds"] == {"accept": 0.8} and d["meta"] == {"gmail_label": "Trips"}


def test_plain_language_problems():
    _, problems = tf.build(tf.TopicForm(name="", description=""))
    assert (
        "Give the topic a name." in problems and "Describe what belongs in this topic." in problems
    )
    form = tf.TopicForm(
        name="T",
        description="d",
        categories=[tf.CategoryRow("a", "A")],
        track=tf.TrackForm(statuses=[tf.StatusRow("s", ["b"])]),
    )
    assert tf.build(form)[1] == ["Status 's' is moved by b, which is not a category."]
    form.track.statuses = []
    assert "at least one status" in tf.build(form)[1][0]


def test_no_label_and_advanced_parts_are_kept():
    data = {
        "name": "T",
        "description": "d",
        "scores": {"urgency": {"levels": ["low", "high"]}},
        "when": {"urgency": "always"},
        "meta": {"gmail_label": False, "colour": "red"},
    }
    form = tf.from_dict(data)
    assert form.gmail_label == "" and form.advanced_keys == ["scores", "when"]
    assert tf.to_dict(form) == data


def test_nested_categories_stay_yaml_only():
    data = {
        "name": "S",
        "description": "d",
        "categories": {"hw": {"description": "x", "children": {"a": "A", "b": "B"}}, "sw": "y"},
    }
    form = tf.from_dict(data)
    assert "categories" in form.advanced_keys
    assert tf.to_dict(form) == data


@pytest.mark.parametrize("name", list(tf.STARTERS))
def test_starters_are_valid_topics(name):
    topic, problems = tf.build(tf.from_dict(tf.STARTERS[name]))
    assert problems == [] and topic.name == name
