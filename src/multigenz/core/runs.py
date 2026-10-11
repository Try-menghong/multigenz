"""Run creation and filesystem layout."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from multigenz.config import Policy
from multigenz.contracts.task import PlanDocument, TaskStatus
from multigenz.core.bootstrap import Services
from multigenz.core.dag import assert_valid_plan
from multigenz.core.redaction import Redactor
from multigenz.core.state_machine import StateMachine
from multigenz.errors import GitError, PolicyViolation
from multigenz.execution.git import Git
from multigenz.execution.worktree import WorktreeManager, integration_branch
from multigenz.storage.sqlite import Store


@dataclass(frozen=True)
class RunPaths:
    home: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return self.home / "runs" / self.run_id

    @property
    def worktrees(self) -> Path:
        return self.run_dir / "worktrees"

    @property
    def attempts(self) -> Path:
        return self.run_dir / "attempts"

    @property
    def scratch(self) -> Path:
        return self.run_dir / "scratch"

    @property
    def lock(self) -> Path:
        return self.run_dir / "run.lock"

    @property
    def report_json(self) -> Path:
        return self.run_dir / "report.json"

    @property
    def report_md(self) -> Path:
        return self.run_dir / "report.md"


def db_path(home: Path) -> Path:
    return home / "multigenz.db"


def open_store(home: Path, redactor: Redactor | None = None, *, readonly: bool = False) -> Store:
    return Store(db_path(home), redactor, readonly=readonly)


def usable_code_capabilities(svc: Services) -> list[set[str]]:
    return [set(p.capabilities) for p in svc.probes.values() if p.enabled and "code" in p.modes]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


async def create_run(store: Store, svc: Services, *, objective: str, repo: Path, plan: PlanDocument | None,
                     home: Path, mode: str = "autonomous", run_id: str | None = None) -> str:
    """Validate everything up-front, then persist the run, its integration worktree and its tasks."""
    policy: Policy = svc.policy
    svc.engine.check_text(objective, what="goal")
    if mode != "autonomous":
        raise PolicyViolation(f"unsupported mode {mode!r}; only 'autonomous' runs are implemented")
    if policy.orchestrator.mode != "autonomous":
        raise PolicyViolation(f"policy orchestrator.mode is {policy.orchestrator.mode!r}; execution is disabled")
    if plan is not None:
        assert_valid_plan(plan, svc.engine, available_capabilities=usable_code_capabilities(svc))
    run_id = run_id or new_run_id()
    git = Git(repo, run_id=run_id)
    try:
        base = git.head()
        top = git.toplevel()
    except GitError as exc:
        raise PolicyViolation(f"{repo} is not a usable git repository with commits: {exc}") from exc
    dirty = bool(git.status_porcelain().strip("\0"))
    snap, sha = policy.snapshot()
    paths = RunPaths(home, run_id)
    sm = StateMachine(store)
    store.create_run(
        run_id=run_id, objective=objective, repo_path=str(top), repo_identity=git.repo_identity(),
        base_commit=base, base_branch=git.current_branch(), mode=mode, policy_snapshot=snap, policy_sha256=sha,
        budget_usd=policy.orchestrator.run_budget_usd, status="created",
        integration_branch=integration_branch(run_id), integration_head=base)
    store.emit(run_id, "run.created", payload={
        "status": "created", "objective": objective, "repo": str(top), "base_commit": base,
        "base_branch": git.current_branch(), "policy_sha256": sha, "mode": mode,
        "original_tree_dirty": dirty,
        "note": ("uncommitted changes in the original working tree are NOT part of the run base"
                 if dirty else "")})
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    wts = WorktreeManager(git, paths.worktrees)
    wts.create("integration", integration_branch(run_id), base)
    if plan is not None:
        register_plan(store, run_id, plan)
        sm.run(run_id, "running", reason="plan validated")
    else:
        sm.run(run_id, "planning", reason="awaiting planner")
    return run_id


def register_plan(store: Store, run_id: str, plan: PlanDocument) -> None:
    with store.tx():
        for t in plan.tasks:
            store.insert_task(run_id, t.task_id, t.model_dump_json(), TaskStatus.PENDING.value)
            store.emit(run_id, "task.registered", task_id=t.task_id, payload={"spec": t.model_dump(mode="json")})
