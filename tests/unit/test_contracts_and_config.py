import pytest
from pydantic import ValidationError

from multigenz.config import ConfigError, load_policy, parse_policy
from multigenz.contracts.paths import normalize_pattern, path_matches, patterns_overlap
from multigenz.contracts.task import PlanDocument, TaskSpec, TaskStatus
from tests.support.helpers import REPO_ROOT


def mk(**kw):
    base = dict(task_id="t1", title="x", objective="do", allowed_paths=["src/"])
    base.update(kw)
    return TaskSpec(**base)


def test_task_defaults_match_contract():
    t = mk()
    assert (t.schema_version, t.max_attempts, t.timeout_seconds) == (1, 2, 900)
    assert t.dependencies == [] and t.required_capabilities == set()


@pytest.mark.parametrize("bad", ["../etc/passwd", "/abs", "a/../../b", "~/x", ".git/config", "a\\b", "", "x/.GIT/y"])
def test_paths_escaping_scope_rejected(bad):
    with pytest.raises(ValidationError):
        mk(allowed_paths=[bad])


@pytest.mark.parametrize("field,val", [("task_id", "Bad Id"), ("task_id", "../x"), ("max_attempts", 0),
                                       ("max_attempts", 6), ("timeout_seconds", 0), ("allowed_paths", []),
                                       ("dependencies", ["t1"]), ("title", " ")])
def test_invalid_task_fields(field, val):
    with pytest.raises(ValidationError):
        mk(**{field: val})


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        mk(command="rm -rf /")


def test_plan_document_roundtrip():
    doc = PlanDocument(tasks=[mk()])
    assert PlanDocument.model_validate_json(doc.model_dump_json()).tasks[0].task_id == "t1"


def test_status_terminal():
    assert TaskStatus.SUCCEEDED.terminal and TaskStatus.BLOCKED.terminal and not TaskStatus.RUNNING.terminal


def test_path_matching_and_overlap():
    assert path_matches("src/a/b.py", ["src/"]) and not path_matches("srcx/a.py", ["src/"])
    assert path_matches("src/a.py", ["src/*.py"]) and not path_matches("src/x/a.py", ["src/*.py"])
    assert path_matches("src/x/a.py", ["src/**/*.py"]) and path_matches("src/a.py", ["src/**/*.py"])
    assert patterns_overlap("src/", "src/a.py") and patterns_overlap("src/a/", "src/")
    assert not patterns_overlap("src/", "tests/") and not patterns_overlap("a.py", "b.py")
    assert patterns_overlap("src/*.py", "src/app.py") and not patterns_overlap("src/*.py", "docs/x.md")
    assert normalize_pattern("./src//app.py") == "src/app.py"


def test_example_policy_loads():
    p = load_policy(REPO_ROOT / "config" / "policy.example.yaml")
    assert p.orchestrator.max_parallel_agents == 3 and p.git.auto_push is False
    assert p.execution.isolation == "rootless-container"
    assert set(p.agents) == {"claude", "codex", "antigravity", "deepseek"}
    text, sha = p.snapshot()
    assert len(sha) == 64 and "claude" in text


@pytest.mark.parametrize("patch,msg", [
    ({"git": {"auto_push": True}}, "auto_push"),
    ({"git": {"protect_default_branch": False}}, "protect_default_branch"),
    ({"execution": {"isolation": "none"}}, "allow_unsandboxed"),
    ({"bogus": 1}, "bogus"),
    ({"orchestrator": {"max_parallel_agents": 0}}, "max_parallel_agents"),
    ({"agents": {"x": {"adapter": "cli"}}}, "command"),
    ({"agents": {"x": {"adapter": "litellm"}}}, "model"),
    ({"validation": {"default_profile": "nope"}}, "unknown validation profile"),
    ({"version": 2}, "version"),
])
def test_invalid_policy_fails_closed(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_policy(patch)


def test_missing_policy_file_is_error(tmp_path):
    with pytest.raises(ConfigError):
        load_policy(tmp_path / "nope.yaml")


def test_enable_claude_pytest_script_edits_a_policy_safely(tmp_path):
    import subprocess, sys
    from tests.support.helpers import REPO_ROOT
    src = (REPO_ROOT / "config" / "policy.claude.example.yaml").read_text().replace("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    f = tmp_path / "my-policy.yaml"
    f.write_text(src)
    run = lambda: subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "enable_claude_pytest.py"), str(f), "4"],
                                 capture_output=True, text=True)
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    pol = load_policy(f)
    assert [i.name for i in pol.agents["claude"].invocations] == ["print-accept-edits-pytest", "print-plan"]
    assert pol.orchestrator.max_task_attempts == 4 and "# must be on PATH" in f.read_text()  # comments survive
    assert (tmp_path / "my-policy.yaml.bak").read_text() == src
    again = run()
    assert again.returncode == 1 and "already exists" in again.stdout  # idempotent: never adds a second block
    for form in ("python -m pytest", "pytest", "python3 -B -m pytest", "/usr/bin/python3 -m pytest"):
        assert f"Bash({form}:*)" in f.read_text()  # every spelling Claude actually uses is allowed
    # a policy written by the earlier single-pattern version is upgraded in place
    old = tmp_path / "old.yaml"
    old.write_text(f.read_text().replace(
        f.read_text().split('"--allowedTools", ')[1].split(', "--no-session-persistence"')[0], '"Bash(python3 -m pytest:*)"'))
    up = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "enable_claude_pytest.py"), str(old)], capture_output=True, text=True)
    assert up.returncode == 0 and "Bash(pytest:*)" in old.read_text(), up.stdout + up.stderr
    assert [i.name for i in load_policy(old).agents["claude"].invocations] == ["print-accept-edits-pytest", "print-plan"]
