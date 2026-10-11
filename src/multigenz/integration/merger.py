"""Isolated merge into the run-owned integration branch.

The merge result is computed with ``git merge-tree --write-tree`` (no worktree touched), the
resulting tree is exported and independently tested, and only then is a commit created and the
integration branch fast-forwarded. A failing or conflicting change therefore never enters the
branch, neither side of a conflict is ever modified, and nothing is reset or force-updated.

Each validated task lands as ONE commit whose tree is exactly the tree that was tested; the
intermediate attempt snapshots (possibly broken, possibly containing material a later attempt
removed) stay on their own attempt branches and never enter the integration history. Provenance is
kept in commit trailers (Multigenz-Task / -Attempt / -Candidate).
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from multigenz.errors import GitError
from multigenz.execution.git import Git
from multigenz.execution.worktree import Worktree
from multigenz.storage.sqlite import Store

TreeCheck = Callable[[Path], Awaitable[tuple[bool, str, list]]]


@dataclass
class IntegrationResult:
    status: str  # merged | already_merged | conflict | rejected
    commit: str | None = None
    conflicts: list[str] = field(default_factory=list)
    detail: str = ""
    reports: list = field(default_factory=list)
    integration_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("merged", "already_merged")


class Merger:
    def __init__(self, git: Git, store: Store, run_id: str, wt: Worktree, scratch: Path):
        self.git = git
        self.store = store
        self.run_id = run_id
        self.wt = wt
        self.scratch = scratch
        self.lock = asyncio.Lock()

    @property
    def branch(self) -> str:
        return self.wt.branch

    def head(self) -> str:
        sha = self.git.rev_parse(f"refs/heads/{self.branch}")
        if sha is None:
            raise GitError(f"integration branch {self.branch} is missing")
        return sha

    def _assert_clean(self) -> None:
        if self.git.merge_in_progress(self.wt.gitdir):
            raise GitError("integration worktree has an unfinished merge; refusing to continue")
        dirty = self.git.run("status", "--porcelain", "--untracked-files=all", git_dir=self.wt.gitdir,
                             work_tree=self.wt.path).stdout.strip()
        if dirty:
            raise GitError("integration worktree is dirty; refusing to merge into it")

    async def integrate(self, task_id: str, attempt_id: str | None, candidate: str,
                        check: TreeCheck | None = None) -> IntegrationResult:
        async with self.lock:
            self._assert_clean()
            ours = self.head()
            iid = self.store.add_integration(self.run_id, task_id, attempt_id, "merging", candidate, ours)
            res = await self._integrate_locked(task_id, attempt_id, candidate, ours, check)
            res.integration_id = iid
            self.store.update_integration(iid, status=res.status, result_commit=res.commit,
                                          detail=(res.detail or "")[:2000])
            return res

    async def _integrate_locked(self, task_id: str, attempt_id: str | None, candidate: str, ours: str,
                                check: TreeCheck | None) -> IntegrationResult:
        g = self.git
        done = g.find_commit_with_trailer(ours, "Multigenz-Candidate", candidate)
        if done or g.is_ancestor(candidate, ours):
            return IntegrationResult("already_merged", commit=ours,
                                     detail="candidate already integrated" + (f" as {done}" if done else ""))
        mt = g.merge_tree(ours, candidate)
        if not mt.clean:
            return IntegrationResult("conflict", conflicts=mt.conflicts,
                                     detail=f"merge conflict in {mt.conflicts}")
        reports: list = []
        if check is not None:
            tmp = self.scratch / f"merge-{uuid.uuid4().hex[:8]}"
            try:
                g.export_tree(mt.tree, tmp)
                ok, detail, reports = await check(tmp)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if not ok:
                return IntegrationResult("rejected", detail=detail, reports=reports)
        if mt.tree == g.tree_of(ours):
            return IntegrationResult("already_merged", commit=ours, reports=reports,
                                     detail="candidate introduces no change relative to the integration branch")
        msg = (f"multigenz: integrate task {task_id}\n\n"
               f"Multigenz-Run: {self.run_id}\nMultigenz-Task: {task_id}\n"
               f"Multigenz-Attempt: {attempt_id or '-'}\nMultigenz-Candidate: {candidate}\n")
        new = g.commit_tree(mt.tree, [ours], msg)
        g.ff_merge(self.wt.gitdir, self.wt.path, new)  # fast-forward only; fails if the branch moved
        return IntegrationResult("merged", commit=new, reports=reports)

    def reconcile_pending(self) -> list[tuple[int, str]]:
        """After a crash: settle integration rows left in 'merging'. Returns (id, new_status)."""
        out = []
        try:
            head = self.head()
        except GitError:
            return out
        for row in self.store.list_integrations(self.run_id):
            if row["status"] != "merging":
                continue
            landed = self.git.find_commit_with_trailer(head, "Multigenz-Candidate", row["candidate_commit"])
            if landed or self.git.is_ancestor(row["candidate_commit"], head):
                self.store.update_integration(row["integration_id"], status="merged", result_commit=landed or head,
                                              detail="reconciled after restart: candidate is already in the integration branch")
                out.append((row["integration_id"], "merged"))
            else:
                self.store.update_integration(row["integration_id"], status="abandoned",
                                              detail="reconciled after restart: merge never completed; will be redone")
                out.append((row["integration_id"], "abandoned"))
        return out
