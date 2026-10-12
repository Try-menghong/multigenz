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
                 completion: Callable[..., Awaitable[Any]] | None = None, provider=None, environ=None):
        self.name = name
        self.cfg = cfg
        self.provider = provider  # ProviderCfg when the agent uses an API provider from `providers:`
        self._environ = environ
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
        if self.provider is not None:
            if self.provider.api_key_env and not self._key():
                return AdapterProbe(name=self.name, enabled=False,
                                    reason=f"no API key: set {self.provider.api_key_env} or add it with "
                                           "`multigenz providers add` / the TUI")
        elif self._completion is None:
            try:
                import litellm

                v = litellm.validate_environment(self.cfg.model)
                if not v.get("keys_in_environment", False) and v.get("missing_keys"):
                    return AdapterProbe(name=self.name, enabled=False,
                                        reason=f"missing credentials in host environment: {v['missing_keys']}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"could not validate environment: {exc}")
        return AdapterProbe(name=self.name, enabled=True, invocation=f"litellm:{self._model()}",
                            capabilities=sorted(self.capabilities), modes=sorted(READ_MODES), warnings=warnings)

    def _key(self) -> str | None:
        from multigenz.providers import key_for

        return key_for(self.provider, self._environ) if self.provider is not None else None

    def _model(self) -> str:
        from multigenz.providers import litellm_model

        assert self.cfg.model
        return litellm_model(self.provider, self.cfg.model) if self.provider is not None else self.cfg.model

    def _call_kwargs(self) -> dict:
        if self.provider is None:
            return {}
        kw: dict = {"api_base": self.provider.base_url}
        key = self._key()
        if key:
            kw["api_key"] = key
        elif not self.provider.api_key_env:
            kw["api_key"] = "not-needed"  # local servers ignore it; client libraries insist on one
        return kw

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
        yield ev("started", f"litellm model={self._model()}")
        code: str | None = None
        text = ""
        cost: float | None = None
        try:
            import asyncio

            resp = await asyncio.wait_for(
                self._get_completion()(model=self._model(), messages=[{"role": "user", "content": prompt}],
                                       **self._call_kwargs()),
                timeout=max(1.0, context.remaining_seconds))
            text = resp["choices"][0]["message"]["content"] or ""
            if isinstance(resp, dict) and resp.get("_multigenz_cost") is not None:
                cost = float(resp["_multigenz_cost"])
            else:
                try:
                    import litellm

                    cost = float(litellm.completion_cost(completion_response=resp))
                except Exception:  # noqa: BLE001 - unmeasurable stays unknown
                    cost = None
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
