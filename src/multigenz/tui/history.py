"""Prompt history that survives restarts (like Claude Code's): one JSON line per entry in MultiGenz's private state
directory (mode 0600), secrets from the environment redacted before anything is written."""

from __future__ import annotations

import json
import os
from pathlib import Path

from multigenz.core.redaction import Redactor

MAX_ENTRIES = 500


class PromptHistory:
    def __init__(self, path: Path, redactor: Redactor | None = None):
        self.path = Path(path)
        self.redactor = redactor or Redactor.from_environ(os.environ)
        self.entries: list[str] = self._load()
        self.pos = len(self.entries)
        self.draft = ""

    def _load(self) -> list[str]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines[-MAX_ENTRIES:]:
            try:
                value = json.loads(ln)
            except ValueError:
                continue
            if isinstance(value, str):
                out.append(value)
        return out

    def add(self, text: str) -> None:
        text = self.redactor.redact(text.strip())
        if not text:
            return
        if not self.entries or self.entries[-1] != text:
            self.entries.append(text)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(text) + "\n")
            if new:
                os.chmod(self.path, 0o600)
            if len(self.entries) > 2 * MAX_ENTRIES:  # keep the file small
                self.entries = self.entries[-MAX_ENTRIES:]
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(e) + "\n" for e in self.entries), encoding="utf-8")
                os.chmod(tmp, 0o600)
                tmp.replace(self.path)
        self.pos = len(self.entries)
        self.draft = ""

    def previous(self, current: str) -> str | None:
        if not self.entries or self.pos == 0:
            return None
        if self.pos == len(self.entries):
            self.draft = current
        self.pos -= 1
        return self.entries[self.pos]

    def next(self) -> str | None:
        if self.pos >= len(self.entries):
            return None
        self.pos += 1
        return self.entries[self.pos] if self.pos < len(self.entries) else self.draft
