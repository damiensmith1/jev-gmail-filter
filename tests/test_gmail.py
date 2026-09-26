import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jev_gmail_filter.gmail import (
    GmailSource,
    HistoryExpired,
    SetupError,
    check_client_file,
    primary_query,
)


def test_client_file_checks(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"installed": {"client_id": "x"}}))
    check_client_file(good)
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(SetupError, match="'web' client, not a Desktop app"):
        check_client_file(web)
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(SetupError, match="isn't JSON"):
        check_client_file(bad)
    with pytest.raises(SetupError, match="no OAuth client file"):
        check_client_file(tmp_path / "missing.json")


def test_primary_query():
    assert primary_query() == "in:inbox category:primary"
    when = datetime(2026, 9, 1, tzinfo=UTC)
    assert primary_query(when) == f"in:inbox category:primary after:{int(when.timestamp())}"


class Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        assert num_retries > 0  # every request must retry rate limits
        return self.fn()


class StubService:
    """Just enough of googleapiclient's Gmail service."""

    def __init__(self):
        self.labels_store = {"Jobs": "L1"}
        self.modified = []
        self.history_pages = []
        self.history_error = None

    def users(self):
        return self

    def labels(self):
        svc = self
        return SimpleNamespace(
            list=lambda userId: Call(
                lambda: {"labels": [{"name": n, "id": i} for n, i in svc.labels_store.items()]}
            ),
            create=lambda userId, body: Call(lambda: svc._create(body["name"])),
        )

    def _create(self, name):
        self.labels_store[name] = f"L{len(self.labels_store) + 1}"
        return {"id": self.labels_store[name]}

    def messages(self):
        svc = self
        return SimpleNamespace(
            modify=lambda userId, id, body: Call(lambda: svc.modified.append((id, body))),
            list=lambda userId, q, maxResults, pageToken: Call(
                lambda: (
                    {"messages": [{"id": "a"}], "nextPageToken": "p2"}
                    if pageToken is None
                    else {"messages": [{"id": "b"}]}
                )
            ),
        )

    def history(self):
        svc = self

        def page(**kw):
            if svc.history_error:
                raise svc.history_error
            return svc.history_pages.pop(0)

        return SimpleNamespace(list=lambda **kw: Call(lambda: page(**kw)))


def source(stub):
    from jev_gmail_filter.gmail import QuotaPacer

    s = GmailSource.__new__(GmailSource)
    s._svc = stub
    s._labels = None
    s.pacer = QuotaPacer(sleep=lambda _: None)
    return s


def test_nested_labels_are_created_once():
    stub = StubService()
    s = source(stub)
    s.add_labels("m1", ["Jobs", "Jobs/applied"])
    s.add_labels("m2", ["Jobs/applied", "Receipts/order"])
    assert set(stub.labels_store) == {"Jobs", "Jobs/applied", "Receipts", "Receipts/order"}
    assert stub.modified[0] == ("m1", {"addLabelIds": ["L1", stub.labels_store["Jobs/applied"]]})


def test_search_follows_pages():
    assert list(source(StubService()).search("q")) == ["a", "b"]


def test_new_since_keeps_primary_only_and_follows_pages():
    stub = StubService()
    stub.history_pages = [
        {
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "p1", "labelIds": ["INBOX", "CATEGORY_PERSONAL"]}},
                        {"message": {"id": "promo", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]}},
                    ]
                }
            ],
            "historyId": "150",
            "nextPageToken": "n",
        },
        {
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "p2", "labelIds": ["INBOX", "CATEGORY_PERSONAL"]}},
                        {"message": {"id": "p1", "labelIds": ["INBOX", "CATEGORY_PERSONAL"]}},
                    ]
                }
            ],
            "historyId": "160",
        },
    ]
    assert source(stub).new_since("100") == (["p1", "p2"], "160")


def test_new_since_expired_history():
    from googleapiclient.errors import HttpError

    stub = StubService()
    stub.history_error = HttpError(SimpleNamespace(status=404, reason="Not Found"), b"{}")
    with pytest.raises(HistoryExpired):
        source(stub).new_since("1")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_pacer_waits_instead_of_exceeding_the_minute_budget():
    from jev_gmail_filter.gmail import QuotaPacer

    clock = FakeClock()
    pacer = QuotaPacer(100, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        pacer.take(20)  # 100 units: fits
    assert clock.slept == []
    clock.now = 10
    pacer.take(20)  # would be 120 in the last minute: wait until the first falls out
    assert clock.slept == [50] and pacer.waited == 50


def test_pacer_allows_a_single_oversized_request():
    from jev_gmail_filter.gmail import QuotaPacer

    pacer = QuotaPacer(10, sleep=lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    pacer.take(50)


def test_every_call_is_paced():
    stub = StubService()
    s = source(stub)
    s.add_labels("m1", ["Jobs"])
    list(s.search("q"))
    # labels.list 1 + modify 5 + two search pages 5 each
    assert sum(u for _, u in s.pacer._spent) == 1 + 5 + 10


def test_inbox_query_and_categories():
    from jev_gmail_filter.gmail import inbox_query, parse_categories

    assert inbox_query(("primary",)) == "in:inbox category:primary"
    assert inbox_query(("primary", "updates")) == "in:inbox (category:primary OR category:updates)"
    everything = ("primary", "updates", "promotions", "social", "forums")
    assert inbox_query(everything) == "in:inbox"
    assert parse_categories(None) == ("primary",)
    assert parse_categories("updates, Primary") == ("primary", "updates")
    with pytest.raises(ValueError, match="unknown Gmail categories"):
        parse_categories("spam")


def test_new_since_filters_by_chosen_categories():
    stub = StubService()
    page = {
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "p", "labelIds": ["INBOX", "CATEGORY_PERSONAL"]}},
                    {"message": {"id": "u", "labelIds": ["INBOX", "CATEGORY_UPDATES"]}},
                    {"message": {"id": "n", "labelIds": ["INBOX"]}},
                ]
            }
        ],
        "historyId": "9",
    }
    stub.history_pages = [dict(page), dict(page)]
    assert source(stub).new_since("1", ("primary", "updates"))[0] == ["p", "u"]
    everything = ("primary", "updates", "promotions", "social", "forums")
    assert source(stub).new_since("1", everything)[0] == ["p", "u", "n"]
