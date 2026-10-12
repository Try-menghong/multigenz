"""Build adapter instances from the policy."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from multigenz.adapters.antigravity import AntigravityAdapter
from multigenz.adapters.claude import ClaudeAdapter
from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.adapters.cline import ClineAdapter
from multigenz.adapters.codex import CodexAdapter
from multigenz.adapters.litellm_adapter import LiteLLMAdapter
from multigenz.adapters.opencode import OpencodeAdapter
from multigenz.config import Policy
from multigenz.contracts.agent import AgentAdapter

CLI_CLASSES: dict[str, type[CliAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "antigravity": AntigravityAdapter,
    "agy": AntigravityAdapter,
    "opencode": OpencodeAdapter,
    "cline": ClineAdapter,
}


def cli_class(name: str, command: str | None) -> type[CliAdapter]:
    """Adapter class by agent name, else by the command's basename (``my-coder: {command: /x/opencode}``)."""
    if name in CLI_CLASSES:
        return CLI_CLASSES[name]
    base = os.path.basename(command or "")
    return CLI_CLASSES.get(base, CliAdapter)


def build_adapters(policy: Policy, *,
                   litellm_completion: Callable[..., Awaitable[Any]] | None = None) -> dict[str, AgentAdapter]:
    out: dict[str, AgentAdapter] = {}
    for name, cfg in policy.agents.items():
        if cfg.adapter == "litellm":
            out[name] = LiteLLMAdapter(name, cfg, completion=litellm_completion)
        else:
            out[name] = cli_class(name, cfg.command)(name, cfg)
    return out
