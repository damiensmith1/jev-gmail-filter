import shutil
from pathlib import Path

import jevfilter as jf
import pytest

from jev_gmail_filter.config import EXAMPLE_TOPICS, Settings
from jev_gmail_filter.db import Store
from jev_gmail_filter.pipeline import Pipeline

from .fakes import FakeMail, judge


@pytest.fixture
def topics():
    return jf.Topic.load(EXAMPLE_TOPICS)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.sqlite")
    yield s
    s.close()


@pytest.fixture
def mail():
    return FakeMail()


@pytest.fixture
def fake_judge():
    return judge()


@pytest.fixture
def pipeline(store, mail, topics, fake_judge):
    return Pipeline(store, mail, topics, judge=fake_judge)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings(tmp_path / "data")
    s.ensure()
    shutil.copytree(EXAMPLE_TOPICS, s.topics_dir, dirs_exist_ok=True)
    return s


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLE_TOPICS
