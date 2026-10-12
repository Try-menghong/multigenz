"""Which models an agent can be switched to, discovered from the tool itself wherever possible.

* API-provider agents: the provider's own model list (``GET <base_url>/models``).
* Claude Code: the aliases its ``--help`` names for ``--model`` (plus any full model name).
* opencode: ``opencode models`` (every model of every provider opencode is set up for).
* other CLIs (Codex, Cline, Hermes): no listing command is known, so the model name is typed; the CLI itself rejects an
  unknown one on its first run.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field

from multigenz.config import AgentCfg, Policy


@dataclass
class ModelChoices:
    models: list[str] = field(default_factory=list)
    note: str = ""
    free_text: bool = True  # a name not in the list may be typed


async def _run(argv: list[str], timeout: float = 45.0) -> tuple[int | None, str]:
    """Run the administrator's own CLI to list models (its normal environment: it may read its own config/logins)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "NO_COLOR": "1", "CI": "1", "TERM": "dumb"}, start_new_session=True)
    except OSError as exc:
        return None, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "timed out"
    return proc.returncode, out.decode("utf-8", "replace")


def claude_aliases(help_text: str) -> list[str]:
    """The model aliases Claude Code's own help names for --model, e.g. ('fable', 'opus', 'sonnet')."""
    m = re.search(r"--model <model>(.*?)(?:\n\s{2}-|\Z)", help_text, re.S)
    if not m:
        return []
    return sorted(dict.fromkeys(re.findall(r"'([a-z][a-z0-9.-]{1,30})'", m.group(1))))


def cli_kind(name: str, cfg: AgentCfg) -> str:
    """Which CLI an agent is (claude, codex, opencode, cline, hermes, agy), by agent name or command basename."""
    from multigenz.adapters.registry import cli_class

    cls = cli_class(name, cfg.command)
    for kind, c in (("claude", "ClaudeAdapter"), ("codex", "CodexAdapter"), ("opencode", "OpencodeAdapter"),
                    ("cline", "ClineAdapter"), ("hermes", "HermesAdapter"), ("agy", "AntigravityAdapter")):
        if cls.__name__ == c:
            return kind
    return os.path.basename(cfg.command or "") or name


async def model_choices(name: str, cfg: AgentCfg, policy: Policy) -> ModelChoices:
    if cfg.provider:
        from multigenz.providers import ProviderError, fetch_models, key_for

        prov = policy.providers[cfg.provider]
        if prov.api_key_env and not key_for(prov):
            return ModelChoices(note=f"no API key for {cfg.provider}: set {prov.api_key_env} or add it in Providers")
        try:
            ids = await asyncio.to_thread(fetch_models, prov, key_for(prov))
        except ProviderError as exc:
            return ModelChoices(note=str(exc))
        return ModelChoices(ids, f"{len(ids)} models served by {cfg.provider}", free_text=False)
    if cfg.adapter == "litellm":
        return ModelChoices(note="LiteLLM model string, e.g. openai/gpt-4o-mini or deepseek/deepseek-chat")
    binary = cfg.command if cfg.command and os.path.isabs(cfg.command) else shutil.which(cfg.command or "")
    kind = cli_kind(name, cfg)
    if not binary:
        return ModelChoices(note=f"{cfg.command!r} is not installed; type a model name")
    if kind == "claude":
        rc, text = await _run([binary, "--help"], timeout=20)
        aliases = claude_aliases(text) if rc is not None else []
        return ModelChoices(aliases, "aliases from `claude --help` (each means the latest model of that family); "
                                     "a full model name also works")
    if kind == "opencode":
        rc, text = await _run([binary, "models"])
        ids = [ln.strip() for ln in text.splitlines() if re.match(r"^[\w.-]+/\S+$", ln.strip())]
        if ids:
            return ModelChoices(ids, f"{len(ids)} models from `opencode models` (provider/model)")
        return ModelChoices(note="`opencode models` listed nothing; type provider/model")
    hint = {"codex": "e.g. gpt-5-codex (whatever your Codex login offers)",
            "cline": "a model id of the provider Cline is set up for (see `cline auth`)",
            "hermes": "e.g. anthropic/claude-sonnet-4.5 or a model of your Hermes provider",
            "agy": "a model your Antigravity login offers"}.get(kind, "")
    return ModelChoices(note=f"{kind} has no command that lists models; type one {hint}".strip())


@dataclass
class ModelCheck:
    ok: bool        # False: the model cannot be passed to this agent, so it must not be saved
    message: str


async def check_model(name: str, cfg: AgentCfg, model: str | None) -> ModelCheck:
    """Before saving ``agents.<name>.model``: can this agent actually be told to use ``model``?

    For a CLI agent this probes the binary's help with the model set (cheap, nothing is run), so a CLI whose help shows
    no model option is refused instead of silently running with its default model.
    """
    if model is None:
        if cfg.provider or cfg.adapter == "litellm":
            return ModelCheck(False, f"{name} talks to an API directly and needs a model")
        return ModelCheck(True, f"{name} will use its CLI's own default model")
    if cfg.provider:
        return ModelCheck(True, f"{name} will use {cfg.provider} model {model} (through the opencode harness)"
                          if cfg.adapter == "cli" else f"{name} will call {cfg.provider} model {model} directly")
    if cfg.adapter == "litellm":
        return ModelCheck(True, f"{name} will call LiteLLM model {model}")
    from multigenz.adapters.registry import cli_class

    adapter = cli_class(name, cfg.command)(name, cfg.model_copy(update={"model": model, "enabled": True}))
    probe = await adapter.probe()
    if adapter.model_problem:
        return ModelCheck(False, adapter.model_problem)
    if not probe.enabled:
        return ModelCheck(True, f"saved for later: {name} cannot be checked now ({probe.reason})")
    how = f"{adapter.MODEL_FLAG} {model}" if adapter.MODEL_FLAG else model
    return ModelCheck(True, f"{name} will run as `{os.path.basename(probe.binary or cfg.command or name)} ... {how}` "
                            "(option verified in its help)")


async def set_model(policy_path, agent: str, model: str | None) -> tuple[ModelCheck, Policy | None]:
    """Check, then save ``agents.<agent>.model`` (None: back to the CLI's default). Returns (result, saved policy)."""
    from pathlib import Path

    from multigenz.config import load_policy
    from multigenz.policy_edit import save_policy_edits, short_error

    path = Path(policy_path)
    try:
        pol = load_policy(path)
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return ModelCheck(False, f"policy problem: {short_error(exc)}"), None
    if agent not in pol.agents:
        return ModelCheck(False, f"no agent {agent!r} (agents: {', '.join(pol.agents)})"), None
    model = model.strip() if model else None
    check = await check_model(agent, pol.agents[agent], model)
    if not check.ok:
        return check, None
    try:
        saved, _ = save_policy_edits(path, models={agent: model})
    except Exception as exc:  # noqa: BLE001
        return ModelCheck(False, f"not saved: {short_error(exc)}"), None
    return check, saved
