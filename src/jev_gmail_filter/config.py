"""Where the app keeps its files, and loading `.env`.

Everything personal lives in the data folder (default `./data`, override
with `JGF_DATA_DIR`), which is gitignored: your OAuth client and token,
your topics, and the SQLite database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

EXAMPLE_TOPICS = Path(__file__).resolve().parents[2] / "topics" / "examples"


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jev-gmail-filter.sqlite"

    @property
    def topics_dir(self) -> Path:
        return self.data_dir / "topics"

    @property
    def env_path(self) -> Path:
        return Path.cwd() / ".env"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(exist_ok=True)


def load_settings(data_dir: str | Path | None = None) -> Settings:
    """Load `.env` from the working directory (never overriding the real
    environment) and resolve the data folder."""
    load_dotenv(Path.cwd() / ".env", override=False)
    folder = data_dir or os.environ.get("JGF_DATA_DIR") or "data"
    return Settings(Path(folder).expanduser().resolve())
