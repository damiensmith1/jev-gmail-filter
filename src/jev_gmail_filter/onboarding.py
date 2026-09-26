"""Setup steps shared by `init` (CLI) and the web UI's setup wizard."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jevfilter as jf

from .config import EXAMPLE_TOPICS, Settings
from .db import Store
from .gmail import GmailSource, MailSource, SetupError, authorize, check_client_file, primary_query

BACKSCAN_DAYS = {"1 day": 1.0, "1 week": 7.0, "2 weeks": 14.0, "1 month": 30.0}

GOOGLE_STEPS = [
    (
        "Create a Google Cloud project",
        "Go to https://console.cloud.google.com and create a project (any name).",
    ),
    ("Enable the Gmail API", 'APIs & Services → Library → search "Gmail API" → Enable.'),
    (
        "Create a Desktop client",
        'Google Auth Platform → Clients → Create client → Application type "Desktop app" → '
        "Create, then Download JSON. If Google first asks you to configure the app, give it "
        "any name, your email as the support and contact email, and audience External; "
        "leave everything else (logo, home page, privacy policy, domains) blank.",
    ),
    (
        "Sign in",
        "Google will say it \"hasn't verified this app\": it's your own app, so choose "
        "Advanced → Go to (your app), then allow access.",
    ),
]

TESTING_NOTE = (
    "Your app stays in Google's \"Testing\" mode, which is fine: you'll be asked to sign in "
    "again every 7 days. If you're signing in with a different Google account from the one "
    "that owns the project, add it under Google Auth Platform → Audience → Test users."
)


@dataclass(frozen=True)
class SetupState:
    api_key: bool
    client: bool
    signed_in: bool
    topics: bool
    finished: bool

    @property
    def complete(self) -> bool:
        return self.api_key and self.client and self.signed_in and self.topics and self.finished


def state(settings: Settings) -> SetupState:
    finished = False
    if settings.db_path.exists():
        with Store(settings.db_path) as store:
            finished = store.get_meta("setup_done") == "1"
    return SetupState(
        api_key=bool(os.environ.get("TYPESAFE_API_KEY")),
        client=settings.credentials_path.exists(),
        signed_in=settings.token_path.exists(),
        topics=has_topics(settings),
        finished=finished,
    )


# -- step 1: API key -----------------------------------------------------------------


def save_api_key(settings: Settings, key: str) -> None:
    key = key.strip()
    if not key:
        raise SetupError("paste your TypeSafe API key")
    path = settings.env_path
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.startswith("TYPESAFE_API_KEY=")]
    path.write_text("\n".join([*lines, f"TYPESAFE_API_KEY={key}"]) + "\n")
    path.chmod(0o600)
    os.environ["TYPESAFE_API_KEY"] = key


# -- step 2: the user's OAuth client ---------------------------------------------------


def install_client(settings: Settings, source: Path | bytes) -> None:
    """Validate a downloaded client JSON (path or uploaded bytes) and keep a copy."""
    settings.ensure()
    tmp = settings.data_dir / ".client-upload.json"
    try:
        if isinstance(source, bytes):
            tmp.write_bytes(source)
        else:
            shutil.copyfile(Path(source).expanduser(), tmp)
        check_client_file(tmp)
        tmp.replace(settings.credentials_path)
        settings.credentials_path.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)


# -- step 3: sign-in -----------------------------------------------------------------------


def sign_in(settings: Settings) -> MailSource:
    """Run the browser sign-in (blocks until the user finishes in the browser)."""
    return GmailSource(authorize(settings.credentials_path, settings.token_path))


def connect(settings: Settings) -> MailSource:
    """Use the saved sign-in; never opens a browser."""
    if not settings.token_path.exists():
        raise SetupError("not signed in yet; finish setup first")
    return GmailSource(authorize(settings.credentials_path, settings.token_path, interactive=False))


def sign_out(settings: Settings) -> None:
    settings.token_path.unlink(missing_ok=True)


# -- step 4: topics ------------------------------------------------------------------------


@dataclass(frozen=True)
class Example:
    path: Path
    name: str
    description: str


def examples() -> list[Example]:
    out = []
    for path in sorted(EXAMPLE_TOPICS.glob("*.yaml")):
        topic = next(iter(jf.Topic.load(path).values()))
        out.append(Example(path, topic.name, " ".join(str(topic.description).split())))
    return out


def install_topics(settings: Settings, chosen: list[Path]) -> jf.Topics:
    if not chosen:
        raise ValueError("pick at least one topic (you can edit or add more later)")
    settings.ensure()
    for path in chosen:
        shutil.copyfile(path, settings.topics_dir / path.name)
    return jf.Topic.load(settings.topics_dir)


def has_topics(settings: Settings) -> bool:
    folder = settings.topics_dir
    return folder.exists() and any(p.suffix in (".yaml", ".yml", ".json") for p in folder.iterdir())


def topic_files(settings: Settings) -> dict[str, Path]:
    """Topic name → the file it came from."""
    out = {}
    for path in sorted(settings.topics_dir.iterdir()):
        if path.suffix in (".yaml", ".yml", ".json"):
            for name in jf.Topic.load(path):
                out[name] = path
    return out


def topic_filename(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "topic"
    return f"{slug}.yaml"


# -- step 5: backscan ---------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    since: datetime
    ids: list[str]
    low_usd: float
    high_usd: float


def estimate(source: MailSource, topics: jf.Topics, days: float, sample: int = 5) -> Estimate:
    """How many Primary emails in the window, and the cost range: membership only
    (low) up to every topic matching (high). Makes no Jev calls."""
    since = datetime.now(UTC) - timedelta(days=days)
    ids = list(source.search(primary_query(since)))
    if not ids:
        return Estimate(since, ids, 0.0, 0.0)
    f = jf.Filter(topics, judge=_NoCalls(), speculative=False)
    picks = ids[:: max(len(ids) // sample, 1)][:sample]
    plans = [f.explain(source.get(i).state()) for i in picks]
    low = sum(p.cost_usd for p in plans) / len(plans) * len(ids)
    high = sum(p.max_cost_usd for p in plans) / len(plans) * len(ids)
    return Estimate(since, ids, low, high)


def budget_for(est: Estimate) -> float:
    """A spend cap with headroom over the estimate, but never absurdly low."""
    return max(round(est.high_usd * 2, 2), 0.05)


def finish(settings: Settings, *, labels_on: bool, backscan_days: float | None) -> None:
    with Store(settings.db_path) as store:
        store.set_meta("labels", "on" if labels_on else "off")
        if backscan_days is not None:
            store.set_meta("backscan_days", f"{backscan_days:g}")
        store.set_meta("setup_done", "1")


class _NoCalls:
    def ask(self, state: Any, questions: Any) -> Any:  # pragma: no cover
        raise AssertionError("estimating must not call Jev")


def client_summary(settings: Settings) -> str:
    """Project id of the installed client, for display (never the secret)."""
    try:
        data = json.loads(settings.credentials_path.read_text())["installed"]
        return data.get("project_id", "your Google project")
    except (OSError, ValueError, KeyError):
        return "your Google project"
