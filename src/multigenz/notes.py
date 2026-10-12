"""Standing notes for a repository - MultiGenz's counterpart of Claude Code's ``#`` memory.

Notes are kept in MultiGenz's own config directory (``~/.config/multigenz/notes/``), never in the repository (whose
working tree MultiGenz does not modify), and are appended to every goal started for that repository from the TUI, so
every planner and worker sees them. They are plain text the user wrote; nothing an agent says can add one.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

HEADER = "Standing notes for this repository (from the user):"


def notes_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "multigenz" / "notes"


def notes_path(repo: Path) -> Path:
    key = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:16]
    return notes_dir() / f"{key}.md"


def read_notes(repo: Path) -> list[str]:
    p = notes_path(repo)
    if not p.is_file():
        return []
    return [ln[2:].strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]


def _write(repo: Path, notes: list[str]) -> list[str]:
    p = notes_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(f"# MultiGenz notes for {Path(repo).resolve()}\n" + "".join(f"- {n}\n" for n in notes),
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return notes


def add_note(repo: Path, text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        raise ValueError("an empty note")
    notes = read_notes(repo)
    if text not in notes:
        notes.append(text)
    return _write(repo, notes)


def remove_note(repo: Path, number: int) -> list[str]:
    notes = read_notes(repo)
    if not 1 <= number <= len(notes):
        raise ValueError(f"there is no note {number} (there are {len(notes)})")
    del notes[number - 1]
    return _write(repo, notes)


def clear_notes(repo: Path) -> None:
    notes_path(repo).unlink(missing_ok=True)


def with_notes(goal: str, notes: list[str]) -> str:
    """The goal as the agents receive it: the user's text, then the standing notes (if any)."""
    if not notes:
        return goal
    return goal.rstrip() + "\n\n" + HEADER + "\n" + "".join(f"- {n}\n" for n in notes).rstrip()
