"""Standing notes (# / /memory) and the persistent prompt history."""

import json
import stat

import pytest

from multigenz.core.redaction import Redactor
from multigenz.notes import HEADER, add_note, clear_notes, notes_path, read_notes, remove_note, with_notes
from multigenz.tui.history import PromptHistory


def test_notes_live_outside_the_repository_and_are_added_to_goals(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert read_notes(repo) == [] and with_notes("Build it", []) == "Build it"
    add_note(repo, "  use   pytest ")
    add_note(repo, "use pytest")                       # no duplicates
    add_note(repo, "keep functions small")
    assert read_notes(repo) == ["use pytest", "keep functions small"]
    p = notes_path(repo)
    assert str(p).startswith(str(tmp_path / "xdg")) and not list(repo.iterdir())     # nothing in the repository
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    goal = with_notes("Build it", read_notes(repo))
    assert goal == f"Build it\n\n{HEADER}\n- use pytest\n- keep functions small"
    assert remove_note(repo, 1) == ["keep functions small"]
    with pytest.raises(ValueError):
        remove_note(repo, 5)
    with pytest.raises(ValueError):
        add_note(repo, "   ")
    other = tmp_path / "other"
    other.mkdir()
    assert read_notes(other) == []                     # per repository
    clear_notes(repo)
    assert read_notes(repo) == []


def test_history_survives_restarts_without_secrets(tmp_path):
    path = tmp_path / "state" / "prompt-history.jsonl"
    red = Redactor(["sk-history-secret-0123456789"])
    h = PromptHistory(path, red)
    h.add("first goal")
    h.add("first goal")                                # consecutive duplicates are kept once
    h.add("deploy with key sk-history-secret-0123456789\nand a second line")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "sk-history-secret" not in path.read_text()
    h2 = PromptHistory(path, red)
    assert len(h2.entries) == 2 and "\n" in h2.entries[1]
    assert h2.previous("draft being typed") == h2.entries[1]
    assert h2.previous("") == "first goal"
    assert h2.previous("") is None                     # at the oldest
    assert h2.next() == h2.entries[1]
    assert h2.next() == "draft being typed"            # back to what was being typed
    assert h2.next() is None
    path.write_text(path.read_text() + "not json\n" + json.dumps({"x": 1}) + "\n")
    assert len(PromptHistory(path, red).entries) == 2  # damaged lines are skipped
