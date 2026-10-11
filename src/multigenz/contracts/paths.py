"""Repository-relative path patterns: normalisation, matching and overlap analysis.

A pattern is a POSIX repo-relative path. A trailing ``/`` means "everything beneath that
directory". ``*`` and ``?`` do not cross ``/``; ``**`` does.
"""

from __future__ import annotations

import re
from functools import lru_cache

GLOB_CHARS = set("*?[")


def normalize_pattern(raw: str) -> str:
    """Return a canonical safe pattern or raise ValueError (paths escaping repo scope)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path pattern must be a non-empty string")
    p = raw.strip()
    if "\x00" in p:
        raise ValueError(f"NUL byte in path pattern {raw!r}")
    if "\\" in p:
        raise ValueError(f"backslash not allowed in path pattern {raw!r}")
    if p.startswith("/") or p.startswith("~") or re.match(r"^[A-Za-z]:", p):
        raise ValueError(f"absolute path pattern not allowed: {raw!r}")
    is_dir = p.endswith("/")
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if not parts:
        raise ValueError(f"path pattern {raw!r} selects the whole repository root")
    for c in parts:
        if c == "..":
            raise ValueError(f"path pattern escapes repository scope: {raw!r}")
        if c.lower() == ".git":
            raise ValueError(f"path pattern may not reference .git: {raw!r}")
    out = "/".join(parts)
    return out + "/" if is_dir else out


@lru_cache(maxsize=2048)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    if pattern.endswith("/"):
        return re.compile("^" + re.escape(pattern) + ".*$", re.S)
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                if pattern[i : i + 1] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.S)


def path_matches(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


def _static_prefix(pattern: str) -> str:
    """Directory prefix (ending in '/') that is fixed before any glob character."""
    if pattern.endswith("/"):
        return pattern
    idx = next((i for i, ch in enumerate(pattern) if ch in GLOB_CHARS), None)
    if idx is None:
        return pattern  # literal file
    cut = pattern.rfind("/", 0, idx)
    return pattern[: cut + 1]


def _is_glob(pattern: str) -> bool:
    return not pattern.endswith("/") and any(ch in GLOB_CHARS for ch in pattern)


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative overlap test: may report overlap for disjoint globs, never the reverse."""
    a_glob, b_glob = _is_glob(a), _is_glob(b)
    if not a_glob and not a.endswith("/"):
        return path_matches(a, [b])
    if not b_glob and not b.endswith("/"):
        return path_matches(b, [a])
    pa, pb = _static_prefix(a), _static_prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


def sets_overlap(xs: list[str], ys: list[str]) -> bool:
    return any(patterns_overlap(x, y) for x in xs for y in ys)
