from pathlib import Path

import jevfilter as jf
import pytest

from jev_gmail_filter import __version__
from jev_gmail_filter.cli import main

EXAMPLES = Path(__file__).parents[1] / "topics" / "examples"


def test_example_topics_are_valid_jevfilter_topics():
    topics = jf.Topic.load(EXAMPLES)
    assert set(topics) >= {"Jobs", "Receipts"}
    for t in topics.values():
        assert t.warnings == ()
        assert "gmail_label" in (t.meta or {})


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out
