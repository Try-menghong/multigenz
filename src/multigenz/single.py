"""Single-agent controlled execution (M1 exit criterion: one validated agent commit)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable

from multigenz.contracts.agent import AgentEvent, CancellationToken
from multigenz.contracts.result import CheckResult
from multigenz.contracts.task import TaskSpec
from multigenz.config import Policy
from multigenz.core.bootstrap import build_services
from multigenz.errors import AdapterUnavailable, GitError, PolicyViolation
from multigenz.execution.git import Git
from multigenz.execution.manager import AttemptExecutor, make_context, new_attempt_id
from multigenz.execution.worktree import WorktreeManager, task_branch
from multigenz.integration.validator import DiffValidator


async def run_single_agent(policy: Policy, *, agent: str, objective: str, repo: Path, allow: list[str],
                           timeout: int | None, home: Path, emit: Callable[[str], None] | None = None,
                           mode: str = "code", services=None) -> dict:
    import json

    svc = services or await build_services(policy)
    svc.engine.check_text(objective, what="objective")
    if agent not in svc.adapters:
        raise AdapterUnavailable(f"unknown agent {agent!r}")
    probe = svc.probes[agent]
    if not probe.enabled:
        raise AdapterUnavailable(f"agent {agent!r} is disabled: {probe.reason}")
    if mode not in probe.modes:
        raise AdapterUnavailable(f"agent {agent!r} has no verified invocation for mode {mode!r}")
    adhoc = uuid.uuid4().hex[:12]
    git = Git(repo, run_id=adhoc)
    base = git.head()
    task = TaskSpec(task_id="adhoc", title=objective[:80], objective=objective, allowed_paths=allow,
                    timeout_seconds=timeout or policy.orchestrator.task_timeout_seconds)
    issues = svc.engine.check_task(task)
    if issues:
        raise PolicyViolation("; ".join(issues))
    run_dir = home / "adhoc" / adhoc
    wts = WorktreeManager(git, run_dir / "worktrees")
    branch = task_branch(adhoc, task.task_id)
    wt = wts.create("task-adhoc", branch, base)
    attempt_id = new_attempt_id()
    cancel = CancellationToken()
    ctx = make_context(run_id=adhoc, task_id=task.task_id, attempt_id=attempt_id, worktree=wt.path,
                       backend=svc.backend, attempt_dir=run_dir / "attempts" / attempt_id,
                       timeout_seconds=task.timeout_seconds, cancel=cancel,
                       credential_refs=policy.agents[agent].credentials.env, mode=mode)

    def on_event(ev: AgentEvent) -> None:
        if emit:
            emit(json.dumps(ev.model_dump()))

    outcome = await AttemptExecutor(git, svc.redactor).run(adapter=svc.adapters[agent], task=task, ctx=ctx, wt=wt,
                                                           on_event=on_event)
    checks: list[CheckResult] = []
    validated = False
    if outcome.agent_result.success and outcome.commit_sha:
        checks, _ = DiffValidator(git, policy, svc.redactor).validate(task, base, outcome.commit_sha)
        validated = all(c.passed for c in checks)
    else:
        checks.append(CheckResult(name="agent_succeeded", passed=False,
                                  detail=f"error_code={outcome.error_code} changed={outcome.changed}"))
    return {"attempt_id": attempt_id, "exit_code": outcome.agent_result.exit_code,
            "error_code": outcome.error_code, "branch": branch, "base_commit": base,
            "commit_sha": outcome.commit_sha, "worktree": str(wt.path), "validated": validated,
            "checks": [c.model_dump() for c in checks], "logs": str(outcome.log_dir)}
