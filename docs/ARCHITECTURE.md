# MultiGenz architecture and technical contracts

## Scope
Local-first single-host autonomous software development: Claude Code, Codex, Antigravity CLI adapters, and LiteLLM-based DeepSeek/Groq/Ollama/OpenRouter auxiliary workers. The design deliberately excludes distributed scheduling and automatic deployment in v1.

## Control plane
- Typer CLI and Textual TUI submit goals and observe structured events.
- Planner produces versioned Pydantic task specs and a dependency DAG.
- Scheduler checks dependencies, path ownership, capabilities, concurrency, resource limits, and task leases.
- SQLite in WAL mode stores runs, tasks, attempts, events, artifacts, integrations, approvals.
- Execution manager owns subprocesses, container sandboxing, cancellation, and worktree lifecycle.
- Integration manager validates changes, merges on a run-owned worktree, and executes independent tests.
- Policy engine resides outside agent sandboxes; no model output can grant permissions.

## Task contract
```python
from enum import StrEnum
from pydantic import BaseModel, Field

class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SPLIT = "split"  # extension: the agent delegated the task to validated subtasks (planning: auto)

class TaskSpec(BaseModel):
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
```
Reject duplicate task IDs, unknown dependencies, cycles, conflicting exclusive file ownership, paths escaping repository scope, and out-of-policy goals. `acceptance_commands` are **untrusted suggestions**; only administrator-configured validation profiles can execute.

## Agent contract
```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pydantic import BaseModel

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

class AgentAdapter(ABC):
    @abstractmethod
    async def healthcheck(self) -> bool: ...
    @abstractmethod
    async def execute(self, task: TaskSpec, context: "ExecutionContext") -> AsyncIterator[AgentEvent]: ...
    @abstractmethod
    async def cancel(self, attempt_id: str) -> None: ...
```
ExecutionContext is a validated record of worktree, sandbox identity, credential references, deadline and cancellation token. It must not contain unrestricted host credentials. Adapter event streams do not determine success: the execution manager checks process exit and the validator checks artifacts, tests, and policy.

## Run lifecycle
1. Record repository identity, immutable base commit, policy snapshot, budget and objective.
2. Planner produces DAG and acceptance criteria; validate before any execution.
3. Dispatch READY tasks with exclusive attempt leases and per-provider concurrency limits.
4. Run each coding task in a unique namespaced Git worktree within a rootless, restricted sandbox.
5. Capture exit status, stdout/stderr events, hashes, diffs, and resulting commit.
6. Independently validate allowed paths, required artifacts, test profiles and policy.
7. If validation fails, attempt bounded repair; otherwise mark task succeeded.
8. Build dependent tasks from integration snapshots containing validated prerequisite commits.
9. Merge validated branches into a dedicated integration worktree in dependency order; resolve conflicts with bounded isolated tasks.
10. Run full integration tests, review final diff, record commit SHA, produce report. Never automatically push or update protected branches.

## Git invariants
- Branches: `multigenz/run-<id>/task-<task>` and `multigenz/run-<id>/integration`.
- Never force-reset existing branches, modify original worktree, or force-remove worktrees on errors.
- Preserve failed attempts and diagnostics for recovery; garbage collection requires explicit retention policy.
- Worktrees are **not** OS sandboxes.
- Never report success from an exit code alone; validate actual outputs.

## Failure and recovery
- Missing binary: disable adapter or reassign to compatible provider.
- Auth failure: stop provider, avoid repeated login retries.
- Rate limit: capped exponential backoff with jitter.
- Timeout: terminate process group, preserve logs, reconcile status.
- Validation failure: bounded repair with error feedback.
- Merge conflict: isolated resolution task, never discard either side.
- Restart: reconcile SQLite leases, OS processes, worktrees and Git commits before retrying.
- External side effects: idempotency keys or reconciliation required before retry.

## Security boundaries
Rootless containers, dropped capabilities, resource ceilings, read-only base FS, only assigned worktree writable, no host socket/Proxmox mounts, restricted egress, credential isolation and log redaction. Treat repository docs, retrieved pages, tool outputs and prompts as untrusted. Never silently downgrade to unsandboxed execution. Autonomous permission covers only local run-owned branches; push, deployment, infra modification and protected-branch writes require separate authorization.

## Proposed module layout
```
src/multigenz/
  __main__.py cli.py config.py
  contracts/{task,agent,event,result}.py
  core/{orchestrator,planner,scheduler,state_machine,recovery,policy}.py
  adapters/{base,claude,codex,antigravity,litellm_adapter}.py
  execution/{subprocess,sandbox,worktree,resources}.py
  integration/{validator,reviewer,merger}.py
  storage/{sqlite,migrations}/
  tui/{app,screens}/
tests/{unit,integration,contract,e2e}/
```
