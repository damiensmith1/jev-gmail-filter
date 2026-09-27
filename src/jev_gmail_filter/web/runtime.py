"""Long-lived state for the web UI: background scans, sign-in, auto-sync, caches.

Requests are short and stateless; anything that outlives a request (a scan,
the browser sign-in, the auto-sync timer) lives here, on its own thread, so
refreshing or navigating never stops it. Each thread opens its own SQLite
connection.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jevfilter as jf

from .. import onboarding
from ..config import Settings
from ..db import Store
from ..gmail import MailSource
from ..mail import Email
from ..pipeline import Pipeline, SyncReport

Connect = Callable[[Settings], MailSource]


@dataclass
class ScanJob:
    kind: str  # "First scan", "Sync", "Rescan", "Re-check", "Labelling"
    labels_on: bool
    n: int = 0
    total: int = 0
    report: SyncReport | None = None
    error: str | None = None
    finished: bool = False
    dismissed: bool = False

    def progress(self, n: int, total: int) -> None:
        self.n, self.total = n, total

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "n": self.n,
            "total": self.total,
            "finished": self.finished,
            "error": self.error,
        }


@dataclass
class SignIn:
    state: str = "idle"  # idle | waiting | done | error
    error: str | None = None


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        connect: Connect | None = None,
        sign_in: Connect | None = None,
        judge: Any = None,
        auto_sync: bool = True,
    ):
        self.settings = settings
        self._connect = connect or onboarding.connect
        self._sign_in = sign_in or onboarding.sign_in
        self.judge = judge
        self._lock = threading.Lock()
        self._source: MailSource | None = None
        self.job: ScanJob | None = None
        self.signin = SignIn()
        self._emails: OrderedDict[str, Email] = OrderedDict()
        self._stop = threading.Event()
        self._last_auto: datetime | None = None
        if auto_sync:
            threading.Thread(target=self._auto_sync_loop, name="jgf-auto-sync", daemon=True).start()

    # -- access ---------------------------------------------------------------------

    def store(self) -> Store:
        return Store(self.settings.db_path)

    def topics(self) -> jf.Topics:
        return jf.Topic.load(self.settings.topics_dir)

    def source(self) -> MailSource:
        with self._lock:
            if self._source is None:
                self._source = self._connect(self.settings)
            return self._source

    def forget_source(self) -> None:
        with self._lock:
            self._source = None
            self._emails.clear()

    def email(self, gmail_id: str) -> Email | None:
        """A full email from Gmail (cached); None if it can't be fetched."""
        with self._lock:
            if gmail_id in self._emails:
                self._emails.move_to_end(gmail_id)
                return self._emails[gmail_id]
        try:
            mail = self.source().get(gmail_id)
        except Exception:
            return None
        with self._lock:
            self._emails[gmail_id] = mail
            while len(self._emails) > 200:
                self._emails.popitem(last=False)
        return mail

    def pipeline(
        self, store: Store, *, budget: float | None = None, labels: bool | None = None
    ) -> Pipeline:
        cap = budget if budget is not None else float(store.get_meta("max_usd") or 1.0)
        write = (store.get_meta("labels") == "on") if labels is None else labels
        return Pipeline(
            store,
            self.source(),
            self.topics(),
            judge=self.judge,
            budget=jf.Budget(usd=cap),
            write_labels=write,
        )

    # -- scans ------------------------------------------------------------------------

    def scanning(self) -> bool:
        return self.job is not None and not self.job.finished

    def start_scan(
        self,
        kind: str,
        *,
        since: datetime | None = None,
        budget: float | None = None,
        labels: bool | None = None,
    ) -> bool:
        with self._lock:
            if self.job is not None and not self.job.finished:
                return False
            with self.store() as s:
                write = (s.get_meta("labels") == "on") if labels is None else labels
            job = self.job = ScanJob(kind, write)

        def work() -> None:
            try:
                with self.store() as s:
                    p = self.pipeline(s, budget=budget, labels=write)
                    if kind == "Re-check":
                        job.report = p.recheck_reviews(progress=job.progress)
                    elif kind == "Labelling":
                        job.report = p.apply_labels_to_matches(progress=job.progress)
                    else:
                        job.report = p.sync(since=since, progress=job.progress)
            except Exception as e:  # shown in the UI; the scan resumes next time
                job.error = str(e)
            finally:
                job.finished = True

        threading.Thread(target=work, name="jgf-scan", daemon=True).start()
        return True

    def wait(self, timeout: float = 30.0) -> None:
        """Block until the current scan finishes (tests, CLI)."""
        deadline = time.monotonic() + timeout
        while self.scanning() and time.monotonic() < deadline:
            time.sleep(0.02)

    # -- sign-in -----------------------------------------------------------------------

    def start_sign_in(self) -> None:
        with self._lock:
            if self.signin.state == "waiting":
                return
            self.signin = SignIn("waiting")

        def work() -> None:
            try:
                src = self._sign_in(self.settings)
                address = src.address()
                with self.store() as s:
                    s.set_meta("account", address)
                with self._lock:
                    self._source = src
                self.signin = SignIn("done")
            except Exception as e:
                self.signin = SignIn("error", str(e))

        threading.Thread(target=work, name="jgf-sign-in", daemon=True).start()

    # -- auto-sync -----------------------------------------------------------------------

    def _auto_sync_loop(self) -> None:
        while not self._stop.wait(20):
            try:
                self.tick()
            except Exception:
                pass  # never let the timer die; the next tick retries

    def tick(self, now: datetime | None = None) -> bool:
        """Start a sync if auto-sync is on and one is due. Returns whether it started."""
        if not self.settings.db_path.exists() or not self.settings.token_path.exists():
            return False
        with self.store() as s:
            minutes = int(s.get_meta("auto_sync_minutes") or 0)
            last = s.get_meta("last_sync_at")
            done = s.get_meta("setup_done") == "1"
        if not minutes or not done or self.scanning():
            return False
        now = now or datetime.now(UTC)
        every = timedelta(minutes=minutes)
        if last and now - datetime.fromisoformat(last) < every:
            return False
        if self._last_auto and now - self._last_auto < every:
            return False  # the last attempt failed or stopped early; wait a full interval
        self._last_auto = now
        return self.start_scan("Sync")

    def stop(self) -> None:
        self._stop.set()
