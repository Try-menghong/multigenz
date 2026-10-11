"""Worktree lifecycle: unique, namespaced, never overwritten. Worktrees are never removed by MultiGenz
(failed attempts and diagnostics are preserved; garbage collection would need an explicit retention policy)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from multigenz.errors import GitError, WorktreeError
from multigenz.execution.git import Git

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,80}$")


def task_branch(run_id: str, task_id: str, *, attempt: int = 1, kind: str = "code") -> str:
    """multigenz/run-<id>/task-<task>[.aN | .cN]  (first code attempt has no suffix)."""
    base = f"multigenz/run-{run_id}/task-{task_id}"
    if kind == "conflict":
        return f"{base}.c{attempt}"
    return base if attempt == 1 else f"{base}.a{attempt}"


def integration_branch(run_id: str) -> str:
    return f"multigenz/run-{run_id}/integration"


@dataclass(frozen=True)
class Worktree:
    name: str
    path: Path
    branch: str
    gitdir: Path
    base_commit: str


class WorktreeManager:
    def __init__(self, git: Git, root: Path):
        self.git = git
        self.root = root.resolve()
        top = git.toplevel()
        if self.root == top or top in self.root.parents:
            raise WorktreeError("worktree root must be outside the user's repository working tree")

    def path_for(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise WorktreeError(f"invalid worktree name {name!r}")
        return self.root / name

    def create(self, name: str, branch: str, start: str) -> Worktree:
        path = self.path_for(name)
        if path.exists():
            raise WorktreeError(f"worktree path already exists, refusing to overwrite: {path}")
        if path.resolve() in self.git.worktree_paths():
            raise WorktreeError(f"path already registered as a worktree: {path}")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            gitdir = self.git.worktree_add(path, branch, start)
        except GitError as exc:
            raise WorktreeError(str(exc)) from exc
        head = self.git.rev_parse("HEAD", git_dir=gitdir, work_tree=path)
        if head != self.git.rev_parse(start):
            raise WorktreeError(f"worktree {name} did not start at {start}")
        return Worktree(name=name, path=path, branch=branch, gitdir=gitdir, base_commit=head)

    def open_existing(self, name: str, branch: str, base_commit: str) -> Worktree | None:
        path = self.path_for(name)
        if not path.is_dir():
            return None
        try:
            gitdir = Path(self.git.out("rev-parse", "--path-format=absolute", "--git-dir", cwd=path)).resolve()
        except GitError:
            return None
        return Worktree(name=name, path=path, branch=branch, gitdir=gitdir, base_commit=base_commit)
