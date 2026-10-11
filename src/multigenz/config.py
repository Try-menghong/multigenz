"""Host policy / configuration (config/policy.example.yaml).

The policy is administrator-owned. It is parsed strictly (unknown keys are errors) and fails
closed: invalid policy means nothing runs. Nothing a model emits can change it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from multigenz.errors import ConfigError

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrchestratorCfg(_Strict):
    mode: Literal["autonomous", "plan-only"] = "autonomous"
    max_parallel_agents: int = Field(3, ge=1, le=16)
    max_api_requests: int = Field(2, ge=1, le=16)
    max_task_attempts: int = Field(2, ge=1, le=5)
    task_timeout_seconds: int = Field(900, ge=1)
    run_budget_usd: float | None = Field(5.0, ge=0)
    max_conflict_repairs: int = Field(1, ge=0, le=3)
    max_plan_attempts: int = Field(2, ge=1, le=5)
    max_interruptions: int = Field(3, ge=0, le=10)
    rate_limit_retries: int = Field(3, ge=0, le=10)
    backoff_base_seconds: float = Field(2.0, gt=0)
    backoff_cap_seconds: float = Field(60.0, gt=0)
    kill_grace_seconds: float = Field(5.0, ge=0)
    lease_seconds: int = Field(60, ge=5)


class InvocationCfg(_Strict):
    """Administrator-defined non-interactive invocation of a CLI agent.

    Used only after ``requires_help_tokens`` are all present in the binary's own help output.
    Placeholders: {command} {prompt_file} {task_file} {task_id} {workspace}.
    """

    name: str
    argv: list[str]
    stdin: Literal["none", "prompt"] = "none"
    requires_help_tokens: list[str] = Field(default_factory=list)
    help_args: list[str] = Field(default_factory=lambda: ["--help"])
    unattended: bool = True  # agent may modify files without asking => sandbox mandatory
    modes: list[str] = Field(default_factory=lambda: ["code", "conflict", "plan", "review", "analysis"])

    @field_validator("argv")
    @classmethod
    def _argv(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("argv must not be empty")
        return v


class CredentialFile(_Strict):
    src: str
    dst: str  # absolute path inside the sandbox


class CredentialCfg(_Strict):
    """Per-agent credential references. Off by default; nothing else is ever exposed."""

    env: list[str] = Field(default_factory=list)
    files: list[CredentialFile] = Field(default_factory=list)

    @field_validator("env")
    @classmethod
    def _env(cls, v: list[str]) -> list[str]:
        for n in v:
            if not ENV_NAME_RE.match(n):
                raise ValueError(f"invalid env var name {n!r}")
        return v


class AgentCfg(_Strict):
    enabled: bool = True
    adapter: Literal["cli", "litellm"]
    command: str | None = None
    model: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    max_concurrent: int | None = Field(None, ge=1)
    invocations: list[InvocationCfg] = Field(default_factory=list)
    credentials: CredentialCfg = Field(default_factory=CredentialCfg)
    rate_limit_markers: list[str] = Field(default_factory=lambda: ["rate limit", "429", "too many requests"])
    auth_failure_markers: list[str] = Field(
        default_factory=lambda: ["not logged in", "unauthorized", "invalid api key", "authentication failed", "401"]
    )
    cost_per_task_usd: float | None = None  # only for adapters whose cost is measurable

    @model_validator(mode="after")
    def _check(self) -> "AgentCfg":
        if self.adapter == "cli" and not self.command:
            raise ValueError("cli adapter requires 'command'")
        if self.adapter == "litellm" and not self.model:
            raise ValueError("litellm adapter requires 'model'")
        return self


class ResourceLimitsCfg(_Strict):
    max_file_mb: int = Field(512, ge=1)
    max_open_files: int = Field(4096, ge=16)
    max_processes: int | None = Field(None, ge=1)
    cpu_seconds: int | None = Field(None, ge=1)  # default: task timeout
    address_space_mb: int | None = Field(None, ge=64)
    memory_mb: int = Field(4096, ge=64)  # enforced by container backend
    tmp_mb: int = Field(512, ge=1)
    pids_limit: int = Field(512, ge=8)


class ExecutionCfg(_Strict):
    isolation: Literal["rootless-container", "userns", "none"] = "rootless-container"
    network: Literal["none", "restricted", "host"] = "restricted"
    allow_unsandboxed: bool = False
    container_runtime: Literal["auto", "podman", "docker"] = "auto"
    container_image: str | None = None
    container_network: str | None = None
    readonly_paths: list[str] = Field(default_factory=list)
    limits: ResourceLimitsCfg = Field(default_factory=ResourceLimitsCfg)

    @model_validator(mode="after")
    def _check(self) -> "ExecutionCfg":
        if self.isolation == "none" and not self.allow_unsandboxed:
            raise ValueError("isolation 'none' requires explicit allow_unsandboxed: true")
        return self


class GitCfg(_Strict):
    auto_integrate: bool = True
    auto_push: bool = False
    protect_default_branch: bool = True

    @model_validator(mode="after")
    def _check(self) -> "GitCfg":
        if self.auto_push:
            raise ValueError("auto_push is not supported: pushing requires separate authorization")
        if not self.protect_default_branch:
            raise ValueError("protect_default_branch must remain true")
        return self


class ProfileCommand(_Strict):
    argv: list[str]
    timeout_seconds: int = Field(300, ge=1)

    @field_validator("argv")
    @classmethod
    def _argv(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("argv must not be empty")
        return v


class ValidationProfile(_Strict):
    """Administrator-defined test profile: fixed argv lists, no shell, no planner input."""

    commands: list[ProfileCommand]
    readonly_paths: list[str] = Field(default_factory=list)

    @field_validator("commands")
    @classmethod
    def _cmds(cls, v: list[ProfileCommand]) -> list[ProfileCommand]:
        if not v:
            raise ValueError("profile needs at least one command")
        return v


class ValidationCfg(_Strict):
    require_tests: bool = True
    require_policy_check: bool = True
    require_diff_review: bool = True
    default_profile: str | None = None
    integration_profile: str | None = None
    profiles: dict[str, ValidationProfile] = Field(default_factory=dict)
    max_changed_files: int = Field(200, ge=1)
    max_file_bytes: int = Field(1_000_000, ge=1)
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".gitmodules", ".github/workflows/", ".gitlab-ci.yml"]
    )

    @model_validator(mode="after")
    def _check(self) -> "ValidationCfg":
        for ref in (self.default_profile, self.integration_profile):
            if ref is not None and ref not in self.profiles:
                raise ValueError(f"unknown validation profile {ref!r}")
        return self


DEFAULT_DENIED_GOALS = [
    r"\bgit\s+push\b",
    r"\bforce[- ]?push\b",
    r"\bpush\b.{0,40}\b(origin|remote|upstream|main|master)\b",
    r"\bdeploy(ment|ing|s)?\b",
    r"\bterraform\s+(apply|destroy)\b",
    r"\bkubectl\b",
    r"\bproxmox\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\brm\s+-rf?\s+/(?!\w)",
    r"\b(curl|wget)\b[^|\n]*\|\s*(ba|z)?sh\b",
    r"\b(exfiltrat|steal)\w*",
    r"\b(ssh|aws|gcloud|az)\s+(key|credential)s?\b",
]


class GoalsCfg(_Strict):
    denied_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_DENIED_GOALS))
    max_objective_chars: int = Field(8000, ge=100)

    @field_validator("denied_patterns")
    @classmethod
    def _re(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(f"bad regex {p!r}: {exc}") from exc
        return v


class Policy(_Strict):
    version: int = 1
    orchestrator: OrchestratorCfg = Field(default_factory=OrchestratorCfg)
    agents: dict[str, AgentCfg] = Field(default_factory=dict)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    git: GitCfg = Field(default_factory=GitCfg)
    validation: ValidationCfg = Field(default_factory=ValidationCfg)
    goals: GoalsCfg = Field(default_factory=GoalsCfg)

    @field_validator("version")
    @classmethod
    def _v(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported policy version {v}")
        return v

    @field_validator("agents")
    @classmethod
    def _names(cls, v: dict[str, AgentCfg]) -> dict[str, AgentCfg]:
        for n in v:
            if not NAME_RE.match(n):
                raise ValueError(f"invalid agent name {n!r}")
        return v

    def snapshot(self) -> tuple[str, str]:
        """Canonical JSON + sha256, stored with each run (policy snapshot)."""
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return text, hashlib.sha256(text.encode()).hexdigest()

    def enabled_agents(self) -> dict[str, AgentCfg]:
        return {n: a for n, a in self.agents.items() if a.enabled}


def default_state_home() -> Path:
    env = os.environ.get("MULTIGENZ_HOME")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser().resolve() / "multigenz"


def default_policy_path() -> Path | None:
    env = os.environ.get("MULTIGENZ_POLICY")
    if env:
        return Path(env).expanduser()
    cand = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "multigenz" / "policy.yaml"
    return cand if cand.is_file() else None


def parse_policy(data: object) -> Policy:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("policy root must be a mapping")
    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid policy: {exc}") from exc


def load_policy(path: Path | str | None = None) -> Policy:
    p = Path(path).expanduser() if path else default_policy_path()
    if p is None:
        return parse_policy({})
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read policy {p}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"policy {p} is not valid YAML: {exc}") from exc
    return parse_policy(data)
