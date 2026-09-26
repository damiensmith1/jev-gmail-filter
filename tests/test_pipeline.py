from datetime import UTC, datetime, timedelta

import jevfilter as jf
import pytest

from jev_gmail_filter.pipeline import Pipeline, label_for

from .fakes import NOW, FakeMail, email, judge


def test_first_sync_judges_labels_and_creates_items(pipeline, mail, store):
    mail.emails = {
        e.id: e
        for e in [
            email("a", "[jobs] Thanks for applying to Acme", "cat=applied Acme", days_ago=3),
            email("b", "Lunch?", "want to grab lunch", sender="Pat <pat@example.com>", days_ago=2),
            email("c", "[receipt] Order shipped", "Your order", sender="Shop <o@shop.example>"),
        ]
    }
    r = pipeline.sync()
    assert (r.scanned, r.judged, r.reviews) == (3, 3, 0)
    assert r.matched == {"Jobs": 1, "Receipts": 1}
    assert mail.labels == {"a": ["Jobs", "Jobs/applied"], "c": ["Receipts", "Receipts/order"]}
    [job] = store.items("Jobs")
    assert job.fields == {"company": "Acme"} and job.status == "applied"
    assert store.get_meta("history_id") == "100"


def test_oldest_first_so_status_moves_forward(pipeline, mail, store):
    for e in [
        email(
            "new", "[jobs] Interview at Acme", "cat=interview Acme item=1", thread="x", days_ago=1
        ),
        email(
            "old", "[jobs] Thanks for applying to Acme", "cat=applied Acme", thread="y", days_ago=5
        ),
    ]:
        mail.emails[e.id] = e
    pipeline.sync()
    [job] = store.items("Jobs")
    assert job.status == "interviewing"


def test_incremental_sync_uses_history(pipeline, mail, fake_judge):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme")
    pipeline.sync()
    calls = len(fake_judge.calls)
    mail.deliver(
        email("b", "[receipt] Receipt", "paid", sender="Shop <o@shop.example>", days_ago=0)
    )
    r = pipeline.sync()
    assert r.scanned == 1 and r.matched == {"Receipts": 1}
    assert mail.fetched[-1] == "b" and len(fake_judge.calls) > calls


def test_already_judged_email_is_skipped(pipeline, mail, fake_judge):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme")
    pipeline.sync()
    calls = len(fake_judge.calls)
    r = pipeline.sync(since=NOW - timedelta(days=7))
    assert r.skipped == 1 and r.judged == 0 and len(fake_judge.calls) == calls


def test_editing_a_topic_rejudges_only_that_topic(store, mail, topics, fake_judge):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme")
    Pipeline(store, mail, topics, judge=fake_judge).sync()
    d = topics["Receipts"].to_dict()
    d["description"] = "Receipts and invoices."
    edited = jf.Topic.load([topics["Jobs"], jf.Topic.from_dict(d)])
    before = len(fake_judge.calls)
    r = Pipeline(store, mail, edited, judge=fake_judge).sync(since=NOW - timedelta(days=7))
    assert r.judged == 1
    asked = {q for _, qs in fake_judge.calls[before:] for q in qs}
    assert asked == {"Receipts/membership"}


def test_same_thread_joins_the_same_item_without_matching(pipeline, mail, store, fake_judge):
    mail.emails["a"] = email(
        "a", "[jobs] Applied to Acme", "cat=applied Acme", thread="t", days_ago=2
    )
    mail.emails["b"] = email(
        "b", "[jobs] Re: Applied", "cat=interview Acme", thread="t", days_ago=1
    )
    pipeline.sync()
    [job] = store.items("Jobs")
    assert job.status == "interviewing"
    assert not any("Jobs/item" in qs for _, qs in fake_judge.calls)


def test_match_item_links_to_existing_or_creates_new(pipeline, mail, store):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme", days_ago=3)
    pipeline.sync()
    mail.deliver(email("b", "[jobs] Acme interview", "cat=interview Acme item=1", days_ago=1))
    mail.deliver(email("c", "[jobs] Acme other role", "cat=recruiter Acme item=new", days_ago=0))
    pipeline.sync()
    items = store.items("Jobs")
    assert [(i.id, i.status) for i in items] == [(1, "interviewing"), (2, "contacted")]


def test_uncertain_membership_goes_to_review_and_can_be_accepted(pipeline, mail, store):
    mail.emails["a"] = email("a", "[unsure] Acme follow up", "cat=applied Acme")
    r = pipeline.sync()
    assert r.reviews == 1 and "a" not in mail.labels and store.items("Jobs") == []
    [review] = store.reviews()
    assert review.kind == "topic" and review.reasons == ["membership_uncertain"]
    assert pipeline.resolve(review.id, "yes") == "added to topic"
    assert mail.labels["a"] == ["Jobs", "Jobs/applied"]
    assert len(store.items("Jobs")) == 1 and store.reviews() == []


def test_rejecting_a_review(pipeline, mail, store):
    mail.emails["a"] = email("a", "[unsure] Maybe", "cat=applied Acme")
    pipeline.sync()
    [review] = store.reviews()
    assert pipeline.resolve(review.id, "no") == "not in topic"
    assert "a" not in mail.labels and store.items("Jobs") == []


def test_uncertain_item_match_goes_to_review(store, mail, topics):
    fake = judge()
    fake.script["Jobs/item"] = {"item 1": 0.45, "new": 0.4}
    p = Pipeline(store, mail, topics, judge=fake)
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme", days_ago=2)
    p.sync()
    mail.deliver(email("b", "[jobs] Acme again", "cat=interview Acme", days_ago=1))
    r = p.sync()
    assert r.reviews == 1 and len(store.items("Jobs")) == 1
    [review] = store.reviews()
    assert review.kind == "item"
    assert p.resolve(review.id, "1") == "linked to item #1"
    assert store.items("Jobs")[0].status == "interviewing"


def test_review_decisions_are_validated(pipeline, mail, store):
    mail.emails["a"] = email("a", "[unsure] ?", "Acme")
    pipeline.sync()
    [review] = store.reviews()
    with pytest.raises(ValueError, match="yes' or 'no"):
        pipeline.resolve(review.id, "maybe")
    with pytest.raises(ValueError, match="no review"):
        pipeline.resolve(999, "yes")


def test_dry_run_writes_no_labels_then_labels_apply(store, mail, topics, fake_judge):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme")
    dry = Pipeline(store, mail, topics, judge=fake_judge, write_labels=False)
    dry.sync()
    assert mail.labels == {}
    live = Pipeline(store, mail, topics, judge=fake_judge)
    assert live.apply_labels_to_matches() == 1
    assert mail.labels == {"a": ["Jobs", "Jobs/applied"]}


def test_budget_stop_keeps_position(store, mail, topics, fake_judge):
    for i in range(3):
        mail.emails[str(i)] = email(str(i), f"[jobs] Acme {i}", "cat=applied Acme", days_ago=3 - i)
    p = Pipeline(store, mail, topics, judge=fake_judge, budget=jf.Budget(usd=0))
    r = p.sync()
    assert r.stopped and "spend cap" in r.stopped and r.judged == 0
    assert store.get_meta("history_id") is None  # next run starts over, nothing lost
    r = Pipeline(store, mail, topics, judge=fake_judge).sync()
    assert r.judged == 3


def test_limit_does_not_advance_position(pipeline, mail, store):
    for i in range(3):
        mail.emails[str(i)] = email(str(i), f"hello {i}", "x", days_ago=3 - i)
    r = pipeline.sync(limit=2)
    assert r.scanned == 2 and store.get_meta("history_id") is None


def test_expired_history_falls_back_to_a_date_scan(pipeline, mail):
    # The fallback rescans from a day before the last sync, so "a" is seen again (and skipped).
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme", days_ago=0.5)
    pipeline.sync()
    mail.expired = True
    mail.emails["b"] = email("b", "[receipt] Paid", "x", sender="S <s@shop.example>", days_ago=0)
    r = pipeline.sync()
    assert r.judged == 1 and r.skipped == 1


def test_stale_items(pipeline, mail, store):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme", days_ago=30)
    mail.emails["b"] = email(
        "b",
        "[jobs] Initech rejection",
        "cat=rejection Initech",
        days_ago=40,
        sender="Initech <hr@initech.example>",
    )
    pipeline.sync(since=NOW - timedelta(days=60))
    stale = {i.fields["company"]: i.stale for i in store.items("Jobs")}
    assert stale == {"Acme": True, "Initech": False}  # rejected items never go stale
    assert pipeline.refresh_stale(NOW - timedelta(days=20)) == 0


def test_label_errors_are_reported_not_fatal(store, topics, fake_judge):
    class Broken(FakeMail):
        def add_labels(self, message_id, names):
            raise RuntimeError("insufficient permissions")

    mail = Broken([email("a", "[jobs] Applied to Acme", "cat=applied Acme")])
    r = Pipeline(store, mail, topics, judge=fake_judge).sync()
    assert r.judged == 1 and "insufficient permissions" in r.errors[0]


def test_candidates_include_known_item_values(pipeline, mail, fake_judge):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme", days_ago=2)
    pipeline.sync()
    mail.deliver(email("b", "[jobs] Update", "cat=interview item=1", sender="x <x@example.com>"))
    pipeline.sync()
    company_qs = [
        qs["Jobs/fields/company"] for _, qs in fake_judge.calls if "Jobs/fields/company" in qs
    ]
    assert "Acme" in company_qs[-1].criteria


def test_label_for():
    assert label_for(jf.Topic(name="A", description="d")) == "A"
    assert label_for(jf.Topic(name="A", description="d", meta={"gmail_label": "Stuff"})) == "Stuff"
    assert label_for(jf.Topic(name="A", description="d", meta={"gmail_label": False})) is None


def test_date_is_iso_in_store(pipeline, mail, store):
    mail.emails["a"] = email("a", "[jobs] Applied to Acme", "cat=applied Acme")
    pipeline.sync()
    [job] = store.items("Jobs")
    assert datetime.fromisoformat(job.last_email_at).tzinfo == UTC
