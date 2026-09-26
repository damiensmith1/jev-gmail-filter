"""Gmail access: sign-in, reading the Primary inbox, and writing labels.

`MailSource` is the interface the rest of the app uses; `GmailSource` is
the real one (Gmail API), and tests use a fake. Each user signs in with
their own OAuth client (see docs/design.md → Google access).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .mail import Email, parse_message

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
# Gmail's inbox categories (tabs): key → (name, Gmail's internal label id).
CATEGORIES = {
    "primary": ("Primary", "CATEGORY_PERSONAL"),
    "updates": ("Updates", "CATEGORY_UPDATES"),
    "promotions": ("Promotions", "CATEGORY_PROMOTIONS"),
    "social": ("Social", "CATEGORY_SOCIAL"),
    "forums": ("Forums", "CATEGORY_FORUMS"),
}
DEFAULT_CATEGORIES: tuple[str, ...] = ("primary",)
RETRIES = 6
"""Gmail limits requests per user per minute. On a rate-limit (or 5xx) answer the
Google client waits with exponential backoff and retries: up to ~2 minutes in all."""


# Gmail's per-user limit is 6,000 quota units per minute; each method has a cost.
# https://developers.google.com/workspace/gmail/api/reference/quota
UNITS_PER_MINUTE = 5_000  # headroom under 6,000 for Gmail itself / other clients
COST = {
    "get": 20,
    "list": 5,
    "modify": 5,
    "history": 2,
    "profile": 1,
    "labels.list": 1,
    "labels.create": 5,
}


class QuotaPacer:
    """Waits before a request that would push the last minute over budget, so
    syncs stay under Gmail's per-user rate limit instead of hitting it."""

    def __init__(
        self,
        units_per_minute: int = UNITS_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.budget = units_per_minute
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()
        self.waited = 0.0

    def take(self, units: int) -> None:
        with self._lock:
            while True:
                now = self._clock()
                while self._spent and now - self._spent[0][0] >= 60:
                    self._spent.popleft()
                used = sum(u for _, u in self._spent)
                if used + units <= self.budget or not self._spent:
                    self._spent.append((now, units))
                    return
                wait = 60 - (now - self._spent[0][0])
                self.waited += wait
                self._sleep(wait)


class SetupError(Exception):
    """Something about the Google setup is wrong; the message says what to fix."""


class HistoryExpired(Exception):
    """The saved history id is too old; fall back to a date-based scan."""


class MailSource(Protocol):
    def address(self) -> str: ...
    def history_id(self) -> str: ...
    def search(self, query: str) -> Iterator[str]: ...
    def get(self, message_id: str) -> Email: ...
    def new_since(
        self, history_id: str, categories: tuple[str, ...] = DEFAULT_CATEGORIES
    ) -> tuple[list[str], str]: ...
    def add_labels(self, message_id: str, names: list[str]) -> None: ...


def parse_categories(value: str | None) -> tuple[str, ...]:
    """Stored setting ("primary,updates") → valid category keys, in a stable order."""
    chosen = {c.strip().lower() for c in (value or "").split(",") if c.strip()}
    unknown = chosen - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown Gmail categories {sorted(unknown)}; use {', '.join(CATEGORIES)}")
    return tuple(c for c in CATEGORIES if c in chosen) or DEFAULT_CATEGORIES


def inbox_query(
    categories: tuple[str, ...] = DEFAULT_CATEGORIES, after: datetime | None = None
) -> str:
    """Gmail search for the chosen inbox categories, optionally only after a date."""
    if set(categories) >= set(CATEGORIES):
        query = "in:inbox"  # everything, including mail Gmail left uncategorised
    elif len(categories) == 1:
        query = f"in:inbox category:{categories[0]}"
    else:
        query = "in:inbox (" + " OR ".join(f"category:{c}" for c in categories) + ")"
    return query + (f" after:{int(after.timestamp())}" if after else "")


def primary_query(after: datetime | None = None) -> str:
    return inbox_query(DEFAULT_CATEGORIES, after)


# -- sign-in ------------------------------------------------------------------


def check_client_file(path: Path) -> None:
    """Fail early, with the fix, if this isn't a Desktop-app OAuth client."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SetupError(f"no OAuth client file at {path}") from None
    except ValueError:
        raise SetupError(f"{path} isn't JSON; download the client JSON again") from None
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        raise SetupError(
            f"{path.name} is a '{kind}' client, not a Desktop app client. "
            "Create a new OAuth client with application type 'Desktop app' (step 5)."
        )


def authorize(credentials_path: Path, token_path: Path, *, interactive: bool = True) -> Any:
    """Load the saved token, refresh it, or run the browser sign-in."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError:
            creds = None  # revoked, or expired after 7 days in Testing mode
    if not interactive:
        raise SetupError(
            "Google sign-in expired or was revoked. Sign in again (Settings → Sign out, then "
            "sign in, or `jev-gmail-filter init`). In Google's Testing mode this happens "
            "every 7 days."
        )
    check_client_file(credentials_path)
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    granted = set(creds.scopes or [])
    if not set(SCOPES) <= granted:
        raise SetupError(
            "Google didn't grant Gmail access. Sign in again and tick the box to let the "
            "app read, compose and send email (it only reads and labels)."
        )
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return creds


# -- the real source ------------------------------------------------------------


class GmailSource:
    def __init__(self, creds: Any):
        from googleapiclient.discovery import build

        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._labels: dict[str, str] | None = None
        self.pacer = QuotaPacer()

    def _call(self, kind: str, request: Any) -> Any:
        self.pacer.take(COST[kind])
        return request.execute(num_retries=RETRIES)

    def address(self) -> str:
        return self._call("profile", self._svc.users().getProfile(userId="me"))["emailAddress"]

    def history_id(self) -> str:
        return str(self._call("profile", self._svc.users().getProfile(userId="me"))["historyId"])

    def search(self, query: str) -> Iterator[str]:
        token = None
        while True:
            request = (
                self._svc.users()
                .messages()
                .list(userId="me", q=query, maxResults=500, pageToken=token)
            )
            res = self._call("list", request)
            for m in res.get("messages", []):
                yield m["id"]
            token = res.get("nextPageToken")
            if not token:
                return

    def get(self, message_id: str) -> Email:
        request = self._svc.users().messages().get(userId="me", id=message_id, format="full")
        return parse_message(self._call("get", request))

    def new_since(
        self, history_id: str, categories: tuple[str, ...] = DEFAULT_CATEGORIES
    ) -> tuple[list[str], str]:
        """Inbox messages in `categories` added since `history_id`, and the new id."""
        everything = set(categories) >= set(CATEGORIES)
        wanted = {CATEGORIES[c][1] for c in categories}
        from googleapiclient.errors import HttpError

        ids: list[str] = []
        latest = history_id
        token = None
        while True:
            request = (
                self._svc.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                    pageToken=token,
                )
            )
            try:
                res = self._call("history", request)
            except HttpError as e:
                if e.resp.status == 404:
                    raise HistoryExpired() from e
                raise
            for h in res.get("history", []):
                for added in h.get("messagesAdded", []):
                    msg = added["message"]
                    labels = set(msg.get("labelIds", []))
                    if (everything or labels & wanted) and msg["id"] not in ids:
                        ids.append(msg["id"])
            latest = str(res.get("historyId", latest))
            token = res.get("nextPageToken")
            if not token:
                return ids, latest

    def add_labels(self, message_id: str, names: list[str]) -> None:
        label_ids = [self._label_id(n) for n in names]
        request = (
            self._svc.users()
            .messages()
            .modify(userId="me", id=message_id, body={"addLabelIds": label_ids})
        )
        self._call("modify", request)

    def _label_id(self, name: str) -> str:
        if self._labels is None:
            res = self._call("labels.list", self._svc.users().labels().list(userId="me"))
            self._labels = {lab["name"]: lab["id"] for lab in res.get("labels", [])}
        if name not in self._labels:
            parent = name.rsplit("/", 1)[0] if "/" in name else None
            if parent:
                self._label_id(parent)  # Gmail nests "A/B" under "A"
            body = {
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            created = self._call(
                "labels.create", self._svc.users().labels().create(userId="me", body=body)
            )
            self._labels[name] = created["id"]
        return self._labels[name]
