"""What the prompt's completion menu offers at the cursor: slash commands and their arguments, and ``@`` file
mentions (the files of the repository's HEAD - exactly what the agents' worktrees contain)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

PLANNING_MODES = {
    "always": "a planner agent splits every goal into tasks first",
    "auto": "one worker takes the goal and asks for subagents only when needed",
    "never": "one worker does the whole goal (cheapest)",
}
MEMORY_ACTIONS = {"add": "add a standing note: /memory add TEXT", "remove": "remove note N: /memory remove N",
                  "clear": "remove every note for this repository"}
RUN_ARG_COMMANDS = ("/resume", "/cancel", "/open")


NEEDS_ARGUMENT = ("/plan",)  # Enter on these in the menu inserts them; the rest run at once


@dataclass(frozen=True)
class Item:
    label: str
    detail: str
    insert: str
    final: bool = False  # Enter on it completes the line, which is then sent


def first_line(text: str | None) -> str:
    return text.splitlines()[0] if text else ""


def repo_files(repo: Path | None, limit: int = 50_000) -> list[str]:
    """Paths in the repository's HEAD commit (what a run's agents see); [] without a repository or commit."""
    if repo is None:
        return []
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", "HEAD"], capture_output=True,
                           timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return [p.decode("utf-8", "replace") for p in r.stdout.split(b"\0") if p][:limit]


def match_files(query: str, files: list[str], limit: int = 30) -> list[str]:
    """Best matches first: file name starts with the query, then contains it, then the path does."""
    q = query.lower()
    if not q:
        return sorted(files, key=lambda f: (f.count("/"), f))[:limit]
    scored = []
    for f in files:
        low = f.lower()
        base = low.rsplit("/", 1)[-1]
        rank = 0 if base.startswith(q) else 1 if q in base else 2 if low.startswith(q) else 3 if q in low else None
        if rank is not None:
            scored.append((rank, len(f), f))
    return [f for *_, f in sorted(scored)[:limit]]


def completions(text: str, cursor: int, *, commands: dict[str, str], agents: dict[str, str] | None = None,
                files: list[str] | None = None, runs: list[dict] | None = None,
                themes: list[str] | None = None) -> tuple[int, int, list[Item]]:
    """(start, end, items): the span of ``text`` an accepted item replaces, and the items to offer."""
    before = text[:cursor]
    start = max(before.rfind(" "), before.rfind("\n")) + 1
    token = before[start:]
    end = cursor
    while end < len(text) and not text[end].isspace():
        end += 1
    if token.startswith("@"):
        return start, end, [Item(f"@{f}", "file", f"@{f} ") for f in match_files(token[1:], files or [])]
    if not text.startswith("/") or "\n" in before:
        return start, end, []
    words = before.split(" ")
    if len(words) == 1:
        return 0, end, [Item(c, d, c + " ", final=c not in NEEDS_ARGUMENT)
                        for c, d in commands.items() if c.startswith(token.lower())]
    cmd, nth = words[0].lower(), len(words) - 1
    if cmd in ("/model", "/models") and nth == 1:
        items = [Item(n, m, n + " ", final=True) for n, m in (agents or {}).items()]
    elif cmd == "/planning" and nth == 1:
        items = [Item(m, d, m, final=True) for m, d in PLANNING_MODES.items()]
    elif cmd in RUN_ARG_COMMANDS and nth == 1:
        items = [Item(r["run_id"], f"{r['status']:<10} {first_line(r['objective'])[:60]}", r["run_id"], final=True)
                 for r in runs or []]
    elif cmd == "/theme" and nth == 1:
        items = [Item(t, "", t, final=True) for t in themes or []]
    elif cmd == "/memory" and nth == 1:
        items = [Item(a, d, a + " ", final=a == "clear") for a, d in MEMORY_ACTIONS.items()]
    else:
        items = []
    return start, end, [i for i in items if i.label.lower().startswith(token.lower())]


def common_prefix(items: list[Item]) -> str:
    """The longest text every item's insertion starts with (for Tab when several match)."""
    if not items:
        return ""
    first = min(i.insert for i in items)
    last = max(i.insert for i in items)
    n = 0
    while n < min(len(first), len(last)) and first[n] == last[n]:
        n += 1
    return first[:n]
