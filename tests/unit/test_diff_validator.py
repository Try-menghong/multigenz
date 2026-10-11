import os

import pytest

from multigenz.config import parse_policy
from multigenz.contracts.task import TaskSpec
from multigenz.execution.git import Git
from multigenz.integration.validator import DiffValidator
from tests.support.helpers import git as sgit

RID = "v1"


@pytest.fixture
def env(repo):
    g = Git(repo, run_id=RID)
    base = g.head()
    return g, base, repo


def commit_changes(g, repo, base, mutate, name="x"):
    """Build a commit on a scratch branch inside the real repo using a throwaway worktree."""
    from multigenz.execution.worktree import WorktreeManager
    wm = WorktreeManager(g, repo.parent / f"wts-{name}")
    wt = wm.create("w", f"multigenz/run-{RID}/task-{name}", base)
    mutate(wt.path)
    g.stage_all(wt.gitdir, wt.path)
    return g.commit(wt.gitdir, wt.path, "c", allow_empty=True)


def validate(g, base, head, paths=("src/",), policy=None, **task_kw):
    pol = policy or parse_policy({})
    t = TaskSpec(task_id="t", title="t", objective="o", allowed_paths=list(paths), **task_kw)
    checks, changed = DiffValidator(g, pol).validate(t, base, head)
    return {c.name: c for c in checks}, changed


def test_clean_change_passes(env):
    g, base, repo = env
    head = commit_changes(g, repo, base, lambda p: (p / "src" / "new.py").write_text("x = 1\n"))
    checks, changed = validate(g, base, head)
    assert all(c.passed for c in checks.values()) and changed == ["src/new.py"]


def test_changes_outside_allowed_paths_including_deletes(env):
    g, base, repo = env
    head = commit_changes(g, repo, base, lambda p: ((p / "README.md").unlink(), (p / "src" / "n.py").write_text("1\n")))
    checks, _ = validate(g, base, head)
    assert not checks["allowed_paths"].passed and "README.md" in checks["allowed_paths"].detail


def test_empty_diff_is_not_success(env):
    g, base, repo = env
    head = commit_changes(g, repo, base, lambda p: None)
    assert not validate(g, base, head)[0]["non_empty_diff"].passed


def test_symlink_escape_and_gitlink_rejected(env):
    g, base, repo = env

    def mutate(p):
        os.symlink("/etc/passwd", p / "src" / "abs")
        os.symlink("../../../outside", p / "src" / "rel")
        os.symlink("app.py", p / "src" / "ok")  # in-repo links are fine
    head = commit_changes(g, repo, base, mutate)
    c = validate(g, base, head)[0]["file_modes"]
    assert not c.passed and "src/abs" in c.detail and "src/rel" in c.detail and "src/ok" not in c.detail


def test_embedded_repository_rejected(env):
    g, base, repo = env

    def mutate(p):
        sub = p / "src" / "vendored"
        sub.mkdir()
        sgit(sub, "init", "-q")
        sgit(sub, "config", "user.email", "a@b")
        sgit(sub, "config", "user.name", "n")
        (sub / "f").write_text("1")
        sgit(sub, "add", "f")
        sgit(sub, "commit", "-qm", "x")
    head = commit_changes(g, repo, base, mutate)
    c = validate(g, base, head)[0]["file_modes"]
    assert not c.passed and "gitlink" in c.detail


def test_forbidden_paths_even_if_allowed_by_task(env):
    g, base, repo = env
    head = commit_changes(g, repo, base, lambda p: ((p / ".github" / "workflows").mkdir(parents=True),
                                                    (p / ".github" / "workflows" / "ci.yml").write_text("on: push\n")))
    checks, _ = validate(g, base, head, paths=(".github/",))
    assert not checks["forbidden_paths"].passed


def test_size_limit_and_required_artifacts_and_secrets(env):
    g, base, repo = env
    pol = parse_policy({"validation": {"max_file_bytes": 100}})
    head = commit_changes(g, repo, base, lambda p: (p / "src" / "big.py").write_text("x" * 500))
    assert not validate(g, base, head, policy=pol)[0]["max_file_bytes"].passed
    head = commit_changes(g, repo, base, lambda p: (p / "src" / "a.py").write_text("1\n"), name="y")
    assert not validate(g, base, head, required_artifacts=["src/report.md"])[0]["required_artifacts"].passed
    head = commit_changes(g, repo, base, lambda p: (p / "src" / "k.py").write_text("T = 'sk-" + "a" * 30 + "'\n"), name="z")
    assert not validate(g, base, head)[0]["no_credentials_added"].passed


def test_result_must_descend_from_base(env):
    g, base, repo = env
    head = commit_changes(g, repo, base, lambda p: (p / "src" / "a.py").write_text("1\n"))
    other = sgit(repo, "commit-tree", "-m", "orphan", f"{head}^{{tree}}")
    checks, _ = validate(g, base, other)
    assert not checks["descends_from_base"].passed
    assert not validate(g, base, "0" * 40)[0]["commit_exists"].passed
