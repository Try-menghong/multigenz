"""Planning stays cheap: plan-only instructions, fewest tasks, read-only tools, a shorter timeout."""

import json
from types import SimpleNamespace

from multigenz.adapters.claude import ClaudeAdapter
from multigenz.config import parse_policy
from multigenz.contracts.agent import AdapterProbe
from multigenz.core.planner import AgentPlanner, planning_prompt

PLAN = {"tasks": [{"task_id": "a", "title": "a", "objective": "do a", "allowed_paths": ["a/"],
                   "required_capabilities": ["coding"]}]}


def test_prompt_tells_the_planner_to_only_plan_and_use_few_tasks():
    text = planning_prompt("Add a health endpoint", ["app.py", "tests/test_app.py"], ["coding"], ["unit"])
    assert "You only PLAN" in text
    assert "do not run commands or tests" in text and "do not try to solve the task" in text
    assert "as FEW tasks as possible" in text and "One task is right unless" in text
    assert "app.py" in text   # the file listing is given, so the planner need not explore


def test_claude_plans_with_read_only_tools_first_and_falls_back_on_older_versions():
    plan_invs = [i for i in ClaudeAdapter.BUILTIN_INVOCATIONS if "plan" in i.modes]
    assert [i.name for i in plan_invs] == ["print-plan-readonly-tools", "print-plan"]
    lean = plan_invs[0]
    assert lean.argv[lean.argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--tools" in lean.requires_help_tokens and "--tools" not in plan_invs[1].requires_help_tokens


async def test_planner_uses_the_plan_timeout_not_the_task_timeout():
    seen = {}

    class Runner:
        async def ask(self, agent, mode, **kw):
            seen.update(kw, agent=agent, mode=mode)
            return json.dumps(PLAN)

    pol = parse_policy({"agents": {"p": {"adapter": "cli", "command": "x", "capabilities": ["architecture", "coding"]}},
                        "orchestrator": {"task_timeout_seconds": 1200, "plan_timeout_seconds": 120}})
    probes = {"p": AdapterProbe(name="p", enabled=True, capabilities=["architecture", "coding"], modes=["plan", "code"])}
    svc = SimpleNamespace(policy=pol, probes=probes, engine=SimpleNamespace(check_text=lambda *a, **k: None,
                                                                            check_task=lambda *a, **k: []))
    plan = await AgentPlanner(svc, Runner()).plan("goal", "HEAD", ["a/x.py"])
    assert [t.task_id for t in plan.tasks] == ["a"]
    assert seen["mode"] == "plan" and seen["timeout"] == 120
