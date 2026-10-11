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
