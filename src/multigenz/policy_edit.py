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


def set_agent_model_text(text: str, agent: str, model: str | None) -> str:
    """Set (or, with None, remove) ``agents.<agent>.model``, keeping everything else."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    agents = _top_level_block(lines, "agents")
    if agents is None:
        raise ConfigError("the policy has no agents: block")
    a_start, a_end = agents
    header = indent = None
    rest = ""
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
    literal = json.dumps(model) if model is not None else None
    if "{" in rest:  # flow mapping on the header line
        line = lines[header]
        if re.search(r"\bmodel\s*:", line):
            if literal is None:
                line = re.sub(r"\bmodel\s*:\s*(\"[^\"]*\"|'[^']*'|[^,}]+)\s*,?\s*", "", line, count=1)
            else:
                line = re.sub(r"(\bmodel\s*:\s*)(\"[^\"]*\"|'[^']*'|[^,}]+)", lambda m: m.group(1) + literal, line,
                              count=1)
        elif literal is not None:
            line = line.replace("{", "{model: " + literal + ", ", 1)
        lines[header] = line
        return "".join(lines)
    for i in range(header + 1, end):
        if re.match(r"^\s+model\s*:", lines[i]):
            if literal is None:
                del lines[i]
            else:
                lines[i] = re.sub(r"^(\s+model\s*:\s*)(\"[^\"]*\"|'[^']*'|[^#\n]*?)(\s*#.*)?$",
                                  lambda m: m.group(1) + literal + (m.group(3) or ""), lines[i].rstrip("\n")) + "\n"
            return "".join(lines)
    if literal is not None:
        child = next((len(lines[i]) - len(lines[i].lstrip()) for i in range(header + 1, end)
                      if lines[i].strip() and not lines[i].lstrip().startswith("#")), indent + 2)
        lines.insert(header + 1, " " * child + f"model: {literal}\n")
    return "".join(lines)


def set_orchestrator_key_text(text: str, key: str, value: str) -> str:
    """Set ``orchestrator.<key>: <value>`` (a plain scalar), keeping everything else, comments included."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    block = _top_level_block(lines, "orchestrator")
    if block is None:
        at = next((i + 1 for i, ln in enumerate(lines) if re.match(r"^version\s*:", ln)), 0)
        lines[at:at] = ["orchestrator:\n", f"  {key}: {value}\n"]
        return "".join(lines)
    start, end = block
    head = lines[start]
    if "{" in head:  # flow mapping
        if re.search(rf"\b{re.escape(key)}\s*:", head):
            lines[start] = re.sub(rf"(\b{re.escape(key)}\s*:\s*)[^,}}#]+", rf"\g<1>{value}", head, count=1)
        else:
            lines[start] = head.replace("{", "{" + f"{key}: {value}, ", 1)
        return "".join(lines)
    for i in range(start + 1, end):
        m = re.match(rf"^(\s+{re.escape(key)}\s*:\s*)([^#\n]*?)(\s*#.*)?$", lines[i].rstrip("\n"))
        if m:
            lines[i] = m.group(1) + value + (m.group(3) or "") + "\n"
            return "".join(lines)
    child = next((len(lines[i]) - len(lines[i].lstrip()) for i in range(start + 1, end)
                  if lines[i].strip() and not lines[i].lstrip().startswith("#")), 2)
    lines.insert(start + 1, " " * child + f"{key}: {value}\n")
    return "".join(lines)


def short_error(exc: BaseException) -> str:
    """The human part of a policy validation error ("roles.worker: unknown agent 'x'"), without pydantic noise."""
    found = re.findall(r"Value error, (.*?) \[type=", str(exc))
    return "; ".join(found) if found else str(exc).splitlines()[0]


def save_policy_edits(path: Path, *, roles: dict[str, list[str]] | None = None,
                      enabled: dict[str, bool] | None = None, planning: str | None = None,
                      models: dict[str, str | None] | None = None) -> tuple[Policy, Path]:
    """Apply edits, validate with the real loader, verify the result, then replace the file (backup kept)."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no policy file (create one with `multigenz init`)")
    text = original = path.read_text(encoding="utf-8")
    for name, on in (enabled or {}).items():
        text = set_enabled_text(text, name, on)
    if roles is not None:
        text = set_roles_text(text, roles)
    if planning is not None:
        text = set_orchestrator_key_text(text, "planning", planning)
    for name, model in (models or {}).items():
        text = set_agent_model_text(text, name, model or None)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        pol = load_policy(tmp)
        for name, on in (enabled or {}).items():
            if pol.agents[name].enabled != on:
                raise ConfigError(f"could not set agents.{name}.enabled to {on} in {path}")
        for name, model in (models or {}).items():
            if pol.agents[name].model != (model or None):
                raise ConfigError(f"could not set agents.{name}.model in {path}")
        if planning is not None and pol.orchestrator.planning != planning:
            raise ConfigError(f"could not set orchestrator.planning to {planning!r} in {path}")
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


# -- API providers ------------------------------------------------------------------------------------------------------
def insert_top_entry(text: str, top_key: str, name: str, block: str, before: str = "agents") -> str:
    """Add ``name`` (a 2-space-indented ``block``) to the top-level mapping ``top_key``, creating it if needed."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    found = _top_level_block(lines, top_key)
    if found:
        start, end = found
        if any(re.match(rf"^\s+{re.escape(name)}\s*:", ln) for ln in lines[start + 1:end]):
            raise ConfigError(f"{top_key}.{name} already exists")
        if re.match(rf"^{re.escape(top_key)}\s*:\s*\{{\}}", lines[start]):
            lines[start] = f"{top_key}:\n"
        lines[end:end] = block.splitlines(keepends=True)
        return "".join(lines)
    anchor = _top_level_block(lines, before)
    at = anchor[0] if anchor else len(lines)
    lines[at:at] = [f"{top_key}:\n", *block.splitlines(keepends=True)]
    return "".join(lines)


def render_provider(name: str, kind: str, base_url: str, api_key_env: str | None) -> str:
    out = [f"  {name}:", f"    kind: {json.dumps(kind)}", f"    base_url: {json.dumps(base_url)}"]
    out.append(f"    api_key_env: {json.dumps(api_key_env)}" if api_key_env else
               "    api_key_env: null   # no key needed (local server)")
    return "\n".join(out) + "\n"


def render_provider_agent(agent: str, provider: str, model: str, use: str, capabilities: list[str]) -> str:
    if use == "coder":
        head = ["    adapter: cli", '    command: "opencode"   # the coding harness that talks to the API']
    else:
        head = ["    adapter: litellm      # text only: may plan or review, cannot change files"]
    out = [f"  {agent}:", f"    # {provider} model {model} ({'coding agent via opencode' if use == 'coder' else 'text only'})",
           "    enabled: true", *head, f"    provider: {json.dumps(provider)}", f"    model: {json.dumps(model)}",
           f"    capabilities: {json.dumps(capabilities)}"]
    return "\n".join(out) + "\n"


def _save_text(path: Path, text: str, check) -> tuple[Policy, Path]:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        pol = load_policy(tmp)
        check(pol)
    except Exception:
        tmp.unlink()
        raise
    backup = path.with_name(path.name + ".bak")
    shutil.copy(path, backup)
    os.replace(tmp, path)
    return pol, backup


def add_provider(path: Path, name: str, *, kind: str, base_url: str, api_key_env: str | None) -> tuple[Policy, Path]:
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no policy file (create one with `multigenz init`)")
    text = insert_top_entry(path.read_text(encoding="utf-8"), "providers", name,
                            render_provider(name, kind, base_url, api_key_env))

    def check(pol: Policy) -> None:
        if name not in pol.providers:
            raise ConfigError(f"could not add providers.{name}")
    return _save_text(path, text, check)


def add_provider_agent(path: Path, agent: str, *, provider: str, model: str, use: str = "coder",
                       capabilities: list[str] | None = None) -> tuple[Policy, Path]:
    """Add an agent that uses ``provider``'s ``model``: a coding agent (opencode harness) or text only (LiteLLM)."""
    from multigenz.presets import insert_agent

    if use not in ("coder", "text"):
        raise ConfigError("use must be 'coder' or 'text'")
    caps = capabilities or (["architecture", "coding", "testing", "review"] if use == "coder"
                            else ["architecture", "review"])
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no policy file (create one with `multigenz init`)")
    text = insert_agent(path.read_text(encoding="utf-8"), render_provider_agent(agent, provider, model, use, caps),
                        agent)

    def check(pol: Policy) -> None:
        a = pol.agents.get(agent)
        if a is None or a.provider != provider or a.model != model:
            raise ConfigError(f"could not add agents.{agent}")
    return _save_text(path, text, check)


def agent_name_for(provider: str, model: str, taken: set[str]) -> str:
    """A valid, unused agent name such as ``openrouter-qwen3-coder``."""
    base = re.sub(r"[^a-z0-9_-]+", "-", f"{provider}-{model.split('/')[-1]}".lower()).strip("-_")[:28] or provider
    if not re.match(r"^[a-z]", base):
        base = "m-" + base
    name, n = base, 2
    while name in taken:
        name = f"{base[:25]}-{n}"
        n += 1
    return name
