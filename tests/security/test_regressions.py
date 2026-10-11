"""Security regression suite: constraints from AGENTS.md that must never silently regress."""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from multigenz.cli import app
from multigenz.core.driver import load_run_policy, start_run
from multigenz.core.runs import open_store
from multigenz.errors import PolicyViolation
from multigenz.execution.git import Git, guard
from tests.support.helpers import T, git, plan_of, scripted_policy, write_scenario, writes

SRC = Path(__file__).resolve().parents[2] / "src" / "multigenz"


def sources():
    return {p: p.read_text() for p in SRC.rglob("*.py")}


def test_no_shell_execution_anywhere_in_the_orchestrator():
    for path, text in sources().items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert not (isinstance(node.value, ast.Constant) and node.value.value is True), f"shell=True in {path}"
            if isinstance(node, ast.Attribute) and node.attr in ("create_subprocess_shell", "system", "popen") \
                    and isinstance(node.value, ast.Name) and node.value.id in ("asyncio", "os"):
                pytest.fail(f"{node.value.id}.{node.attr} used in {path}")
    assert "|| true" not in "".join(sources().values())


def test_dangerous_git_operations_are_not_even_spelled_in_the_source():
    blob = "\n".join(t for p, t in sources().items() if p.name != "git.py")
    for needle in ("reset --hard", '"push"', "--force", "force-with-lease", "branch -D", "clean -f", "remove --force"):
        assert needle not in blob, needle


@pytest.mark.parametrize("args", [("push", "origin", "main"), ("reset", "--hard", "HEAD~1"), ("clean", "-fdx"),
                                  ("branch", "-D", "x"), ("worktree", "remove", "--force", "p"), ("rebase", "main"),
                                  ("checkout", "main"), ("update-ref", "--delete", "refs/heads/x"), ("fetch",),
                                  ("worktree", "prune"), ("gc", "--prune=now"), ("remote", "add", "o", "u")])
def test_git_wrapper_refuses_destructive_commands(repo, args):
    with pytest.raises(PolicyViolation):
        Git(repo, run_id="x").run(*args)
    guard(("rev-parse", "HEAD"))
    guard(("worktree", "add", "-b", "multigenz/run-x/task-a", "/p", "abc"))  # allowed commands pass the guard


def test_autonomous_actions_cannot_include_push_or_deploy(repo):
    from multigenz.config import parse_policy
    from multigenz.core.policy import PolicyEngine
    e = PolicyEngine(parse_policy({}))
    for a in ("git.push", "deploy", "infra.modify", "branch.write_protected", "branch.delete", "git.force", "shell.exec"):
        with pytest.raises(PolicyViolation):
            e.authorize(a)


def test_only_run_owned_refs_can_be_created(repo, tmp_path):
    g = Git(repo, run_id="abc123")
    from multigenz.execution.worktree import WorktreeManager
    wm = WorktreeManager(g, tmp_path / "w")
    for bad in ("main", "master", "release/1", "multigenz/run-abc123/../../main", "multigenz/run-zzz/integration"):
        with pytest.raises(Exception):
            wm.create("x" + str(abs(hash(bad)) % 999), bad, g.head())
    assert git(repo, "for-each-ref", "refs/heads/").split() == [git(repo, "rev-parse", "HEAD"), "commit", "refs/heads/main"]


@pytest.mark.needs_userns
async def test_run_uses_policy_snapshot_not_the_mutable_policy_file(tmp_path, repo, scenario):
    write_scenario(scenario, "a", writes("a/f.py", "1\n"))
    pol = scripted_policy(scenario)
    rid, out = await start_run(pol, objective="o", repo=repo, plan=plan_of(T("a", "a/")),
                               home=tmp_path / "h", execute=False)
    st = open_store(tmp_path / "h")
    snap = load_run_policy(st, rid)
    assert snap.git.auto_push is False and snap.snapshot() == pol.snapshot()
    assert st.get_run(rid)["policy_sha256"] == pol.snapshot()[1]


def test_unavailable_isolation_refuses_to_start_and_creates_nothing(tmp_path, repo, scenario):
    pol = scripted_policy(scenario, isolation="rootless-container", execution={"container_image": "x", "container_runtime": "podman"})
    pf = tmp_path / "p.yaml"
    pf.write_text(yaml.safe_dump(json.loads(pol.model_dump_json())))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("a", "a/")).model_dump_json())
    r = subprocess.run([sys.executable, "-m", "multigenz", "run", "g", "--repo", str(repo), "--policy", str(pf),
                        "--plan-file", str(plan_file)], capture_output=True, text=True,
                       env={"PATH": "/nonexistent-so-no-container-runtime", "HOME": str(tmp_path), "MULTIGENZ_HOME": str(tmp_path / "h"),
                            "PYTHONPATH": ""} | {})
    # (PATH hides podman/docker; the interpreter is invoked by absolute path)
    assert r.returncode != 0 and "never falls back" in r.stderr + r.stdout
    assert git(repo, "for-each-ref", "refs/heads/multigenz/") == ""
    assert not (tmp_path / "h" / "runs").exists()


def test_credentials_never_appear_in_container_argv(tmp_path):
    from multigenz.execution.resources import ResourceLimits
    from multigenz.execution.sandbox import ContainerBackend, SandboxRequest
    b = ContainerBackend("podman", "img", None)
    req = SandboxRequest(argv=["a"], env={"API_KEY": "sk-secret-value-123456"}, workspace=tmp_path, state_dir=tmp_path / "s",
                         sandbox_id="x", network="none", limits=ResourceLimits())
    argv = b.build_argv(req, runtime="podman", env_file=tmp_path / "env")
    assert "sk-secret-value-123456" not in " ".join(argv)


def test_sensitive_state_is_private(tmp_path, repo, scenario):
    from multigenz.core.runs import private_dir
    d = private_dir(tmp_path / "a" / "b" / "c")
    for p in (d, d.parent, d.parent.parent):
        assert (p.stat().st_mode & 0o077) == 0
