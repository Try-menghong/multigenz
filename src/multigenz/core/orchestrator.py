"""The orchestrator: owns task state, scheduling, Git integration and every success decision.

Models never decide anything here. An attempt becomes a SUCCEEDED task only after independent
validation of the resulting commit and a verified merge into the run-owned integration branch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from multigenz.config import Policy
from multigenz.contracts.agent import AgentEvent, AgentResult, CancellationToken
from multigenz.contracts.event import AGENT_OUTPUT, ATTEMPT_FINISHED, ATTEMPT_STARTED, INTEGRATION, PROVIDER, VALIDATION_RESULT
from multigenz.contracts.task import TaskSpec, TaskStatus
from multigenz.core import scheduler as sched
from multigenz.core.backoff import backoff_delay
from multigenz.core.bootstrap import Services
from multigenz.core.budget import Budget
from multigenz.core.locks import RunLock
from multigenz.core.planner import AgentPlanner
from multigenz.core.readonly import ReadOnlyRunner
from multigenz.core.recovery import Reconciler
from multigenz.core.report import write_report
from multigenz.core.runs import RunPaths, register_plan
from multigenz.core.state_machine import StateMachine
from multigenz.errors import InvalidTransition, LeaseError, PlanValidationError, WorktreeError
from multigenz.execution.git import Git
from multigenz.execution.manager import AttemptExecutor, AttemptOutcome, make_context, new_attempt_id
from multigenz.execution.subprocess import ProcessStart
from multigenz.execution.worktree import Worktree, WorktreeManager, task_branch
from multigenz.integration.merger import Merger
from multigenz.integration.pipeline import IntegrationPipeline
from multigenz.integration.reviewer import AgentReviewer
from multigenz.storage.sqlite import Store, now_iso

S = TaskStatus
PROVIDER_FAILURES = {"binary_missing", "spawn_failed", "adapter_disabled", "auth_failure", "sandbox_required"}
MAX_OUTPUT_EVENTS_PER_ATTEMPT = 200
TAIL_LINES = 40


@dataclass
class RunOutcome:
    run_id: str
    status: str  # succeeded | failed | cancelled | interrupted
    final_commit: str | None = None


class Orchestrator:
    def __init__(self, store: Store, svc: Services, run_id: str, home: Path, *, tick: float = 0.2,
                 pipeline_factory=None):
        self.store = store
        self.svc = svc
        self.policy: Policy = svc.policy
        self.run_id = run_id
        self.paths = RunPaths(home, run_id)
        self.sm = StateMachine(store)
        self.owner = f"orch-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.tick = tick
        run = store.get_run(run_id)
        assert run is not None, f"unknown run {run_id}"
        self.git = Git(Path(run["repo_path"]), run_id=run_id)
        self.wts = WorktreeManager(self.git, self.paths.worktrees)
        self.integration_wt = self.wts.open_existing("integration", run["integration_branch"], run["base_commit"])
        if self.integration_wt is None:
            raise WorktreeError("integration worktree is missing; cannot continue this run")
        self.merger = Merger(self.git, store, run_id, self.integration_wt, self.paths.scratch)
        self.budget = Budget(self.policy.orchestrator.run_budget_usd, store, run_id)
        self.readonly = ReadOnlyRunner(svc, self.git, self.paths.run_dir / "readonly", self.budget, run_id=run_id)
        make = pipeline_factory or IntegrationPipeline
        self.pipeline = make(self.git, self.policy, svc.redactor, self.merger, runner=svc.runner, paths=self.paths,
                             agent_reviewer=AgentReviewer(svc, self.readonly, self.git, svc.redactor))
        self._plan_cancel = CancellationToken()
        self.executor = AttemptExecutor(self.git, svc.redactor)
        self.tokens: dict[str, CancellationToken] = {}
        self.running: dict[asyncio.Task, str] = {}
        self.stop: str | None = None  # None | "interrupt" | "cancel"
        self.val_sem = asyncio.Semaphore(self.policy.orchestrator.max_parallel_agents)
        self._last_hb = 0.0
        svc.runner.on_process_start = self._on_proc_start

    # ------------------------------------------------------------------ control
    def request_interrupt(self) -> None:
        if self.stop is None:
            self.stop = "interrupt"
        self._plan_cancel.cancel("interrupt")
        for t in self.tokens.values():
            t.cancel("interrupt")

    def request_cancel(self) -> None:
        self.stop = "cancel"
        self._plan_cancel.cancel("cancel")
        for t in self.tokens.values():
            t.cancel("cancel")

    def _on_proc_start(self, attempt_id: str, start: ProcessStart) -> None:
        self.store.update_attempt(attempt_id, pid=start.pid, pgid=start.pgid, proc_start=start.start_ticks,
                                  sandbox_identity=start.identity, status="running")

    # ------------------------------------------------------------------ main entry
    async def execute(self) -> RunOutcome:
        lock = RunLock(self.paths.lock)
        lock.acquire()
        try:
            return await self._execute_locked()
        finally:
            lock.release()

    async def _execute_locked(self) -> RunOutcome:
        st, rid = self.store, self.run_id
        run = st.get_run(rid)
        assert run is not None
        if run["status"] in ("succeeded", "failed", "cancelled", "plan_rejected"):
            return RunOutcome(rid, run["status"], run["final_commit"])
        if run["status"] in ("created", "planning"):
            out = await self._plan_phase()
            if out is not None:
                return out
            run = st.get_run(rid)
        if run["status"] != "running":
            raise InvalidTransition(f"run is {run['status']}; cannot execute")
        kill = None
        if hasattr(self.svc.backend, "runtime"):
            kill = self._container_kill
        rec = Reconciler(st, self.git, rid, self.merger, self.executor, self.sm,
                         max_interruptions=self.policy.orchestrator.max_interruptions,
                         kill_grace=self.policy.orchestrator.kill_grace_seconds, container_kill=kill)
        await rec.run()
        if st.get_run(rid)["cancel_requested"]:
            self.stop = "cancel"
        return await self._loop()

    # ------------------------------------------------------------------ planning
    async def _plan_phase(self) -> RunOutcome | None:
        """Ask a planner agent for a plan and validate it *before anything executes* (resumable)."""
        st, rid = self.store, self.run_id
        run = st.get_run(rid)
        if run["cancel_requested"]:
            self.request_cancel()
        if run["status"] == "created":
            self.sm.run(rid, "planning", reason="awaiting planner")
        planner = AgentPlanner(self.svc, self.readonly, cancel=self._plan_cancel,
                               emit=lambda kind, payload: st.emit(rid, kind, payload=payload))
        try:
            plan = await planner.plan(run["objective"], run["base_commit"], self.git.ls_tree(run["base_commit"]))
        except PlanValidationError as exc:
            if self.stop == "interrupt":
                return RunOutcome(rid, "interrupted")
            if self.stop == "cancel":
                self.sm.run(rid, "cancelled", reason="cancelled during planning")
                write_report(st, rid, self.paths)
                return RunOutcome(rid, "cancelled")
            st.emit(rid, "plan.failed", payload={"issues": exc.issues})
            self.sm.run(rid, "plan_rejected", reason="; ".join(exc.issues)[:500])
            write_report(st, rid, self.paths)
            return RunOutcome(rid, "plan_rejected")
        if self.stop == "interrupt":
            return RunOutcome(rid, "interrupted")
        if self.stop == "cancel":
            self.sm.run(rid, "cancelled", reason="cancelled during planning")
            write_report(st, rid, self.paths)
            return RunOutcome(rid, "cancelled")
        register_plan(st, rid, plan)
        self.sm.run(rid, "running", reason=f"plan validated ({len(plan.tasks)} tasks)")
        return None

    def _container_kill(self, identity: str) -> None:
        import subprocess
        rt, _, name = identity.partition(":")
        subprocess.run([rt, "kill", name], capture_output=True, check=False, timeout=15)

    # ------------------------------------------------------------------ main loop
    async def _loop(self) -> RunOutcome:
        st, rid = self.store, self.run_id
        # tasks resumed in VALIDATING continue at validation - never at execution
        for t in st.list_tasks(rid):
            if t["status"] == S.VALIDATING.value:
                self._spawn(self._resume_validating(t["task_id"]), t["task_id"])
        while True:
            run = st.get_run(rid)
            if run["cancel_requested"] and self.stop != "cancel":
                self.request_cancel()
            now = time.time()
            if now - self._last_hb > self.policy.orchestrator.lease_seconds / 3:
                self._heartbeat(now)
            views = [sched.TaskView.from_row(r) for r in st.list_tasks(rid)]
            if self.stop is None:
                self._advance_states(views)
                views = [sched.TaskView.from_row(r) for r in st.list_tasks(rid)]
                self._dispatch(views)
            elif self.stop == "cancel":
                self._cancel_unstarted(views)
            if not self.running:
                views = [sched.TaskView.from_row(r) for r in st.list_tasks(rid)]
                if self.stop == "interrupt":
                    return RunOutcome(rid, "interrupted")
                if all(v.status.terminal for v in views):
                    break
                if self.stop is None and self._fail_stuck(views):
                    continue
            if self.running:
                done, _ = await asyncio.wait(set(self.running), timeout=self.tick,
                                             return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    self.running.pop(d, None)
                    exc = d.exception()
                    if exc is not None:
                        self._internal_error(exc)
            else:
                await asyncio.sleep(self.tick)
        return await self._finish()

    def _spawn(self, coro, task_id: str) -> None:
        self.running[asyncio.ensure_future(coro)] = task_id

    def _heartbeat(self, now: float) -> None:
        self._last_hb = now
        exp = now + self.policy.orchestrator.lease_seconds
        for a in self.store.active_attempts(self.run_id):
            if a["lease_owner"] == self.owner:
                self.store.update_attempt(a["attempt_id"], lease_expires=exp)

    # ------------------------------------------------------------------ state advancement
    def _advance_states(self, views: list[sched.TaskView]) -> None:
        for tid in sched.promotable(views):
            self.sm.task(self.run_id, tid, S.READY, reason="all prerequisites validated and integrated")
        while True:  # a blocked task can block its own dependents in the same pass
            changed = False
            for tid, cause in sched.blocked_by_failure(views):
                cur = self.store.get_task(self.run_id, tid)
                if cur["status"] in (S.PENDING.value, S.READY.value):
                    self.sm.task(self.run_id, tid, S.BLOCKED, reason=f"prerequisite {cause} did not succeed",
                                 failure_reason=f"blocked by {cause}")
                    changed = True
            if not changed:
                break
            views = [sched.TaskView.from_row(r) for r in self.store.list_tasks(self.run_id)]

    def _cancel_unstarted(self, views: list[sched.TaskView]) -> None:
        for v in views:
            if v.status in (S.PENDING, S.READY):
                self.sm.task(self.run_id, v.task_id, S.CANCELLED, reason="run cancelled")

    def _disabled_providers(self) -> set[str]:
        return {p for p, r in self.store.provider_states(self.run_id).items() if r["status"] == "disabled"}

    def _klass_of(self) -> dict[str, str]:
        return {n: a.provider_class for n, a in self.svc.adapters.items()}

    def _dispatch(self, views: list[sched.TaskView]) -> None:
        ready = [v for v in views if v.status == S.READY and v.task_id not in self.running.values()]
        active = [v for v in views if v.status in sched.ACTIVE]
        running_agents = [(v.provider, self._klass_of().get(v.provider or "", "cli"))
                          for v in views if v.status == S.RUNNING and v.provider]
        picks = sched.pick_dispatch(ready, active, policy=self.policy, probes=self.svc.probes,
                                    klass_of=self._klass_of(), disabled=self._disabled_providers(),
                                    running_agents=running_agents)
        for task_id, agent in picks:
            self._spawn(self._run_attempt(task_id, agent), task_id)

    def _fail_stuck(self, views: list[sched.TaskView]) -> bool:
        """Nothing is running: fail unroutable READY tasks, or block everything if no progress is possible.

        Returns True when it changed state (the caller re-evaluates immediately).
        """
        ready = [v for v in views if v.status == S.READY]
        dead = sched.unroutable(ready, probes=self.svc.probes, disabled=self._disabled_providers(),
                                roles=self.policy.roles)
        for tid in dead:
            self.sm.task(self.run_id, tid, S.FAILED, reason="no compatible provider available",
                         failure_reason="no_compatible_provider")
        if dead:
            return True
        if ready:
            return False  # dispatchable or waiting out a rate-limit backoff
        stuck = [v for v in views if not v.status.terminal]
        for v in stuck:
            self.sm.task(self.run_id, v.task_id, S.BLOCKED, reason="deadlock guard: no progress possible",
                         failure_reason="no progress possible")
        return bool(stuck)

    def _internal_error(self, exc: BaseException) -> None:
        self.store.emit(self.run_id, "internal_error", payload={"error": f"{type(exc).__name__}: {exc}"[:500]})

    # ------------------------------------------------------------------ one attempt
    def _lease(self, task_id: str, agent: str) -> dict:
        st, rid = self.store, self.run_id
        policy = self.policy
        with st.tx():
            t = st.get_task(rid, task_id)
            if t is None or t["status"] != S.READY.value:
                raise LeaseError(f"task {task_id} is not READY")
            spec = TaskSpec.model_validate_json(t["spec"])
            number = t["total_attempts"] + 1
            run = st.get_run(rid)
            kind = t["pending_kind"] or "code"
            if kind == "conflict":
                base = t["repair_from"]  # the validated task commit that failed to merge cleanly
            else:
                base = t["repair_from"] or (self.merger.head() if spec.dependencies else run["base_commit"])
            attempt_id = new_attempt_id()
            if kind == "conflict":  # one unique branch per attempt, so a retried/interrupted attempt never collides
                branch = task_branch(rid, task_id, attempt=number, kind="conflict")
            else:
                branch = task_branch(rid, task_id, attempt=number)
            wt_name = branch.rsplit("/", 1)[1]
            attempt_dir = self.paths.attempts / attempt_id
            now = time.time()
            try:
                st.insert_attempt(
                    attempt_id=attempt_id, run_id=rid, task_id=task_id, number=number, kind=kind, provider=agent,
                    status="leased", lease_owner=self.owner, lease_expires=now + policy.orchestrator.lease_seconds,
                    worktree=str(self.paths.worktrees / wt_name), branch=branch, base_commit=base,
                    started_at=now_iso(), log_dir=str(attempt_dir / "logs"),
                    idempotency_key=f"{rid}:{task_id}:{number}")
            except sqlite3.IntegrityError as exc:
                raise LeaseError(f"an active attempt already holds the lease for {task_id}") from exc
            # A repair chain keeps its original base so validation sees the cumulative diff; a fresh
            # start (no repair_from) re-bases on the current snapshot.
            if kind == "conflict":
                chain_base = t["conflict_target"]  # validated against the integration head it was merged with
            else:
                chain_base = t["base_commit"] if (t["repair_from"] and t["base_commit"]) else base
            self.sm.task(rid, task_id, S.RUNNING, reason=f"lease acquired by {agent}", attempt_id=attempt_id,
                         provider=agent, total_attempts=number, base_commit=chain_base, branch=branch, not_before=None)
            st.emit(rid, ATTEMPT_STARTED, task_id=task_id, attempt_id=attempt_id,
                    payload={"provider": agent, "number": number, "base_commit": base, "branch": branch})
        return {"attempt_id": attempt_id, "number": number, "base": base, "branch": branch, "wt_name": wt_name,
                "attempt_dir": attempt_dir, "spec": spec, "feedback": t["feedback"], "kind": kind,
                "target": t["conflict_target"]}

    async def _run_attempt(self, task_id: str, agent: str) -> None:
        st, rid = self.store, self.run_id
        lease = self._lease(task_id, agent)
        attempt_id, spec = lease["attempt_id"], lease["spec"]
        cancel = CancellationToken()
        self.tokens[attempt_id] = cancel
        if self.stop:
            cancel.cancel(self.stop)
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        n_out = 0
        try:
            try:
                wt = self.wts.create(lease["wt_name"], lease["branch"], lease["base"])
            except WorktreeError as exc:
                st.update_attempt(attempt_id, status="failed", error_code="worktree_error", finished_at=now_iso())
                self.sm.task(rid, task_id, S.FAILED, reason=f"worktree error: {exc}", attempt_id=attempt_id,
                             failure_reason="worktree_error")
                return
            st.update_attempt(attempt_id, gitdir=str(wt.gitdir))
            feedback = lease["feedback"]
            skip_agent = False
            if lease["kind"] == "conflict":
                clean = self.git.merge_no_commit(wt.gitdir, wt.path, lease["target"])
                unmerged = self.git.unmerged_paths(wt.gitdir, wt.path)
                if clean and not unmerged:
                    skip_agent = True  # integration moved on; the merge is now trivial
                else:
                    feedback = (f"Merge in progress: your task commit is being merged with the current integration "
                                f"branch. Conflicted files: {unmerged}. Resolve every conflict marker keeping the "
                                f"intent of BOTH sides, then stop. Do not commit.")
            timeout = min(spec.timeout_seconds, self.policy.orchestrator.task_timeout_seconds)
            ctx = make_context(run_id=rid, task_id=task_id, attempt_id=attempt_id, worktree=wt.path,
                               backend=self.svc.backend, attempt_dir=lease["attempt_dir"], timeout_seconds=timeout,
                               cancel=cancel, credential_refs=self.policy.agents[agent].credentials.env,
                               feedback=feedback, attempt_number=lease["number"],
                               mode="conflict" if lease["kind"] == "conflict" else "code")

            def on_event(ev: AgentEvent) -> None:
                nonlocal n_out
                if ev.kind in ("stdout", "stderr", "error"):
                    tail.append(ev.message)
                if ev.kind in ("stdout", "stderr") and n_out >= MAX_OUTPUT_EVENTS_PER_ATTEMPT:
                    return
                if ev.kind in ("stdout", "stderr"):
                    n_out += 1
                st.emit(rid, AGENT_OUTPUT, task_id=task_id, attempt_id=attempt_id,
                        payload={"sequence": ev.sequence, "kind": ev.kind, "message": ev.message[:2000]})

            if skip_agent:
                notes: list[str] = []
                head = self.executor.snapshot(wt, attempt_id, None, notes)
                outcome = AttemptOutcome(
                    attempt_id=attempt_id,
                    agent_result=AgentResult(task_id=task_id, attempt_id=attempt_id, exit_code=0, commit_sha=head,
                                             artifacts=[], success=True),
                    commit_sha=head if head != wt.base_commit else None, changed=head != wt.base_commit,
                    log_dir=ctx.log_dir, notes=notes)
            else:
                outcome = await self.executor.run(adapter=self.svc.adapters[agent], task=spec, ctx=ctx, wt=wt,
                                                  on_event=on_event)
            await self._after_agent(task_id, agent, lease, wt, outcome, list(tail))
        finally:
            self.tokens.pop(attempt_id, None)

    # ------------------------------------------------------------------ outcome handling
    async def _after_agent(self, task_id: str, agent: str, lease: dict, wt: Worktree, outcome: AttemptOutcome,
                           tail: list[str]) -> None:
        st, rid = self.store, self.run_id
        attempt_id, spec = lease["attempt_id"], lease["spec"]
        code = outcome.error_code
        res = outcome.agent_result
        cost = self._cost_of(agent, attempt_id)
        status = "finished" if code is None else {"timeout": "timed_out", "cancelled": "cancelled"}.get(
            code, "provider_failed" if code in PROVIDER_FAILURES else "failed")
        if code == "cancelled" and self.stop == "interrupt":
            status = "interrupted"
        st.update_attempt(attempt_id, status=status, exit_code=res.exit_code, error_code=code,
                          commit_sha=outcome.commit_sha, finished_at=now_iso(), cost_usd=cost)
        if cost is not None:
            st.update_run(rid, spent_usd=(st.get_run(rid)["spent_usd"] or 0) + cost)
        else:
            st.update_run(rid, cost_unknown=1)
        st.emit(rid, ATTEMPT_FINISHED, task_id=task_id, attempt_id=attempt_id, payload={
            "provider": agent, "status": status, "exit_code": res.exit_code, "error_code": code,
            "commit": outcome.commit_sha, "tail": tail[-10:]})
        if outcome.commit_sha:
            st.add_artifact(rid, "attempt-commit", f"{lease['branch']}@{outcome.commit_sha}", task_id=task_id,
                            attempt_id=attempt_id)
        if outcome.log_dir:
            for name in ("stdout.log", "stderr.log"):
                p = outcome.log_dir / name
                if p.exists():
                    st.add_artifact(rid, "log", str(p), task_id=task_id, attempt_id=attempt_id, size=p.stat().st_size)

        task = st.get_task(rid, task_id)
        # -- cancellation / interruption ------------------------------------------------
        if code == "cancelled":
            if self.stop == "interrupt":
                self._requeue_interrupted(task, attempt_id, outcome)
            else:
                self.sm.task(rid, task_id, S.CANCELLED, reason="cancelled; work preserved on attempt branch",
                             attempt_id=attempt_id, head_commit=outcome.commit_sha,
                             failure_reason="cancelled")
            return
        if code == "sandbox_failure":
            self.sm.task(rid, task_id, S.FAILED, reason="sandbox could not be established (fail closed)",
                         attempt_id=attempt_id, failure_reason="sandbox_failure")
            return
        # -- provider-level failures: reassign without charging the task -------------------
        if code in PROVIDER_FAILURES:
            reason = f"{agent}: {code}"
            st.set_provider_state(rid, agent, "disabled", reason)
            st.emit(rid, PROVIDER, payload={"provider": agent, "status": "disabled", "reason": reason})
            self._requeue_excluding(task, agent, attempt_id, reason)
            return
        if code == "rate_limited":
            if task["rate_limit_retries"] < self.policy.orchestrator.rate_limit_retries:
                delay = backoff_delay(task["rate_limit_retries"], base=self.policy.orchestrator.backoff_base_seconds,
                                      cap=self.policy.orchestrator.backoff_cap_seconds)
                self.sm.task(rid, task_id, S.READY, reason=f"rate limited by {agent}; backoff {delay:.2f}s",
                             attempt_id=attempt_id, rate_limit_retries=task["rate_limit_retries"] + 1,
                             not_before=time.time() + delay, extra={"backoff_seconds": round(delay, 3)})
            else:
                self._requeue_excluding(task, agent, attempt_id, f"{agent}: rate limit retries exhausted")
            return
        # -- charged failures -----------------------------------------------------------------
        if code is not None and lease["kind"] == "conflict":
            self._begin_conflict_repair(task, attempt_id, task["repair_from"],
                                        f"conflict-resolution agent failed: {code}")
            return
        if code is not None:
            fb = (f"Previous attempt failed with error_code={code}, exit_code={res.exit_code}. "
                  f"Last output lines:\n" + "\n".join(tail[-20:]))
            self._charged_failure(task, attempt_id, f"agent failed: {code}", fb, repair_from=None)
            return
        # -- agent exited 0: the claim must now be verified independently -----------------------------
        head = outcome.commit_sha or wt.base_commit
        self.sm.task(rid, task_id, S.VALIDATING, reason="agent exited 0; verifying result independently",
                     attempt_id=attempt_id, head_commit=head)
        await self._finalize(task_id, attempt_id, head)

    def _cost_of(self, agent: str, attempt_id: str) -> float | None:
        ad = self.svc.adapters[agent]
        costs = getattr(ad, "costs", None)
        if costs is not None and attempt_id in costs:
            return costs[attempt_id]
        return None

    def _requeue_interrupted(self, task: dict, attempt_id: str, outcome: AttemptOutcome) -> None:
        n = task["interruptions"] + 1
        if n > self.policy.orchestrator.max_interruptions:
            self.sm.task(self.run_id, task["task_id"], S.FAILED, reason="too many interruptions",
                         attempt_id=attempt_id, interruptions=n, failure_reason="exceeded max interruptions")
            return
        # a conflict attempt always restarts from the validated task commit it is trying to merge, never from its own snapshot
        keep = task["repair_from"] if task["pending_kind"] == "conflict" else (outcome.commit_sha or task["repair_from"])
        self.sm.task(self.run_id, task["task_id"], S.READY, reason="interrupted (resumable)", attempt_id=attempt_id,
                     interruptions=n, repair_from=keep,
                     feedback="The previous attempt was interrupted. Partial work (if any) is in your working tree.")

    def _requeue_excluding(self, task: dict, agent: str, attempt_id: str, reason: str) -> None:
        excl = sorted(set(json.loads(task["excluded_providers"])) | {agent})
        spec = TaskSpec.model_validate_json(task["spec"])
        mode = "conflict" if "pending_kind" in task.keys() and task["pending_kind"] == "conflict" else "code"
        remaining = sched.candidates(spec, mode, self.svc.probes, excluded=frozenset(excl),
                                     disabled=self._disabled_providers(), roles=self.policy.roles)
        if not remaining:
            self.sm.task(self.run_id, task["task_id"], S.FAILED, reason=f"{reason}; no compatible provider left",
                         attempt_id=attempt_id, excluded_providers=json.dumps(excl),
                         failure_reason="no_compatible_provider")
        else:
            self.sm.task(self.run_id, task["task_id"], S.READY, reason=f"{reason}; reassigning to {remaining}",
                         attempt_id=attempt_id, excluded_providers=json.dumps(excl), not_before=None)

    def _charged_failure(self, task: dict, attempt_id: str, reason: str, feedback: str,
                         repair_from: str | None) -> None:
        charged = task["charged_attempts"] + 1
        spec = TaskSpec.model_validate_json(task["spec"])
        limit = min(spec.max_attempts, self.policy.orchestrator.max_task_attempts)
        if charged >= limit:
            self.sm.task(self.run_id, task["task_id"], S.FAILED, reason=f"{reason}; attempts exhausted ({charged}/{limit})",
                         attempt_id=attempt_id, charged_attempts=charged, failure_reason=reason)
        else:
            self.sm.task(self.run_id, task["task_id"], S.READY, reason=f"{reason}; retrying ({charged}/{limit})",
                         attempt_id=attempt_id, charged_attempts=charged, feedback=feedback[:6000],
                         repair_from=repair_from, not_before=None)

    # ------------------------------------------------------------------ validation + integration
    async def _resume_validating(self, task_id: str) -> None:
        atts = [a for a in self.store.list_attempts(self.run_id, task_id) if a["status"] == "finished"]
        if not atts:
            self.sm.task(self.run_id, task_id, S.READY, reason="resume: no finished attempt to validate")
            return
        a = atts[-1]
        await self._finalize(task_id, a["attempt_id"], a["commit_sha"] or a["base_commit"])

    async def _finalize(self, task_id: str, attempt_id: str, head: str) -> None:
        st, rid = self.store, self.run_id
        task = st.get_task(rid, task_id)
        spec = TaskSpec.model_validate_json(task["spec"])
        base = task["base_commit"]
        is_conflict = (st.get_attempt(attempt_id) or {}).get("kind") == "conflict"
        if self.stop == "cancel":
            self.sm.task(rid, task_id, S.CANCELLED, reason="run cancelled before integration",
                         attempt_id=attempt_id, head_commit=head, failure_reason="cancelled")
            return
        if self.stop == "interrupt":
            self.sm.task(rid, task_id, S.READY, reason="interrupted before validation (resumable)",
                         attempt_id=attempt_id, repair_from=head if head != base and not is_conflict else task["repair_from"])
            return
        async with self.val_sem:
            if is_conflict:
                vr = await self.pipeline.validate_conflict_resolution(
                    spec, task["conflict_target"], task["repair_from"], head, attempt_id=attempt_id)
            else:
                vr = await self.pipeline.validate(spec, base, head, attempt_id=attempt_id)
        st.emit(rid, VALIDATION_RESULT, task_id=task_id, attempt_id=attempt_id, payload={
            "passed": vr.passed, "commit": head, "changed_paths": vr.changed_paths, "conflict_resolution": is_conflict,
            "checks": [c.model_dump() for c in vr.checks],
            "test_reports": [t.model_dump() for t in vr.test_reports]})
        for tr in vr.test_reports:
            for c in tr.commands:
                if c.log_path:
                    st.add_artifact(rid, "test-log", c.log_path, task_id=task_id, attempt_id=attempt_id)
        if not vr.passed:
            only_empty = {c.name for c in vr.failures} == {"non_empty_diff"} and not vr.test_reports
            if only_empty and not is_conflict:
                self._validation_failed(task_id, attempt_id, head, base, "agent produced no changes",
                                        "You produced no file changes. Every task must create or modify files within "
                                        "its allowed paths; do not stop at analysis. If nothing needs to change, the "
                                        "plan was wrong - the orchestrator will report that.")
                return
            if is_conflict:
                self._begin_conflict_repair(task, attempt_id, task["repair_from"],
                                            "conflict resolution failed validation:\n" + vr.feedback())
            else:
                self._validation_failed(task_id, attempt_id, head, base, "validation failed", vr.feedback())
            return
        ir = await self.pipeline.integrate(spec, head, attempt_id=attempt_id)
        st.emit(rid, INTEGRATION, task_id=task_id, attempt_id=attempt_id, payload={
            "status": ir.status, "commit": ir.commit, "conflicts": ir.conflicts, "detail": ir.detail[:1000],
            "test_reports": [t.model_dump() for t in ir.reports]})
        for tr in ir.reports:
            for c in tr.commands:
                if c.log_path:
                    st.add_artifact(rid, "test-log", c.log_path, task_id=task_id, attempt_id=attempt_id)
        if ir.ok:
            st.update_run(rid, integration_head=self.merger.head())
            st.add_artifact(rid, "integrated-commit", f"{self.integration_wt.branch}@{ir.commit}", task_id=task_id,
                            attempt_id=attempt_id)
            self.sm.mark_succeeded(rid, task_id, attempt_id, head_commit=head, integrated_commit=ir.commit,
                                   failure_reason=None, feedback=None, repair_from=None, pending_kind="code",
                                   conflict_target=None)
        elif ir.status == "conflict":
            if is_conflict:
                self._begin_conflict_repair(task, attempt_id, task["repair_from"],
                                            f"integration moved on and conflicts again in {ir.conflicts}")
            else:
                self._begin_conflict_repair(task, attempt_id, head, f"merge conflict in {ir.conflicts}")
        else:
            if is_conflict:
                self._begin_conflict_repair(task, attempt_id, task["repair_from"], f"integration rejected: {ir.detail}")
            else:
                self._validation_failed(task_id, attempt_id, head, base, "integration rejected",
                                        f"integration rejected: {ir.detail}")

    def _begin_conflict_repair(self, task: dict, attempt_id: str, candidate: str, why: str) -> None:
        """Bounded, isolated conflict resolution. Neither side is ever discarded or rewritten."""
        used = task["conflict_repairs"]
        if used >= self.policy.orchestrator.max_conflict_repairs or not candidate:
            self.sm.task(self.run_id, task["task_id"], S.FAILED,
                         reason=f"{why}; conflict-repair budget exhausted ({used}/{self.policy.orchestrator.max_conflict_repairs})",
                         attempt_id=attempt_id, failure_reason="unresolved merge conflict",
                         pending_kind="code", head_commit=candidate)
            return
        target = self.merger.head()
        self.sm.task(self.run_id, task["task_id"], S.READY, reason=f"{why}; scheduling isolated conflict resolution",
                     attempt_id=attempt_id, conflict_repairs=used + 1, pending_kind="conflict", conflict_target=target,
                     repair_from=candidate, feedback=why[:4000], not_before=None)

    def _validation_failed(self, task_id: str, attempt_id: str, head: str, base: str, reason: str,
                           feedback: str) -> None:
        task = self.store.get_task(self.run_id, task_id)
        keep = head if head and head != base and self.git.is_ancestor(base, head) else None
        self._charged_failure(task, attempt_id, reason, feedback, repair_from=keep)

    # ------------------------------------------------------------------ completion
    async def _finish(self) -> RunOutcome:
        st, rid = self.store, self.run_id
        tasks = st.list_tasks(rid)
        if self.stop == "cancel":
            self.sm.run(rid, "cancelled", reason="cancelled by request")
            write_report(st, rid, self.paths)
            return RunOutcome(rid, "cancelled")
        ok = all(t["status"] == S.SUCCEEDED.value for t in tasks)
        final = None
        if ok:
            head = self.merger.head()
            rep = await self.pipeline.final_check(head)
            if rep is not None:
                st.emit(rid, "integration.final_tests", payload={"passed": rep.passed, "commit": head,
                                                                  "report": rep.model_dump()})
                for c in rep.commands:
                    if c.log_path:
                        st.add_artifact(rid, "test-log", c.log_path)
                if not rep.passed:
                    ok = False
        if ok:
            final = self.merger.head()
            st.update_run(rid, final_commit=final, integration_head=final)
            self.sm.run(rid, "succeeded", reason="all tasks validated and integrated")
        else:
            bad = [f"{t['task_id']}={t['status']}" for t in tasks if t["status"] != S.SUCCEEDED.value]
            self.sm.run(rid, "failed", reason=("; ".join(bad) or "final integration tests failed")[:500])
        write_report(st, rid, self.paths)
        return RunOutcome(rid, "succeeded" if ok else "failed", final)
