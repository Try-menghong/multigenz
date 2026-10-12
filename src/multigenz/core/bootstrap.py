"""Wire policy, redaction, sandbox backend, runner and adapters together."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.adapters.registry import build_adapters
from multigenz.config import Policy
from multigenz.contracts.agent import AdapterProbe, AgentAdapter
from multigenz.core.policy import PolicyEngine
from multigenz.core.redaction import Redactor
from multigenz.execution.manager import ExecutionRunner
from multigenz.execution.sandbox import SandboxBackend, select_backend


@dataclass
class Services:
    policy: Policy
    engine: PolicyEngine
    redactor: Redactor
    backend: SandboxBackend
    runner: ExecutionRunner
    adapters: dict[str, AgentAdapter]
    probes: dict[str, AdapterProbe] = field(default_factory=dict)

    def usable(self, mode: str) -> list[str]:
        return [n for n, p in self.probes.items() if p.enabled and mode in p.modes]


async def build_services(policy: Policy, *, environ: Mapping[str, str] | None = None,
                         litellm_completion: Callable[..., Awaitable[Any]] | None = None) -> Services:
    """Select the sandbox (fail closed), build adapters and run capability probes."""
    from multigenz.providers import merged_environ, read_secrets

    # API keys stored with `multigenz providers` (0600 secrets file) count as environment; the real environment wins
    env = merged_environ() if environ is None else dict(environ)
    redactor = Redactor.from_environ(env, extra=read_secrets().values() if environ is None else ())
    backend = await select_backend(policy)
    runner = ExecutionRunner(policy, backend, redactor, environ=env)
    adapters = build_adapters(policy, litellm_completion=litellm_completion, environ=env)
    probes: dict[str, AdapterProbe] = {}
    for name, ad in adapters.items():
        if isinstance(ad, CliAdapter):
            ad.bind_runner(runner)
        probes[name] = await ad.probe()
    return Services(policy=policy, engine=PolicyEngine(policy), redactor=redactor, backend=backend,
                    runner=runner, adapters=adapters, probes=probes)
