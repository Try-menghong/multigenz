"""Agent contract (docs/ARCHITECTURE.md "Agent contract")."""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from multigenz.contracts.task import TaskSpec

CRED_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class AgentEvent(BaseModel):
    run_id: str
    task_id: str
    attempt_id: str
    sequence: int
    kind: str
    message: str
    timestamp: str


class AgentResult(BaseModel):
    task_id: str
    attempt_id: str
    exit_code: int | None
    commit_sha: str | None
    artifacts: list[str]
    success: bool
    error_code: str | None = None


class CancellationToken:
    """Cooperative cancellation shared between orchestrator, adapter and process manager."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class ExecutionContext(BaseModel):
    """Validated record handed to an adapter.

    Holds *references* to credentials (names only), never their values, and no handles
    to the host environment.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: str
    task_id: str
    attempt_id: str
    attempt_number: int = 1
    worktree: Path
    sandbox_id: str
    sandbox_backend: str
    credential_refs: tuple[str, ...] = ()
    deadline: float  # epoch seconds
    cancel: CancellationToken
    log_dir: Path
    inbox: Path | None = None  # host dir with prompt/task files (mounted read-only)
    feedback: str | None = None  # untrusted validation feedback from a previous attempt
    extra_prompt: str | None = None  # additional untrusted context (e.g. diff to review)
    mode: Literal["code", "plan", "review", "analysis", "conflict"] = "code"

    @field_validator("worktree", "log_dir", "inbox")
    @classmethod
    def _abs(cls, v: Path | None) -> Path | None:
        if v is not None and not v.is_absolute():
            raise ValueError("path must be absolute")
        return v

    @field_validator("credential_refs")
    @classmethod
    def _refs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for r in v:
            if not CRED_REF_RE.match(r):
                raise ValueError(f"credential reference {r!r} is not a plain name")
        return v

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.time())


class AdapterProbe(BaseModel):
    """Outcome of capability detection for an adapter."""

    name: str
    enabled: bool
    reason: str = ""
    binary: str | None = None
    version: str | None = None
    invocation: str | None = None  # name of the verified invocation, if any
    capabilities: list[str] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)  # execution modes with a verified invocation
    warnings: list[str] = Field(default_factory=list)


class AgentAdapter(ABC):
    """Provider-neutral adapter. Event streams never decide success."""

    name: str
    provider_class: Literal["cli", "api"] = "cli"
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    async def healthcheck(self) -> bool: ...

    @abstractmethod
    async def execute(self, task: TaskSpec, context: ExecutionContext) -> AsyncIterator[AgentEvent]: ...

    @abstractmethod
    async def cancel(self, attempt_id: str) -> None: ...

    # -- extensions used by the execution manager -------------------------------------
    async def probe(self) -> AdapterProbe:  # pragma: no cover - overridden
        return AdapterProbe(name=self.name, enabled=await self.healthcheck())

    def result(self, attempt_id: str) -> AgentResult | None:
        return None

    def output_text(self, attempt_id: str) -> str:
        """Captured (redacted) stdout text of the attempt, for planners/reviewers."""
        return ""
