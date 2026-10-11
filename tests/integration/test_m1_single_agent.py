"""M1 exit criterion: one validated agent commit, via the real CLI, sandbox and adapter."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from multigenz.cli import app
from tests.support.helpers import git, scripted_policy, write_scenario

pytestmark = pytest.mark.needs_userns
runner = CliRunner()


def policy_file(tmp_path, scenario, **kw):
    pol = scripted_policy(scenario, **kw)
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump(json.loads(pol.model_dump_json())))
    return f


def run_cli(tmp_path, scenario, repo, allow=("src/",), **kw):
    pf = policy_file(tmp_path, scenario, **kw)
    args = ["agents", "run", "alpha", "Implement the feature", "--repo", str(repo), "--policy", str(pf), "--json"]
    for a in allow:
        args += ["--allow", a]
    r = runner.invoke(app, args)
    lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    return r, lines[-1]


def test_one_validated_agent_commit(tmp_path, repo, scenario):
    write_scenario(scenario, "adhoc", {"attempts": {"default": [
        {"write": {"src/feature.py": "def feature():\n    return 42\n"}, "print": ["working..."]}]}})
    before = git(repo, "rev-parse", "HEAD")
    r, out = run_cli(tmp_path, scenario, repo)
    assert r.exit_code == 0, r.stdout
    assert out["validated"] and out["exit_code"] == 0 and out["commit_sha"]
    assert all(c["passed"] for c in out["checks"])
    assert git(repo, "show", f"{out['commit_sha']}:src/feature.py") == "def feature():\n    return 42"
    assert git(repo, "rev-parse", "HEAD") == before and git(repo, "status", "--porcelain") == ""
    assert not (repo / "src" / "feature.py").exists()
    events = [json.loads(x) for x in r.stdout.splitlines() if x.startswith('{"run_id"')]
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "started" and "exit" in kinds and any(e["message"] == "working..." for e in events)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))


@pytest.mark.parametrize("name,actions,failing_check", [
    ("outside_paths", [{"write": {"README.md": "hacked\n", "src/ok.py": "x=1\n"}}], "allowed_paths"),
    ("no_change", [{"print": ["I did nothing"]}], "agent_succeeded"),
    ("symlink_escape", [{"symlink": {"src/link": "../../../../etc/passwd"}}], "file_modes"),
    ("secret", [{"write": {"src/cfg.py": "KEY = 'ghp_" + "a" * 36 + "'\n"}}], "no_credentials_added"),
])
def test_bad_results_not_validated(tmp_path, repo, scenario, name, actions, failing_check):
    write_scenario(scenario, "adhoc", {"attempts": {"default": actions}})
    r, out = run_cli(tmp_path, scenario, repo, allow=("src/",))
    assert r.exit_code == 1 and not out["validated"]
    failed = [c["name"] for c in out["checks"] if not c["passed"]]
    assert failing_check in failed, out["checks"]


def test_failed_agent_exit_is_never_success_even_with_changes(tmp_path, repo, scenario):
    write_scenario(scenario, "adhoc", {"attempts": {"default": [{"write": {"src/x.py": "x=1\n"}, "exit": 7}]}})
    r, out = run_cli(tmp_path, scenario, repo)
    assert r.exit_code == 1 and not out["validated"] and out["error_code"] == "nonzero_exit" and out["exit_code"] == 7
    # partial work is still preserved on the attempt branch for diagnostics
    assert out["commit_sha"] and git(repo, "show", f"{out['commit_sha']}:src/x.py") == "x=1"


def test_timeout_terminates_agent_and_is_reported(tmp_path, repo, scenario):
    write_scenario(scenario, "adhoc", {"attempts": {"default": [
        {"spawn_background": ["/bin/sleep", "300"]}, {"sleep": 60}]}})
    pf = policy_file(tmp_path, scenario, orchestrator={"kill_grace_seconds": 0.5})
    r = runner.invoke(app, ["agents", "run", "alpha", "slow", "--repo", str(repo), "--policy", str(pf),
                            "--allow", "src/", "--timeout", "2", "--json"])
    out = json.loads([x for x in r.stdout.splitlines() if x.startswith("{")][-1])
    assert out["error_code"] == "timeout" and not out["validated"]


def test_missing_isolation_refuses_to_run(tmp_path, repo, scenario):
    pf = policy_file(tmp_path, scenario, isolation="rootless-container",
                     execution={"container_image": "img", "container_runtime": "podman"})
    r = runner.invoke(app, ["agents", "run", "alpha", "x", "--repo", str(repo), "--policy", str(pf), "--allow", "src/"])
    assert r.exit_code != 0 and (r.exception is not None or "unavailable" in r.output)


def test_out_of_policy_objective_rejected(tmp_path, repo, scenario):
    pf = policy_file(tmp_path, scenario)
    r = runner.invoke(app, ["agents", "run", "alpha", "deploy to production and git push origin main", "--repo",
                            str(repo), "--policy", str(pf), "--allow", "src/"])
    assert r.exit_code == 1 and "out of policy" in r.output


def test_doctor_and_agents_list(tmp_path, scenario):
    pf = policy_file(tmp_path, scenario)
    r = runner.invoke(app, ["doctor", "--policy", str(pf), "--json"])
    checks = {c["name"]: c for c in json.loads(r.stdout)}
    assert r.exit_code == 0 and checks["sandbox backend: userns"]["status"] == "ok"
    assert checks["agent: alpha"]["status"] == "ok"
    r = runner.invoke(app, ["agents", "list", "--policy", str(pf), "--json"])
    assert json.loads(r.stdout)[0]["enabled"] is True


def test_doctor_fails_closed_for_example_policy_without_runtime(tmp_path):
    from tests.support.helpers import REPO_ROOT
    r = runner.invoke(app, ["doctor", "--policy", str(REPO_ROOT / "config" / "policy.example.yaml")])
    assert r.exit_code == 1 and "required isolation" in r.stdout


def test_forbidden_path_in_task_rejected_before_any_execution(tmp_path, repo, scenario):
    write_scenario(scenario, "adhoc", {"attempts": {"default": [{"write": {".gitmodules": "x"}}]}})
    pf = policy_file(tmp_path, scenario)
    r = runner.invoke(app, ["agents", "run", "alpha", "x", "--repo", str(repo), "--policy", str(pf),
                            "--allow", ".gitmodules"])
    assert r.exit_code == 1 and "forbidden by policy" in r.output
    assert git(repo, "for-each-ref", "refs/heads/multigenz/") == ""  # nothing was even created
