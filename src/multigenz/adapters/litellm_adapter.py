"""LiteLLM-backed auxiliary worker (analysis / review / planning text only).

Runs inside the orchestrator process, has no filesystem access to worktrees and therefore cannot
produce commits. Cost is recorded only when LiteLLM can measure it; otherwise it stays unknown.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from multigenz.adapters.base import render_prompt, utcnow
from multigenz.config import AgentCfg
from multigenz.contracts.agent import (
    AdapterProbe,
    AgentAdapter,
    AgentEvent,
    AgentResult,
    ExecutionContext,
)
from multigenz.contracts.task import TaskSpec

READ_MODES = {"plan", "review", "analysis"}


class LiteLLMAdapter(AgentAdapter):
    provider_class = "api"

    def __init__(self, name: str, cfg: AgentCfg,
                 completion: Callable[..., Awaitable[Any]] | None = None):
        self.name = name
        self.cfg = cfg
        self.capabilities = frozenset(cfg.capabilities)
        self._completion = completion
        self._results: dict[str, AgentResult] = {}
        self._outputs: dict[str, str] = {}
        self.costs: dict[str, float | None] = {}

    def _get_completion(self) -> Callable[..., Awaitable[Any]]:
        if self._completion is not None:
            return self._completion
        import litellm  # lazy: optional dependency

        return litellm.acompletion

    async def probe(self) -> AdapterProbe:
        if not self.cfg.enabled:
            return AdapterProbe(name=self.name, enabled=False, reason="disabled in policy")
        if self._completion is None and importlib.util.find_spec("litellm") is None:
            return AdapterProbe(name=self.name, enabled=False, reason="litellm is not installed")
        warnings: list[str] = []
        if self._completion is None:
            try:
                import litellm

                v = litellm.validate_environment(self.cfg.model)
                if not v.get("keys_in_environment", False) and v.get("missing_keys"):
                    return AdapterProbe(name=self.name, enabled=False,
                                        reason=f"missing credentials in host environment: {v['missing_keys']}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not validate environment: {exc}")
        return AdapterProbe(name=self.name, enabled=True, invocation=f"litellm:{self.cfg.model}",
                            capabilities=sorted(self.capabilities), modes=sorted(READ_MODES), warnings=warnings)

    async def healthcheck(self) -> bool:
        return (await self.probe()).enabled

    async def execute(self, task: TaskSpec, context: ExecutionContext) -> AsyncIterator[AgentEvent]:
        seq = 0
        attempt = context.attempt_id

        def ev(kind: str, message: str) -> AgentEvent:
            nonlocal seq
            seq += 1
            return AgentEvent(run_id=context.run_id, task_id=task.task_id, attempt_id=attempt, sequence=seq,
                              kind=kind, message=message, timestamp=utcnow())

        if context.mode not in READ_MODES:
            self._results[attempt] = AgentResult(task_id=task.task_id, attempt_id=attempt, exit_code=None,
                                                 commit_sha=None, artifacts=[], success=False,
                                                 error_code="mode_unsupported")
            yield ev("error", f"LiteLLM worker cannot run mode {context.mode!r} (no filesystem access)")
            return
        prompt = render_prompt(task, context)
        yield ev("started", f"litellm model={self.cfg.model}")
        code: str | None = None
        text = ""
        cost: float | None = None
        try:
            import asyncio

            resp = await asyncio.wait_for(
                self._get_completion()(model=self.cfg.model, messages=[{"role": "user", "content": prompt}]),
                timeout=max(1.0, context.remaining_seconds))
            text = resp["choices"][0]["message"]["content"] or ""
            try:
                import litellm

                cost = float(litellm.completion_cost(completion_response=resp))
            except Exception:  # noqa: BLE001
                cost = (resp.get("_multigenz_cost") if isinstance(resp, dict) else None)
        except TimeoutError:
            code = "timeout"
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            if "ratelimit" in name or "rate limit" in msg or "429" in msg:
                code = "rate_limited"
            elif "authentication" in name or "api key" in msg or "401" in msg:
                code = "auth_failure"
            else:
                code = "provider_error"
            yield ev("error", f"{type(exc).__name__}: {exc}"[:500])
        self.costs[attempt] = cost
        self._outputs[attempt] = text
        for line in text.splitlines():
            yield ev("stdout", line)
        yield ev("exit", f"error_code={code} cost_usd={'unknown' if cost is None else cost}")
        self._results[attempt] = AgentResult(task_id=task.task_id, attempt_id=attempt,
                                             exit_code=0 if code is None else None, commit_sha=None,
                                             artifacts=[], success=code is None, error_code=code)

    async def cancel(self, attempt_id: str) -> None:
        return None

    def result(self, attempt_id: str) -> AgentResult | None:
        return self._results.get(attempt_id)

    def output_text(self, attempt_id: str) -> str:
        return self._outputs.get(attempt_id, "")
