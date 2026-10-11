"""Build adapter instances from the policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from multigenz.adapters.antigravity import AntigravityAdapter
from multigenz.adapters.claude import ClaudeAdapter
from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.adapters.codex import CodexAdapter
from multigenz.adapters.litellm_adapter import LiteLLMAdapter
from multigenz.config import Policy
from multigenz.contracts.agent import AgentAdapter

CLI_CLASSES: dict[str, type[CliAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "antigravity": AntigravityAdapter,
}


def build_adapters(policy: Policy, *,
                   litellm_completion: Callable[..., Awaitable[Any]] | None = None) -> dict[str, AgentAdapter]:
    out: dict[str, AgentAdapter] = {}
    for name, cfg in policy.agents.items():
        if cfg.adapter == "litellm":
            out[name] = LiteLLMAdapter(name, cfg, completion=litellm_completion)
        else:
            out[name] = CLI_CLASSES.get(name, CliAdapter)(name, cfg)
    return out
