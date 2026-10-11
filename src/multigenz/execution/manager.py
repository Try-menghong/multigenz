"""Execution manager: sandboxed command runner + single-attempt executor."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from multigenz.adapters.base import AdapterCommand
from multigenz.config import Policy
from multigenz.contracts.agent import (
    AgentAdapter,
    AgentEvent,
    AgentResult,
    CancellationToken,
    ExecutionContext,
)
from multigenz.contracts.task import TaskSpec
from multigenz.core.redaction import Redactor
from multigenz.errors import PolicyViolation
from multigenz.execution.git import Git
from multigenz.execution.resources import ResourceLimits
from multigenz.execution.sandbox import SandboxBackend, SandboxRequest
from multigenz.execution.subprocess import CommandSpec, ProcessEvent, ProcessManager, ProcessStart
from multigenz.execution.worktree import Worktree


class ExecutionRunner:
    """Runs commands inside the configured sandbox. Implements ``CommandRunner``."""

    def __init__(self, policy: Policy, backend: SandboxBackend, redactor: Redactor,
                 *, environ: dict[str, str] | None = None):
        self.policy = policy
        self.backend = backend
        self.redactor = redactor
        self.pm = ProcessManager(redactor)
        self._environ = dict(os.environ if environ is None else environ)
        self.on_process_start: Callable[[str, ProcessStart], None] | None = None

    @property
    def sandboxed(self) -> bool:
        return self.backend.sandboxed

    def workspace_path(self, ctx: ExecutionContext) -> str:
        return self.backend.workspace_path(ctx.worktree)

    def inbox_path(self, ctx: ExecutionContext) -> str:
        assert ctx.inbox is not None
        return self.backend.inbox_path(ctx.inbox)

    # -- credentials ---------------------------------------------------------------------
    def resolve_credentials(self, agent: str, refs: Sequence[str]) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """Resolve credential *references* for one agent. Only allowlisted names are exposed."""
        cfg = self.policy.agents.get(agent)
        allowed = set(cfg.credentials.env) if cfg else set()
        env: dict[str, str] = {}
        for ref in refs:
            if ref not in allowed:
                raise PolicyViolation(f"credential reference {ref!r} not granted to agent {agent!r}")
            val = self._environ.get(ref)
            if val is not None:
                env[ref] = val
                self.redactor.add_secret(val)
        files: list[tuple[str, str]] = []
        if cfg:
            for f in cfg.credentials.files:
                src = str(Path(f.src).expanduser())
                if os.path.exists(src):
                    files.append((src, f.dst))
        return env, files

    # -- generic sandboxed execution -----------------------------------------------------------
    async def run_sandboxed(
        self,
        *,
        argv: list[str],
        workspace: Path,
        state_dir: Path,
        log_dir: Path,
        timeout: float,
        sandbox_id: str,
        label: str,
        inbox: Path | None = None,
        env: dict[str, str] | None = None,
        ro_mounts: list[tuple[str, str]] | None = None,
        stdin_data: bytes | None = None,
        network: str | None = None,
        cancel: CancellationToken | None = None,
        attempt_id: str | None = None,
        log_prefix: str = "",
    ) -> AsyncIterator[ProcessEvent]:
        limits = ResourceLimits.from_cfg(self.policy.execution.limits, timeout_seconds=timeout)
        req = SandboxRequest(argv=argv, env=env or {}, workspace=workspace, state_dir=state_dir,
                             sandbox_id=sandbox_id, inbox=inbox, ro_mounts=ro_mounts or [],
                             network=network or self.policy.execution.network, limits=limits)
        wrapped = self.backend.wrap(req)
        spec = CommandSpec(argv=wrapped.argv, cwd=wrapped.cwd, env=wrapped.env, timeout_seconds=timeout,
                           stdin_data=stdin_data, stdout_log=log_dir / f"{log_prefix}stdout.log",
                           stderr_log=log_dir / f"{log_prefix}stderr.log", label=label,
                           kill_grace_seconds=self.policy.orchestrator.kill_grace_seconds,
                           on_terminate=wrapped.terminate)
        try:
            async for ev in self.pm.stream(spec, cancel=cancel):
                if ev.kind == "started" and ev.start and attempt_id and self.on_process_start:
                    self.on_process_start(attempt_id, ev.start)
                yield ev
        finally:
            wrapped.cleanup()

    async def run_command(self, cmd: AdapterCommand, ctx: ExecutionContext) -> AsyncIterator[ProcessEvent]:
        env = dict(cmd.env)
        cred_env, cred_files = self.resolve_credentials(cmd.agent, ctx.credential_refs)
        env.update(cred_env)
        async for ev in self.run_sandboxed(
            argv=cmd.argv, workspace=ctx.worktree, state_dir=ctx.log_dir.parent / "sandbox",
            log_dir=ctx.log_dir, timeout=max(1.0, ctx.remaining_seconds), sandbox_id=ctx.sandbox_id,
            label=cmd.label, inbox=ctx.inbox, env=env, ro_mounts=cred_files, stdin_data=cmd.stdin_data,
            cancel=ctx.cancel, attempt_id=ctx.attempt_id):
            yield ev


# ---------------------------------------------------------------------------------------
@dataclass
class AttemptOutcome:
    attempt_id: str
    agent_result: AgentResult
    commit_sha: str | None  # branch HEAD if it moved past base (orchestrator-made commit included)
    changed: bool
    events: int = 0
    error_code: str | None = None
    log_dir: Path | None = None
    notes: list[str] = field(default_factory=list)


def new_attempt_id() -> str:
    return "att-" + uuid.uuid4().hex[:12]


def make_context(*, run_id: str, task_id: str, attempt_id: str, worktree: Path, backend: SandboxBackend,
                 attempt_dir: Path, timeout_seconds: float, cancel: CancellationToken,
                 credential_refs: Sequence[str] = (), mode: str = "code", feedback: str | None = None,
                 extra_prompt: str | None = None, attempt_number: int = 1) -> ExecutionContext:
    (attempt_dir / "logs").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "inbox").mkdir(parents=True, exist_ok=True)
    return ExecutionContext(
        run_id=run_id, task_id=task_id, attempt_id=attempt_id, worktree=worktree.resolve(),
        sandbox_id=attempt_id.replace("att-", "")[:12], sandbox_backend=backend.name,
        credential_refs=tuple(credential_refs), deadline=(datetime.now(UTC) + timedelta(seconds=timeout_seconds)).timestamp(),
        cancel=cancel, log_dir=(attempt_dir / "logs").resolve(), inbox=(attempt_dir / "inbox").resolve(),
        feedback=feedback, extra_prompt=extra_prompt, mode=mode,  # type: ignore[arg-type]
        attempt_number=attempt_number)


class AttemptExecutor:
    """Runs one agent attempt in a prepared worktree and snapshots the result in Git.

    The agent never commits (it has no Git access in the sandbox); the orchestrator commits
    whatever is in the worktree afterwards - including partial work from failed, timed-out or
    cancelled attempts - so nothing an agent produced is lost.
    """

    def __init__(self, git: Git, redactor: Redactor):
        self.git = git
        self.redactor = redactor

    async def run(self, *, adapter: AgentAdapter, task: TaskSpec, ctx: ExecutionContext, wt: Worktree,
                  on_event: Callable[[AgentEvent], None] | None = None) -> AttemptOutcome:
        count = 0
        async for ev in adapter.execute(task, ctx):
            count += 1
            if on_event is not None:
                on_event(ev)
        result = adapter.result(ctx.attempt_id) or AgentResult(
            task_id=task.task_id, attempt_id=ctx.attempt_id, exit_code=None, commit_sha=None, artifacts=[],
            success=False, error_code="no_result")
        notes: list[str] = []
        head = self.snapshot(wt, ctx.attempt_id, result.error_code, notes)
        changed = head is not None and head != wt.base_commit
        result = result.model_copy(update={"commit_sha": head if changed else None})
        return AttemptOutcome(attempt_id=ctx.attempt_id, agent_result=result,
                              commit_sha=head if changed else None, changed=changed, events=count,
                              error_code=result.error_code, log_dir=ctx.log_dir, notes=notes)

    def snapshot(self, wt: Worktree, attempt_id: str, error_code: str | None, notes: list[str]) -> str | None:
        """Commit worktree contents on the attempt branch (explicit --git-dir; no hooks)."""
        g = self.git
        try:
            if g.merge_in_progress(wt.gitdir):
                # conflict-resolution attempt: staging + committing completes the merge
                g.stage_all(wt.gitdir, wt.path)
                if g.unmerged_paths(wt.gitdir, wt.path):
                    notes.append("unmerged paths remain")
                    return g.rev_parse("HEAD", git_dir=wt.gitdir, work_tree=wt.path)
                return g.commit(wt.gitdir, wt.path, f"multigenz: merge resolution (attempt {attempt_id})",
                                allow_empty=True)
            g.stage_all(wt.gitdir, wt.path)
            if g.has_staged(wt.gitdir, wt.path):
                status = error_code or "ok"
                return g.commit(wt.gitdir, wt.path, f"multigenz: attempt {attempt_id} snapshot [{status}]")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"snapshot failed: {exc}")
        return g.rev_parse("HEAD", git_dir=wt.gitdir, work_tree=wt.path)
