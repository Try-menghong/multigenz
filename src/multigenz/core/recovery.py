"""Restart reconciliation (AT-07): leases, OS processes, worktrees and Git commits.

Runs before anything is (re-)executed. Principles:
* a leftover agent process is verified to be ours (pid + kernel start time) and terminated;
* work found in an interrupted attempt's worktree is snapshotted in Git, never discarded;
* completed or integrated work is never re-executed (VALIDATING tasks resume at validation,
  'merging' rows are settled by asking Git whether the commit is already in the branch);
* an interrupted attempt is not charged against the task's retry budget (bounded separately).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from multigenz.contracts.task import TaskSpec, TaskStatus
from multigenz.core.state_machine import StateMachine
from multigenz.execution.git import Git
from multigenz.execution.manager import AttemptExecutor
from multigenz.execution.subprocess import pgid_alive, proc_start_ticks, terminate_group
from multigenz.execution.worktree import Worktree
from multigenz.integration.merger import Merger
from multigenz.storage.sqlite import Store, now_iso

S = TaskStatus


class Reconciler:
    def __init__(self, store: Store, git: Git, run_id: str, merger: Merger, executor: AttemptExecutor,
                 sm: StateMachine, *, max_interruptions: int, kill_grace: float = 2.0, container_kill=None):
        self.store, self.git, self.run_id = store, git, run_id
        self.merger, self.executor, self.sm = merger, executor, sm
        self.max_interruptions = max_interruptions
        self.kill_grace = kill_grace
        self.container_kill = container_kill  # callable(sandbox_identity) for container backends

    async def run(self) -> dict:
        st = self.store
        summary = {"attempts_interrupted": 0, "processes_killed": 0, "integrations": [], "resumed_validating": []}
        # 1. integrations left half-done
        for iid, status in self.merger.reconcile_pending():
            summary["integrations"].append({"id": iid, "status": status})
        # 2. attempts that were leased/running when the previous process died
        for att in st.active_attempts(self.run_id):
            killed = await self._kill_leftover(att)
            summary["processes_killed"] += int(killed)
            wt_commit, has_work = self._snapshot_worktree(att)
            st.update_attempt(att["attempt_id"], status="interrupted", error_code="interrupted",
                              finished_at=now_iso(), commit_sha=wt_commit if has_work else None)
            summary["attempts_interrupted"] += 1
            task = st.get_task(self.run_id, att["task_id"])
            interruptions = task["interruptions"] + 1
            st.emit(self.run_id, "reconcile", task_id=att["task_id"], attempt_id=att["attempt_id"], payload={
                "action": "attempt_interrupted", "process_killed": killed, "work_preserved": has_work,
                "commit": wt_commit if has_work else None, "interruptions": interruptions})
            if interruptions > self.max_interruptions:
                self.sm.task(self.run_id, att["task_id"], S.FAILED, reason="too many interruptions",
                             attempt_id=att["attempt_id"], interruptions=interruptions,
                             failure_reason="exceeded max interruptions")
                continue
            fb = ("The previous attempt was interrupted by an orchestrator restart. "
                  + ("Its partial work is already present in your working tree; review and complete it."
                     if has_work else "No partial work was found; start from scratch."))
            self.sm.task(self.run_id, att["task_id"], S.READY, reason="interrupted; lease released",
                         attempt_id=att["attempt_id"], interruptions=interruptions,
                         repair_from=(task["repair_from"] if att["kind"] == "conflict"
                                      else (wt_commit if has_work else task["repair_from"])),
                         feedback=fb, not_before=None)
        # 3. tasks whose agent finished and were being validated/integrated: resume at validation
        for t in st.list_tasks(self.run_id):
            if t["status"] == S.VALIDATING.value:
                summary["resumed_validating"].append(t["task_id"])
        st.emit(self.run_id, "reconcile", payload={"action": "summary", **summary})
        return summary

    async def _kill_leftover(self, att: dict) -> bool:
        pid, pgid, ticks = att["pid"], att["pgid"], att["proc_start"]
        killed = False
        if pid and pgid and ticks and proc_start_ticks(pid) == ticks and pgid_alive(pgid):
            await terminate_group(pgid, self.kill_grace)  # verified to be our own process group
            killed = True
        ident = att.get("sandbox_identity") or ""
        if self.container_kill and ident and not ident.startswith(("userns:", "none:")):
            try:
                self.container_kill(ident)
                killed = True
            except Exception:  # noqa: BLE001
                pass
        return killed

    def _snapshot_worktree(self, att: dict) -> tuple[str | None, bool]:
        path = att.get("worktree")
        gitdir = att.get("gitdir")
        base = att.get("base_commit")
        if not path or not gitdir or not Path(path).is_dir() or not Path(gitdir).is_dir():
            return None, False
        wt = Worktree(name=Path(path).name, path=Path(path), branch=att["branch"], gitdir=Path(gitdir),
                      base_commit=base)
        notes: list[str] = []
        head = self.executor.snapshot(wt, att["attempt_id"], "interrupted", notes)
        return head, bool(head and head != base)
