"""Gmail access: sign-in, reading the Primary inbox, and writing labels.

`MailSource` is the interface the rest of the app uses; `GmailSource` is
the real one (Gmail API), and tests use a fake. Each user signs in with
their own OAuth client (see docs/design.md → Google access).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .mail import Email, parse_message

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PRIMARY = "in:inbox category:primary"
PRIMARY_LABEL = "CATEGORY_PERSONAL"  # Gmail's internal id for the Primary tab


class SetupError(Exception):
    """Something about the Google setup is wrong; the message says what to fix."""


class HistoryExpired(Exception):
    """The saved history id is too old; fall back to a date-based scan."""


class MailSource(Protocol):
    def address(self) -> str: ...
    def history_id(self) -> str: ...
    def search(self, query: str) -> Iterator[str]: ...
    def get(self, message_id: str) -> Email: ...
    def new_since(self, history_id: str) -> tuple[list[str], str]: ...
    def add_labels(self, message_id: str, names: list[str]) -> None: ...


def primary_query(after: datetime | None = None) -> str:
    """Gmail search for the Primary inbox, optionally only after a date."""
    return PRIMARY + (f" after:{int(after.timestamp())}" if after else "")


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
            "Google sign-in expired or was revoked. Run `jev-gmail-filter init` to sign in "
            "again. If this happens every week, publish your OAuth app to 'In production' "
            "(step 4)."
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

    def address(self) -> str:
        return self._svc.users().getProfile(userId="me").execute()["emailAddress"]

    def history_id(self) -> str:
        return str(self._svc.users().getProfile(userId="me").execute()["historyId"])

    def search(self, query: str) -> Iterator[str]:
        token = None
        while True:
            res = (
                self._svc.users()
                .messages()
                .list(userId="me", q=query, maxResults=500, pageToken=token)
                .execute()
            )
            for m in res.get("messages", []):
                yield m["id"]
            token = res.get("nextPageToken")
            if not token:
                return

    def get(self, message_id: str) -> Email:
        msg = self._svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        return parse_message(msg)

    def new_since(self, history_id: str) -> tuple[list[str], str]:
        """Primary-inbox messages added since `history_id`, and the new id."""
        from googleapiclient.errors import HttpError

        ids: list[str] = []
        latest = history_id
        token = None
        while True:
            try:
                res = (
                    self._svc.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=history_id,
                        historyTypes=["messageAdded"],
                        labelId="INBOX",
                        pageToken=token,
                    )
                    .execute()
                )
            except HttpError as e:
                if e.resp.status == 404:
                    raise HistoryExpired() from e
                raise
            for h in res.get("history", []):
                for added in h.get("messagesAdded", []):
                    msg = added["message"]
                    if PRIMARY_LABEL in msg.get("labelIds", []) and msg["id"] not in ids:
                        ids.append(msg["id"])
            latest = str(res.get("historyId", latest))
            token = res.get("nextPageToken")
            if not token:
                return ids, latest

    def add_labels(self, message_id: str, names: list[str]) -> None:
        label_ids = [self._label_id(n) for n in names]
        self._svc.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": label_ids}
        ).execute()

    def _label_id(self, name: str) -> str:
        if self._labels is None:
            res = self._svc.users().labels().list(userId="me").execute()
            self._labels = {lab["name"]: lab["id"] for lab in res.get("labels", [])}
        if name not in self._labels:
            parent = name.rsplit("/", 1)[0] if "/" in name else None
            if parent:
                self._label_id(parent)  # Gmail nests "A/B" under "A"
            created = (
                self._svc.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            self._labels[name] = created["id"]
        return self._labels[name]
