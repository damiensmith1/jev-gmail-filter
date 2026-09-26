import base64

from jev_gmail_filter.mail import html_to_text, parse_message


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(payload, **kw):
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1758800000000",
        "snippet": "Hi &amp; bye",
        "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
        "payload": payload,
        **kw,
    }


HEADERS = [
    {"name": "From", "value": "Acme <jobs@acme.example>"},
    {"name": "Subject", "value": "Hello"},
]


def test_plain_text_preferred_in_multipart():
    m = parse_message(
        message(
            {
                "mimeType": "multipart/alternative",
                "headers": HEADERS,
                "parts": [
                    {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
                    {"mimeType": "text/plain", "body": {"data": b64("plain\r\n\r\n\r\n\r\ntext")}},
                ],
            }
        )
    )
    assert m.body == "plain\n\ntext"
    assert (m.id, m.thread_id, m.subject, m.sender_name, m.sender_email) == (
        "m1",
        "t1",
        "Hello",
        "Acme",
        "jobs@acme.example",
    )
    assert m.date.year >= 2025 and m.snippet == "Hi & bye" and "CATEGORY_PERSONAL" in m.label_ids


def test_html_only_is_converted_to_text():
    html = (
        "<html><head><style>p{}</style></head><body><p>Hello&nbsp;there</p>"
        "<script>x()</script><div>Bye</div></body></html>"
    )
    m = parse_message(
        message({"mimeType": "text/html", "headers": HEADERS, "body": {"data": b64(html)}})
    )
    assert "Hello" in m.body and "Bye" in m.body and "x()" not in m.body and "p{}" not in m.body


def test_missing_body_falls_back_to_snippet():
    m = parse_message(message({"mimeType": "text/plain", "headers": HEADERS, "body": {}}))
    assert m.body == "Hi & bye"


def test_state_trims_long_bodies():
    m = parse_message(
        message({"mimeType": "text/plain", "headers": HEADERS, "body": {"data": b64("x" * 10000)}})
    )
    assert len(m.state()["body"]) == 6000 and set(m.state()) == {"from", "subject", "date", "body"}


def test_html_to_text_blocks_on_new_lines():
    assert html_to_text("<div>a</div><div>b</div>").split() == ["a", "b"]
