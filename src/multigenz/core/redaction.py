"""Credential redaction for every log line, event payload and report.

Redaction happens *before* anything is written to disk or SQLite. It combines
(1) exact values of known secrets (host env vars whose names look sensitive plus any value
explicitly registered) in plain / URL-quoted / base64 forms, and (2) structural patterns for
common token formats and PEM private-key blocks.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 6

SENSITIVE_NAME_RE = re.compile(r"(?i)(key|token|secret|passw|credential|auth|private|cookie|session)")

PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----", re.S),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
    re.compile(
        r"(?i)\b([A-Za-z0-9_.\-]*(?:api[_-]?key|secret|token|passw(?:or)?d|credential)[A-Za-z0-9_.\-]*)"
        r"(\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}\3"
    ),
]
_ASSIGN_PATTERN = PATTERNS[-1]
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")


def _variants(value: str) -> set[str]:
    out = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)}
    out.add(base64.b64encode(value.encode()).decode())
    out.add(base64.urlsafe_b64encode(value.encode()).decode())
    return {v for v in out if len(v) >= MIN_SECRET_LEN}


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets: set[str] = set()
        self._compiled: re.Pattern[str] | None = None
        for s in secrets:
            self.add_secret(s)

    @classmethod
    def from_environ(cls, env: Mapping[str, str], extra: Iterable[str] = ()) -> "Redactor":
        r = cls(extra)
        r.add_environ(env)
        return r

    def add_environ(self, env: Mapping[str, str]) -> None:
        for name, value in env.items():
            if SENSITIVE_NAME_RE.search(name):
                self.add_secret(value)

    def add_secret(self, value: str | None) -> None:
        if not value or len(value) < MIN_SECRET_LEN:
            return
        before = len(self._secrets)
        self._secrets |= _variants(value)
        if len(self._secrets) != before:
            self._compiled = None

    def _secret_re(self) -> re.Pattern[str] | None:
        if not self._secrets:
            return None
        if self._compiled is None:
            alts = sorted(self._secrets, key=len, reverse=True)
            self._compiled = re.compile("|".join(re.escape(s) for s in alts))
        return self._compiled

    def redact(self, text: str) -> str:
        if not text:
            return text
        rx = self._secret_re()
        if rx is not None:
            text = rx.sub(REDACTED, text)
        for pat in PATTERNS:
            if pat is _ASSIGN_PATTERN:
                text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
            else:
                text = pat.sub(REDACTED, text)
        return text

    def redact_obj(self, obj: Any) -> Any:
        """Recursively redact strings in JSON-like structures (keys are kept)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj

    def stream(self) -> "StreamRedactor":
        return StreamRedactor(self)


class StreamRedactor:
    """Line-oriented redaction that also hides multi-line PEM private-key blocks."""

    def __init__(self, base: Redactor):
        self._base = base
        self._in_pem = False

    def line(self, text: str) -> str:
        if self._in_pem:
            if _PEM_END.search(text):
                self._in_pem = False
            return REDACTED
        if _PEM_BEGIN.search(text) and not _PEM_END.search(text):
            self._in_pem = True
            return REDACTED
        return self._base.redact(text)


def contains_secret(text: str, redactor: "Redactor | None" = None) -> str | None:
    """High-confidence secret detection (structural token formats + known values).

    Deliberately excludes the generic ``name = value`` heuristic used for log redaction, which
    would flag ordinary test fixtures such as ``password = "hunter2"``.
    """
    if redactor is not None:
        rx = redactor._secret_re()  # noqa: SLF001
        if rx is not None and rx.search(text):
            return "known credential value"
    for pat in PATTERNS:
        if pat is _ASSIGN_PATTERN:
            continue
        if pat.search(text):
            return f"credential-like token ({pat.pattern[:30]}...)"
    return None
