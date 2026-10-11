import pytest

from multigenz.contracts.task import TaskSpec
from multigenz.core.budget import Budget, BudgetExceeded
from multigenz.core.planner import AgentPlanner
from multigenz.core.readonly import extract_json_object
from multigenz.execution.git import Git
from multigenz.integration.reviewer import DiffReviewer, is_test_path
from tests.unit.test_diff_validator import commit_changes


def test_extract_json_variants():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('Sure! Here is the plan:\n```json\n{"a": {"b": [1, 2]}}\n```\nDone.') == {"a": {"b": [1, 2]}}
    assert extract_json_object('prefix {"x": "}"} suffix') == {"x": "}"}
    assert extract_json_object("no json at all") is None
    assert extract_json_object("[1,2,3]") is None


def test_plan_parse_errors_are_specific():
    plan, issues = AgentPlanner._parse("nothing")
    assert plan is None and "JSON object" in issues[0]
    plan, issues = AgentPlanner._parse('{"tasks": [{"task_id": "Bad Id", "title": "t", "objective": "o", "allowed_paths": ["../x"]}]}')
    assert plan is None and any("task_id" in i for i in issues) and any("allowed_paths" in i for i in issues)
    plan, issues = AgentPlanner._parse('{"tasks": [{"task_id": "a", "title": "t", "objective": "o", "allowed_paths": ["a/"], "run": "rm -rf /"}]}')
    assert plan is None and any("run" in i for i in issues)  # unknown fields are errors, not ignored


def test_budget_blocks_measurable_calls_only_after_exhaustion():
    b = Budget(5.0)
    b.check("x")
    b.record(3.0)
    b.check("x")
    b.record(2.5)
    with pytest.raises(BudgetExceeded):
        b.check("another api call")
    unlimited = Budget(None)
    unlimited.record(1e9)
    unlimited.check("x")
    b2 = Budget(1.0)
    b2.record(None)
    assert b2.unknown and b2.spent == 0.0


def test_is_test_path():
    for p in ("tests/a.py", "src/test_x.py", "pkg/x_test.go", "web/app.test.ts", "spec/x.rb", "a/__tests__/b.js"):
        assert is_test_path(p), p
    for p in ("src/app.py", "docs/testing.md", "latest/readme"):
        assert not is_test_path(p), p


@pytest.fixture
def rv(repo):
    g = Git(repo, run_id="v1")
    return g, g.head(), repo


def review(g, base, head):
    t = TaskSpec(task_id="t", title="t", objective="o", allowed_paths=["tests/", "src/"])
    return {c.name: c for c in DiffReviewer(g).review(t, base, head)}


def test_reviewer_flags_deleted_tests(rv):
    g, base, repo = rv
    head = commit_changes(g, repo, base, lambda p: (p / "tests" / "test_app.py").unlink())
    r = review(g, base, head)
    assert not r["review:tests_not_deleted"].passed and r["review:no_skipped_tests"].passed


def test_reviewer_flags_skip_markers_and_gutted_assertions(rv):
    g, base, repo = rv
    many = "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n" + "".join(
        f"        self.assertEqual({i}, {i})\n" for i in range(6))
    (repo / "tests" / "test_many.py").write_text(many)
    from tests.support.helpers import git as sgit
    sgit(repo, "add", "-A"); sgit(repo, "commit", "-qm", "more tests")
    base2 = g.head()
    skipped = many.replace("class T", "@unittest.skip('later')\nclass T")
    head = commit_changes(g, repo, base2, lambda p: (p / "tests" / "test_many.py").write_text(skipped), name="s")
    assert not review(g, base2, head)["review:no_skipped_tests"].passed
    gutted = "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
    head = commit_changes(g, repo, base2, lambda p: (p / "tests" / "test_many.py").write_text(gutted), name="g")
    assert not review(g, base2, head)["review:assertions_not_removed"].passed


def test_reviewer_passes_ordinary_change(rv):
    g, base, repo = rv
    head = commit_changes(g, repo, base, lambda p: (p / "src" / "n.py").write_text("x = 1\n"))
    assert all(c.passed for c in review(g, base, head).values())
