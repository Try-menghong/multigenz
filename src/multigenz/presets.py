"""Agent presets: ready-made policy entries for common coding-agent CLIs, and policy bootstrapping.

A preset only writes configuration. Whether an agent is usable is still decided at probe time: every invocation must
be verified against the installed binary's own ``--help`` output, and unattended invocations refuse to run outside
the MultiGenz sandbox. Each preset says how well it has been tested:

* ``run-tested``  - driven end to end through MultiGenz (plan -> sandboxed attempt -> validation -> integration)
* ``help-verified`` - flags verified against the binary's help, but not run inside MultiGenz in development
* ``experimental`` - flags taken from a help text only; never executed in development

Logins are handed to an agent by *name*: environment variables (``credentials.env``) and files mounted READ-ONLY at
the same place under the sandbox home (``credentials.files``). Missing ones are skipped. Nothing else from the host is
ever visible to the agent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from multigenz.config import load_policy

SANDBOX_HOME = "/home/agent"


@dataclass(frozen=True)
class Preset:
    key: str
    agent: str                      # policy key written
    command: str
    capabilities: list[str]
    status: str                     # run-tested | help-verified | experimental
    summary: str
    env: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # paths under ~ mounted read-only at the same place in the sandbox
    invocations: list[dict] | None = None           # None: use the adapter's verified built-ins
    install_hint: str = ""


def _claude_pytest_invocations() -> list[dict]:
    allow = ["Bash(python3 -m pytest:*)", "Bash(python -m pytest:*)", "Bash(python3 -B -m pytest:*)",
             "Bash(python -B -m pytest:*)", "Bash(/usr/bin/python3 -m pytest:*)", "Bash(pytest:*)"]
    return [
        {"name": "print-accept-edits-pytest",
         "argv": ["{command}", "--print", "--permission-mode", "acceptEdits", "--allowedTools", *allow,
                  "--no-session-persistence"],
         "stdin": "prompt",
         "requires_help_tokens": ["--print", "--permission-mode", '"acceptEdits"', "--allowedTools",
                                  "--no-session-persistence"],
         "modes": ["code", "conflict"]},
        {"name": "print-plan",
         "argv": ["{command}", "--print", "--permission-mode", "plan", "--no-session-persistence"],
         "stdin": "prompt",
         "requires_help_tokens": ["--print", "--permission-mode", '"plan"', "--no-session-persistence"],
         "modes": ["plan", "review", "analysis"]},
    ]


def _dump(invs) -> list[dict]:
    return [i.model_dump(exclude_defaults=True) | {"name": i.name, "argv": i.argv} for i in invs]


def _agy_invocations() -> list[dict]:
    from multigenz.adapters.antigravity import EXPERIMENTAL_INVOCATIONS
    return _dump(EXPERIMENTAL_INVOCATIONS)


PRESETS: dict[str, Preset] = {p.key: p for p in [
    Preset("claude", "claude", "claude", ["architecture", "coding", "testing", "review"], "run-tested",
           "Claude Code (`claude --print`); may run pytest itself inside the sandbox",
           env=["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
           invocations=_claude_pytest_invocations(),
           install_hint="npm i -g @anthropic-ai/claude-code; for a Pro/Max login export the token from "
                        "`claude setup-token` as CLAUDE_CODE_OAUTH_TOKEN"),
    Preset("opencode", "opencode", "opencode", ["architecture", "coding", "testing", "review"], "run-tested",
           "opencode (`opencode run --auto`), any provider opencode is logged in to",
           env=["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
                "DEEPSEEK_API_KEY"],
           files=["~/.local/share/opencode/auth.json", "~/.config/opencode/opencode.json"],
           install_hint="npm i -g opencode-ai; then `opencode auth login`"),
    Preset("cline", "cline", "cline", ["architecture", "coding", "testing", "review"], "run-tested",
           "Cline CLI (`cline --yolo`), any provider Cline is configured for",
           files=["~/.cline/data/settings/providers.json"],
           install_hint="npm i -g cline; then `cline auth`"),
    Preset("codex", "codex", "codex", ["architecture", "coding", "testing", "review"], "run-tested",
           "OpenAI Codex (`codex exec`), keeping its own sandbox (workspace-write / read-only) inside MultiGenz's",
           env=["OPENAI_API_KEY", "CODEX_API_KEY"],
           files=["~/.codex/auth.json", "~/.codex/config.toml"],
           install_hint="npm i -g @openai/codex; then `codex login`"),
    Preset("agy", "antigravity", "agy", ["research", "architecture"], "experimental",
           "Google Antigravity (`agy --print`); candidate flags from `agy --help` 1.3.3, never run in development",
           invocations=None),
]}


def _expand(p: str) -> str:
    return str(Path(p).expanduser())


def _sandbox_dst(p: str) -> str:
    rel = p[2:] if p.startswith("~/") else p.lstrip("/")
    return f"{SANDBOX_HOME}/{rel}"


def preset_invocations(preset: Preset) -> list[dict] | None:
    if preset.key == "agy":
        return _agy_invocations()
    return preset.invocations


def render_agent(preset: Preset, *, name: str | None = None, command: str | None = None) -> str:
    """YAML for one ``agents:`` entry (2-space indented). Values are JSON, which is valid YAML flow syntax."""
    name = name or preset.agent
    lines = [f"  {name}:",
             f"    # preset {preset.key} ({preset.status}): {preset.summary}",
             "    enabled: true",
             "    adapter: cli",
             f"    command: {json.dumps(command or preset.command)}",
             f"    capabilities: {json.dumps(preset.capabilities)}"]
    if preset.env or preset.files:
        lines.append("    credentials:   # handed to THIS agent only; missing ones are skipped; files are read-only")
        if preset.env:
            lines.append(f"      env: {json.dumps(preset.env)}")
        if preset.files:
            lines.append("      files:")
            for f in preset.files:
                lines.append(f"        - {{src: {json.dumps(f)}, dst: {json.dumps(_sandbox_dst(f))}}}")
    invs = preset_invocations(preset)
    if invs:
        lines.append("    invocations:")
        for inv in invs:
            lines.append(f"      - name: {json.dumps(inv['name'])}")
            for k in ("argv", "stdin", "help_args", "requires_help_tokens", "modes"):
                if k in inv:
                    lines.append(f"        {k}: {json.dumps(inv[k])}")
    return "\n".join(lines) + "\n"


def insert_agent(text: str, block: str, name: str) -> str:
    """Insert an agent block at the end of the top-level ``agents:`` mapping (comments elsewhere are kept)."""
    lines = text.splitlines(keepends=True)
    if text and not text.endswith("\n"):
        lines[-1] += "\n"
    if any(re.match(rf"^  {re.escape(name)}:", ln) for ln in lines):
        raise ValueError(f"agents.{name} already exists")
    start = next((i for i, ln in enumerate(lines) if re.match(r"^agents:\s*(\{\}\s*)?(#.*)?$", ln)), None)
    if start is None:
        return "".join(lines) + "agents:\n" + block
    if re.match(r"^agents:\s*\{\}", lines[start]):
        lines[start] = "agents:\n"
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "#"))):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = block.splitlines(keepends=True)
    return "".join(lines)


def add_preset(path: Path, key: str, *, name: str | None = None, command: str | None = None) -> str:
    """Add a preset to a policy file: backup, edit, validate with the real loader, restore on failure."""
    if key not in PRESETS:
        raise ValueError(f"unknown preset {key!r}; choose from {', '.join(PRESETS)}")
    preset = PRESETS[key]
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no policy file (create one with `multigenz init`)")
    original = path.read_text(encoding="utf-8")
    new = insert_agent(original, render_agent(preset, name=name, command=command), name or preset.agent)
    backup = path.with_name(path.name + ".bak")
    shutil.copy(path, backup)
    path.write_text(new, encoding="utf-8")
    try:
        load_policy(path)
    except Exception:
        shutil.copy(backup, path)
        raise
    return str(backup)


def detect_agents(path_env: str | None = None) -> list[str]:
    """Preset keys whose command is on PATH."""
    return [k for k, p in PRESETS.items() if shutil.which(p.command, path=path_env)]


def policy_template(*, python: str | None = None) -> str:
    py = python or shutil.which("python3") or "/usr/bin/python3"
    return f"""# MultiGenz policy (created by `multigenz init`). Docs: docs/OPERATIONS.md
version: 1
orchestrator:
  mode: autonomous
  max_parallel_agents: 1          # raise once your agents run reliably; every agent run uses your plan/API quota
  max_task_attempts: 3
  task_timeout_seconds: 1200
  run_budget_usd: 5.00            # only enforced where cost is measurable (subscription CLIs report "unknown")
agents: {{}}
execution:
  isolation: userns               # rootless Linux namespaces; never falls back to unsandboxed execution
  network: host                   # cloud models need the network. UNRESTRICTED egress for agents (docs/SECURITY.md)
  readonly_paths: []              # extra host paths an agent's install needs (npm/node installs are found automatically)
git:
  auto_integrate: true
  auto_push: false
  protect_default_branch: true
validation:
  require_tests: true
  require_policy_check: true
  require_diff_review: true
  # The ONLY commands used to verify agent work: fixed argv, run in a no-network sandbox on an exported copy.
  # Adjust for your project (e.g. ["/usr/bin/npm", "test"]). Planner-suggested commands are never executed.
  default_profile: tests
  integration_profile: tests
  profiles:
    tests:
      commands:
        - argv: [{json.dumps(py)}, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
          timeout_seconds: 900
      readonly_paths: []          # e.g. your virtualenv, if the tests need it
"""


def init_policy(path: Path, *, agents: list[str] | None = None, overwrite: bool = False) -> list[str]:
    """Write a starter policy with the given (or detected) presets. Returns the agents added."""
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (use --overwrite to replace it; a .bak copy is kept)")
    keys = agents if agents is not None else detect_agents()
    unknown = [k for k in keys if k not in PRESETS]
    if unknown:
        raise ValueError(f"unknown preset(s): {', '.join(unknown)}")
    added: list[str] = []
    text = policy_template()
    for k in keys:
        p = PRESETS[k]
        text = insert_agent(text, render_agent(p), p.agent)
        added.append(k)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        shutil.copy(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    load_policy(tmp)  # never write an invalid policy
    os.replace(tmp, path)
    return added
