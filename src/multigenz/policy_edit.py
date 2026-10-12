"""Small, comment-preserving edits to a policy file: the ``roles:`` block and an agent's ``enabled:`` flag.

Every save goes through the real policy loader before it replaces the file, keeps a ``.bak`` copy, and checks that the
loaded policy actually says what was asked for; anything else leaves the original untouched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from multigenz.config import ROLE_NAMES, Policy, load_policy
from multigenz.errors import ConfigError

ROLES_COMMENT = ("# Who plays which part, in order of preference (the next one is the fallback). An empty list means\n"
                 "# automatic (chosen by capability). Edit with `multigenz tui` (Team) or `multigenz roles`.\n")


def _top_level_block(lines: list[str], key: str) -> tuple[int, int] | None:
    """[start, end) of a top-level ``key:`` block: the key line and the indented/blank lines that follow it."""
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", ln)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def render_roles(roles: dict[str, list[str]]) -> str:
    body = "".join(f"  {r}: {json.dumps(list(roles.get(r, [])))}\n" for r in ROLE_NAMES)
    return ROLES_COMMENT + "roles:\n" + body


def set_roles_text(text: str, roles: dict[str, list[str]]) -> str:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    block = render_roles(roles).splitlines(keepends=True)
    found = _top_level_block(lines, "roles")
    if found:
        start, end = found
        # drop our own explanatory comment if it sits right above the old block, so it is not duplicated
        head = ROLES_COMMENT.splitlines(keepends=True)
        if start >= len(head) and lines[start - len(head):start] == head:
            start -= len(head)
        lines[start:end] = block
        return "".join(lines)
    agents = _top_level_block(lines, "agents")
    at = agents[1] if agents else len(lines)
    lines[at:at] = block
    return "".join(lines)


def set_enabled_text(text: str, agent: str, enabled: bool) -> str:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    agents = _top_level_block(lines, "agents")
    if agents is None:
        raise ConfigError("the policy has no agents: block")
    a_start, a_end = agents
    header = None
    for i in range(a_start + 1, a_end):
        m = re.match(rf"^(\s+){re.escape(agent)}\s*:(.*)$", lines[i].rstrip("\n"))
        if m:
            header, indent, rest = i, len(m.group(1)), m.group(2)
            break
    if header is None:
        raise ConfigError(f"agent {agent!r} not found under agents:")
    end = header + 1
    while end < a_end and (not lines[end].strip() or lines[end].lstrip().startswith("#")
                           or len(lines[end]) - len(lines[end].lstrip()) > indent):
        end += 1
    value = "true" if enabled else "false"
    for i in range(header, end):
        new, n = re.subn(r"\benabled\s*:\s*(true|false|yes|no|True|False)\b", f"enabled: {value}", lines[i], count=1)
        if n:
            lines[i] = new
            return "".join(lines)
    if "{" in rest:  # flow mapping: "  name: {adapter: cli, ...}"
        lines[header] = lines[header].replace("{", "{enabled: " + value + ", ", 1)
        return "".join(lines)
    child = next((len(lines[i]) - len(lines[i].lstrip()) for i in range(header + 1, end)
                  if lines[i].strip() and not lines[i].lstrip().startswith("#")), indent + 2)
    lines.insert(header + 1, " " * child + f"enabled: {value}\n")
    return "".join(lines)


def short_error(exc: BaseException) -> str:
    """The human part of a policy validation error ("roles.worker: unknown agent 'x'"), without pydantic noise."""
    found = re.findall(r"Value error, (.*?) \[type=", str(exc))
    return "; ".join(found) if found else str(exc).splitlines()[0]


def save_policy_edits(path: Path, *, roles: dict[str, list[str]] | None = None,
                      enabled: dict[str, bool] | None = None) -> tuple[Policy, Path]:
    """Apply edits, validate with the real loader, verify the result, then replace the file (backup kept)."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no policy file (create one with `multigenz init`)")
    text = original = path.read_text(encoding="utf-8")
    for name, on in (enabled or {}).items():
        text = set_enabled_text(text, name, on)
    if roles is not None:
        text = set_roles_text(text, roles)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        pol = load_policy(tmp)
        for name, on in (enabled or {}).items():
            if pol.agents[name].enabled != on:
                raise ConfigError(f"could not set agents.{name}.enabled to {on} in {path}")
        if roles is not None:
            for r in ROLE_NAMES:
                if getattr(pol.roles, r) != list(roles.get(r, [])):
                    raise ConfigError(f"could not set roles.{r} in {path}")
    except Exception:
        tmp.unlink()
        raise
    backup = path.with_name(path.name + ".bak")
    if text != original:
        shutil.copy(path, backup)
        os.replace(tmp, path)
    else:
        tmp.unlink()
    return pol, backup
