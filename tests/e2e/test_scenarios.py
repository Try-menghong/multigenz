"""End-to-end scenarios A and B (docs/IMPLEMENTATION_PLAN.md). Scenario C: test_scenario_c.py.

The *agents* are scripted stand-ins (no LLM): they exercise the real planner path, adapters, sandbox,
process management, validation, merging and reporting. They write real FastAPI code which is tested
by real pytest runs inside the sandbox and again independently on the host.
"""

import json

import pytest

from multigenz.core.runs import open_store
from tests.e2e.harness import (GOAL, build_scenario, cli, demo_repo, e2e_policy, files_for, pytest_on_commit,
                               write_policy)
from tests.support.helpers import git

pytestmark = [pytest.mark.e2e, pytest.mark.needs_userns]


def run_goal(tmp_path, repo, pol, *extra):
    pf = write_policy(tmp_path / "policy.yaml", pol)
    return cli("run", GOAL, "--repo", str(repo), "--mode", "autonomous", "--policy", str(pf), "--json", *extra)


def report_of(proc) -> dict:
    return json.loads(proc.stdout)


def integration_history(repo, branch):
    return git(repo, "rev-list", "--reverse", f"main..{branch}").split()


def test_scenario_a_successful_feature(tmp_path, scenario):
    repo = demo_repo(tmp_path / "demo-api")
    build_scenario(scenario)
    base = git(repo, "rev-parse", "HEAD")
    p = run_goal(tmp_path, repo, e2e_policy(scenario))
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
    rep = report_of(p)

    # verified task states, test report, artifact list, final SHA, explicit unknown costs, no push
    assert rep["status"] == "succeeded"
    assert {t["task_id"]: t["status"] for t in rep["tasks"]} == {"security": "succeeded", "api": "succeeded", "docs": "succeeded"}
    assert all(t["last_validation"]["passed"] for t in rep["tasks"])
    assert all(any(c["name"].startswith("tests[") and c["passed"] for c in t["last_validation"]["checks"]) for t in rep["tasks"])
    assert rep["final_integration_commit"] and rep["final_tests"][0]["passed"]
    assert rep["pushed"] is False and rep["cost"]["unknown_for_attempts"] >= 3 and "unknown" in rep["cost"]["note"]
    assert {a["kind"] for a in rep["artifacts"]} >= {"attempt-commit", "integrated-commit", "log", "test-log"}

    # the integrated result is a working FastAPI service - verified independently of MultiGenz
    final = rep["final_integration_commit"]
    files = set(git(repo, "ls-tree", "-r", "--name-only", final).split())
    assert {"app/main.py", "app/security.py", "docs/AUTH.md", "tests/test_api.py", "tests/test_security.py"} <= files
    ok, tail = pytest_on_commit(repo, final)
    assert ok and "10 passed" in tail, tail

    # every commit that ever entered the integration branch passes the tests
    for c in integration_history(repo, rep["integration_branch"]):
        assert pytest_on_commit(repo, c)[0], c

    # dependency order and snapshot: api was built on a snapshot containing the validated security commit
    st = open_store(__import__("pathlib").Path(__import__("os").environ["MULTIGENZ_HOME"]))
    api, sec = st.list_attempts(rep["run_id"], "api")[0], st.list_attempts(rep["run_id"], "security")[0]
    assert api["started_at"] >= sec["finished_at"]
    assert git(repo, "cat-file", "-e", f"{api['base_commit']}:app/security.py") == ""

    # the user's repository was never touched and nothing was pushed
    assert git(repo, "rev-parse", "HEAD") == base and git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--show-current") == "main" and git(repo, "remote") == ""
    new_refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/").split()
    assert all(r == "refs/heads/main" or r.startswith(f"refs/heads/multigenz/run-{rep['run_id']}/") for r in new_refs)

    # CLI views over the same run
    show = cli("runs", "show", rep["run_id"])
    assert "SUCCEEDED" in show.stdout and "cost:" in show.stdout and "nothing was pushed" in show.stdout
    audit = cli("runs", "audit", rep["run_id"])
    assert audit.returncode == 0 and "audit OK" in audit.stdout


def test_scenario_b_injected_failure_is_repaired_within_bounds(tmp_path, scenario):
    repo = demo_repo(tmp_path / "demo-api")
    build_scenario(scenario, api_attempts={
        "1": files_for("api", variant="buggy"),  # login accepts any password -> injected test failure
        "2": [{"echo_prompt_contains": ["test_login_wrong_password", "Validation feedback"]}, *files_for("api")]})
    p = run_goal(tmp_path, repo, e2e_policy(scenario))
    assert p.returncode == 0, p.stdout[-3000:]
    rep = report_of(p)
    assert rep["status"] == "succeeded"
    api = next(t for t in rep["tasks"] if t["task_id"] == "api")
    assert api["status"] == "succeeded" and api["attempts"] == 2 and api["charged_attempts"] == 1

    st = open_store(__import__("pathlib").Path(__import__("os").environ["MULTIGENZ_HOME"]))
    vals = [e for e in st.events(rep["run_id"], kinds=("validation.result",)) if e["task_id"] == "api"]
    assert [v["payload"]["passed"] for v in vals] == [False, True]
    failing = [c for c in vals[0]["payload"]["checks"] if c["name"].startswith("tests[")][0]
    assert not failing["passed"]
    tail = vals[0]["payload"]["test_reports"][0]["commands"][0]["output_tail"]
    assert "test_login_wrong_password_is_unauthorized" in tail
    # the failing test output really reached the repair attempt (untrusted feedback channel)
    a1, a2 = st.list_attempts(rep["run_id"], "api")
    log = open(f"{a2['log_dir']}/stdout.log").read()
    assert "PROMPT-HAS[test_login_wrong_password]=True" in log and "PROMPT-HAS[Validation feedback]=True" in log
    # the broken implementation never reached the integration branch; the failed attempt is preserved
    for c in integration_history(repo, rep["integration_branch"]):
        blob = git(repo, "show", f"{c}:app/main.py", ) if git(repo, "ls-tree", c, "app/main.py") else ""
        assert "if stored is None:\n" not in blob + "\n"
    assert "if stored is None:" in git(repo, "show", f"{a1['branch']}:app/main.py")
    ok, tail = pytest_on_commit(repo, rep["final_integration_commit"])
    assert ok, tail


def test_scenario_b_unfixable_failure_is_reported_accurately_as_failure(tmp_path, scenario):
    repo = demo_repo(tmp_path / "demo-api")
    build_scenario(scenario, api_attempts={"default": files_for("api", variant="buggy")})
    p = run_goal(tmp_path, repo, e2e_policy(scenario))
    assert p.returncode == 1, p.stdout[-2000:]
    rep = report_of(p)
    assert rep["status"] == "failed"
    states = {t["task_id"]: t["status"] for t in rep["tasks"]}
    assert states == {"security": "succeeded", "api": "failed", "docs": "succeeded"}
    api = next(t for t in rep["tasks"] if t["task_id"] == "api")
    assert api["attempts"] == 2 and "validation failed" in api["failure_reason"]
    assert api["last_validation"]["passed"] is False
    assert rep["final_integration_commit"] is None  # no "final" SHA is claimed for a failed run
    # integration contains the verified parts only
    files = set(git(repo, "ls-tree", "-r", "--name-only", rep["integration_head"]).split())
    assert "app/security.py" in files and "docs/AUTH.md" in files and "app/main.py" not in files
    show = cli("runs", "show", rep["run_id"])
    assert "FAILED" in show.stdout and "api" in show.stdout
    assert cli("runs", "audit", rep["run_id"]).returncode == 0
