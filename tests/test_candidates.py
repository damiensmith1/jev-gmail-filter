import pytest

from jev_gmail_filter.candidates import MAX_PER_FIELD, extract

from .fakes import email


@pytest.mark.parametrize(
    "sender, subject, body, orgs",
    [
        (
            "Acme Robotics Careers <no-reply@us.greenhouse-mail.io>",
            "Thank you for applying to Acme Robotics",
            "Thanks for applying for the Backend Engineer role at Acme Robotics.",
            ["Acme Robotics"],
        ),
        (
            "Sam Lee <sam@initech.example>",
            "Interview",
            "A phone screen for the role at Initech.",
            ["Initech"],
        ),
        (
            "Workday <globex@myworkday.com>",
            "Your application",
            "It was sent to Globex Corporation.",
            ["Globex", "Globex Corporation"],
        ),
        (
            "Shop <orders@shop.example>",
            "Order shipped",
            "Thanks for your purchase from Northwind Traders.",
            ["Shop", "Northwind Traders"],
        ),
    ],
)
def test_orgs(sender, subject, body, orgs):
    found = extract(email("1", subject, body, sender=sender), "org")
    for o in orgs:
        assert o in found
    assert "Sam Lee" not in found and "Workday" not in found


def test_platform_and_alert_senders_are_not_orgs():
    found = extract(
        email(
            "1",
            "25 new jobs",
            "Software Engineer at Globex, Analyst at Hooli.",
            sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        ),
        "org",
    )
    assert found == ["Globex", "Hooli"]


def test_titles():
    found = extract(
        email(
            "1",
            "Interview for Data Engineer",
            "the Senior Data Engineer role and a Software Developer II opening",
        ),
        "title",
    )
    assert {"Data Engineer", "Senior Data Engineer", "Software Developer II"} <= set(found)


def test_known_values_only_when_mentioned():
    found = extract(email("1", "Update", "news from acme"), "org", known=["Acme", None, "Initech"])
    assert found[0] == "Acme" and found.count("Acme") == 1 and "Initech" not in found


def test_many_known_values_never_crowd_out_the_emails_own():
    known = [f"Company{i}" for i in range(40)]
    e = email(
        "1",
        "Damien, your application was sent to StackAdapt",
        "",
        sender="LinkedIn <jobs-noreply@linkedin.com>",
    )
    assert extract(e, "org", known=known) == ["StackAdapt"]


def test_email_kind_and_unknown_kind():
    e = email("1", "Contact", "write to sam@initech.example", sender="x <x@a.example>")
    assert extract(e, "email") == ["x@a.example", "sam@initech.example"]
    assert extract(e, "order_number", known=["#123"]) == []  # not mentioned
    assert extract(email("2", "Order #123", "x"), "order_number", known=["#123"]) == ["#123"]
    assert extract(e, None) == []


def test_candidates_are_capped():
    body = " ".join(f"at Company{i}" for i in range(40))
    assert len(extract(email("1", "x", body, sender="y <y@example.com>"), "org")) == MAX_PER_FIELD
