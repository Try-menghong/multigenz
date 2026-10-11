import os
from pathlib import Path

import pytest

from multigenz.errors import GitError, PolicyViolation, WorktreeError
from multigenz.execution.git import Git
from multigenz.execution.worktree import WorktreeManager, integration_branch, task_branch
from tests.support.helpers import git as sgit

RID = "abc123"


def setup(repo, tmp_path):
    g = Git(repo, run_id=RID)
    return g, WorktreeManager(g, tmp_path / "wts")


def test_worktree_created_outside_repo_on_namespaced_branch(repo, tmp_path):
    g, wm = setup(repo, tmp_path)
    base = g.head()
    wt = wm.create("task-a", task_branch(RID, "a"), base)
    assert wt.path.parent == (tmp_path / "wts").resolve() and (wt.path / "README.md").exists()
    assert sgit(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/multigenz/") == "multigenz/run-abc123/task-a"
    assert g.rev_parse("HEAD", git_dir=wt.gitdir, work_tree=wt.path) == base


def test_worktree_never_overwrites_existing(repo, tmp_path):
    g, wm = setup(repo, tmp_path)
    base = g.head()
    wt = wm.create("task-a", task_branch(RID, "a"), base)
    (wt.path / "mine.txt").write_text("keep me")
    with pytest.raises(WorktreeError, match="already exists"):
        wm.create("task-a", task_branch(RID, "a", attempt=2), base)
    with pytest.raises(WorktreeError):
        wm.create("task-b", task_branch(RID, "a"), base)  # branch already exists
    assert (wt.path / "mine.txt").read_text() == "keep me"


def test_branches_outside_run_namespace_refused(repo, tmp_path):
    g, wm = setup(repo, tmp_path)
    for bad in ("main", "feature/x", "multigenz/run-other/integration", "multigenz/run-abc123/evil"):
        with pytest.raises((PolicyViolation, WorktreeError, GitError)):
            wm.create("w-" + bad.replace("/", "-"), bad, g.head())


def test_worktree_root_inside_repo_refused(repo):
    g = Git(repo, run_id=RID)
    with pytest.raises(WorktreeError):
        WorktreeManager(g, repo / "wts")


def test_original_tree_untouched_by_worktree_work(repo, tmp_path):
    (repo / "dirty.txt").write_text("uncommitted")
    (repo / "README.md").write_text("# modified locally\n")
    before_status = sgit(repo, "status", "--porcelain")
    before_head = sgit(repo, "rev-parse", "HEAD")
    before_branch = sgit(repo, "branch", "--show-current")
    g, wm = setup(repo, tmp_path)
    wt = wm.create("task-a", task_branch(RID, "a"), g.head())
    (wt.path / "src" / "app.py").write_text("VALUE = 2\n")
    g.stage_all(wt.gitdir, wt.path)
    sha = g.commit(wt.gitdir, wt.path, "x")
    assert sgit(repo, "status", "--porcelain") == before_status
    assert sgit(repo, "rev-parse", "HEAD") == before_head and sgit(repo, "branch", "--show-current") == before_branch
    assert (repo / "README.md").read_text() == "# modified locally\n" and (repo / "dirty.txt").read_text() == "uncommitted"
    assert (repo / "src" / "app.py").read_text() == "VALUE = 1\n" and sha != before_head


def test_tampered_gitfile_cannot_redirect_commit(repo, tmp_path):
    """An agent can rewrite the worktree's .git pointer; the orchestrator's explicit --git-dir ignores it."""
    g, wm = setup(repo, tmp_path)
    wt = wm.create("task-a", task_branch(RID, "a"), g.head())
    (wt.path / ".git").write_text(f"gitdir: {repo}/.git\n")
    (wt.path / "new.txt").write_text("x")
    g.stage_all(wt.gitdir, wt.path)
    g.commit(wt.gitdir, wt.path, "snap")
    assert sgit(repo, "log", "-1", "--format=%s", "main") == "initial"
    assert g.rev_parse("multigenz/run-abc123/task-a") != g.head()


def test_hooks_are_not_executed(repo, tmp_path):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch /tmp/hook-ran-marker\nexit 1\n")
    hook.chmod(0o755)
    g, wm = setup(repo, tmp_path)
    wt = wm.create("task-a", task_branch(RID, "a"), g.head())
    (wt.path / "f").write_text("1")
    g.stage_all(wt.gitdir, wt.path)
    g.commit(wt.gitdir, wt.path, "m")
    assert not Path("/tmp/hook-ran-marker").exists()


def test_merge_tree_clean_and_conflict(repo, tmp_path):
    g, wm = setup(repo, tmp_path)
    base = g.head()
    a = wm.create("task-a", task_branch(RID, "a"), base)
    b = wm.create("task-b", task_branch(RID, "b"), base)
    (a.path / "src/app.py").write_text("VALUE = 'A'\n")
    (b.path / "src/app.py").write_text("VALUE = 'B'\n")
    (b.path / "other.txt").write_text("o")
    for w in (a, b):
        g.stage_all(w.gitdir, w.path)
        g.commit(w.gitdir, w.path, "c")
    r = g.merge_tree(task_branch(RID, "a"), task_branch(RID, "b"))
    assert not r.clean and r.conflicts == ["src/app.py"]
    assert g.rev_parse(task_branch(RID, "a")) and g.rev_parse(task_branch(RID, "b"))  # sides untouched
    c = wm.create("task-c", task_branch(RID, "c"), base)
    (c.path / "z.txt").write_text("z")
    g.stage_all(c.gitdir, c.path); g.commit(c.gitdir, c.path, "c")
    assert g.merge_tree(task_branch(RID, "a"), task_branch(RID, "c")).clean


def test_diff_raw_parses_adds_deletes_modes(repo, tmp_path):
    g, wm = setup(repo, tmp_path)
    base = g.head()
    w = wm.create("task-a", task_branch(RID, "a"), base)
    (w.path / "new dir").mkdir()
    (w.path / "new dir" / "sp ace.txt").write_text("x")
    (w.path / "README.md").unlink()
    os.symlink("../../etc/passwd", w.path / "link")
    g.stage_all(w.gitdir, w.path)
    head = g.commit(w.gitdir, w.path, "c")
    ch = {c.path: c for c in g.diff_raw(base, head)}
    assert ch["new dir/sp ace.txt"].status == "A" and ch["README.md"].status == "D" and ch["link"].new_mode == "120000"
