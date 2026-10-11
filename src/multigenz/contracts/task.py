"""Task contract (docs/ARCHITECTURE.md "Task contract")."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from multigenz.contracts.paths import normalize_pattern

TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
)


class TaskSpec(BaseModel):
    """Versioned task specification produced by the planner.

    ``acceptance_commands`` are **untrusted suggestions** and are never executed; only
    administrator-defined validation profiles can run commands.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    task_id: str
    title: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    allowed_paths: list[str]
    required_capabilities: set[str] = Field(default_factory=set)
    acceptance_commands: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1, le=5)
    timeout_seconds: int = Field(default=900, ge=1)
    # --- additive, optional extensions (defaults keep the base contract valid) ---
    shared_paths: list[str] = Field(default_factory=list)  # non-exclusive paths (e.g. README)
    required_artifacts: list[str] = Field(default_factory=list)  # must exist in the result
    validation_profile: str | None = None  # name of an administrator-defined profile

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported task schema_version {v}")
        return v

    @field_validator("task_id")
    @classmethod
    def _task_id(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(f"task_id {v!r} must match {TASK_ID_RE.pattern}")
        return v

    @field_validator("title", "objective")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v

    @field_validator("allowed_paths", "shared_paths")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        return [normalize_pattern(p) for p in v]

    @field_validator("required_artifacts")
    @classmethod
    def _artifacts(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            n = normalize_pattern(p)
            if n.endswith("/"):
                raise ValueError(f"required artifact must be a file, got {p!r}")
            out.append(n)
        return out

    @field_validator("required_capabilities")
    @classmethod
    def _caps(cls, v: set[str]) -> set[str]:
        bad = [c for c in v if not CAPABILITY_RE.match(c)]
        if bad:
            raise ValueError(f"invalid capability names: {sorted(bad)}")
        return v

    @model_validator(mode="after")
    def _check(self) -> "TaskSpec":
        if not self.allowed_paths:
            raise ValueError("allowed_paths must not be empty")
        if self.task_id in self.dependencies:
            raise ValueError("task cannot depend on itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("duplicate dependencies")
        return self

    @property
    def writable_paths(self) -> list[str]:
        return [*self.allowed_paths, *self.shared_paths]


class PlanDocument(BaseModel):
    """Versioned plan emitted by a planner."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    summary: str = ""
    tasks: list[TaskSpec]

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported plan schema_version {v}")
        return v
