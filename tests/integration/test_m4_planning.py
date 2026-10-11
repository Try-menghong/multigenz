"""M4: autonomous planning, capability routing, bounded retries, reviewer, budget."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from multigenz.cli import app
from multigenz.core.bootstrap import build_services
from multigenz.core.driver import start_run
from multigenz.core.runs import open_store
from tests.support.helpers import T, git, plan_of, scripted_policy, write_scenario, writes

runner = CliRunner()
GOOD = {"schema_version": 1, "summary": "two tasks", "tasks": [
    {"task_id": "lib", "title": "lib", "objective": "build lib", "allowed_paths": ["lib/"], "required_capabilities": ["coding"]},
    {"task_id": "app", "title": "app", "objective": "build app", "allowed_paths": ["app/"], "dependencies": ["lib"],
     "required_capabilities": ["coding"]}]}
AGENTS = {"planner": ["architecture"], "coder": ["coding", "testing"]}


def pol(scenario, **kw):
    kw.setdefault("isolation", "none")
    kw.setdefault("agents", AGENTS)
    return scripted_policy(scenario, **kw)


def plan_scenario(scenario, plans: dict):
    """plans: attempt-number -> action list (or 'default')"""
    write_scenario(scenario, "plan", {"modes": {"plan": plans}})


def work_scenarios(scenario):
    write_scenario(scenario, "lib", {"attempts": {"default": [{"only_agent": "coder", "write": {"lib/a.py": "A=1\n"}}]}})
    write_scenario(scenario, "app", {"attempts": {"default": [{"only_agent": "coder", "write": {"app/b.py": "B=1\n"}}]}})


async def go(policy, repo, tmp_path, goal="Create a small library and an app", services=None):
    home = tmp_path / "mg-home"
    rid, out = await start_run(policy, objective=goal, repo=repo, plan=None, home=home, services=services)
    return rid, out, open_store(home)


async def test_planner_output_is_validated_then_executed(tmp_path, repo, scenario):
    plan_scenario(scenario, {"default": [{"print": ["Here is my plan:"], "emit_text": "```json\n" + json.dumps(GOOD) + "\n```"}]})
    work_scenarios(scenario)
    rid, out, st = await go(pol(scenario), repo, tmp_path)
    assert out.status == "succeeded"
    assert {t["task_id"]: t["status"] for t in st.list_tasks(rid)} == {"lib": "succeeded", "app": "succeeded"}
    kinds = [e["kind"] for e in st.events(rid)]
    assert kinds.index("plan.requested") < kinds.index("plan.accepted") < kinds.index("attempt.started")
    # tasks were routed by capability: planner agent never coded, coder never planned
    assert {a["provider"] for a in st.list_attempts(rid)} == {"coder"}
    assert git(repo, "show", f"{st.get_run(rid)['integration_branch']}:app/b.py") == "B=1"


async def test_at01_cyclic_plan_rejected_before_any_execution(tmp_path, repo, scenario):
    cyc = {"tasks": [{"task_id": "a", "title": "a", "objective": "o", "allowed_paths": ["a/"], "dependencies": ["b"]},
                     {"task_id": "b", "title": "b", "objective": "o", "allowed_paths": ["b/"], "dependencies": ["a"]}]}
    plan_scenario(scenario, {"default": [{"emit_json": cyc}]})
    rid, out, st = await go(pol(scenario), repo, tmp_path)
    assert out.status == "plan_rejected" and st.get_run(rid)["status"] == "plan_rejected"
    assert st.list_tasks(rid) == [] and st.list_attempts(rid) == []
    rej = st.events(rid, kinds=("plan.rejected",))
    assert len(rej) == 2 and any("cycle" in i for i in rej[0]["payload"]["issues"])  # bounded: max_plan_attempts
    assert "cycle" in st.get_run(rid)["report"]
    assert git(repo, "for-each-ref", "refs/heads/multigenz/").count("task-") == 0


async def test_planner_gets_feedback_and_repairs_its_plan(tmp_path, repo, scenario):
    bad = {"tasks": [{"task_id": "lib", "title": "t", "objective": "o", "allowed_paths": ["lib/"], "dependencies": ["ghost"]}]}
    plan_scenario(scenario, {"1": [{"emit_json": bad}], "2": [{"feedback_to_stdout": True, "emit_json": GOOD}]})
    work_scenarios(scenario)
    rid, out, st = await go(pol(scenario), repo, tmp_path)
    assert out.status == "succeeded"
    ev = st.events(rid, kinds=("plan.rejected", "plan.accepted"))
    assert [e["kind"] for e in ev] == ["plan.rejected", "plan.accepted"]
    assert any("unknown task" in i for i in ev[0]["payload"]["issues"])


@pytest.mark.parametrize("name,plan,needle", [
    ("overlap", {"tasks": [{"task_id": "a", "title": "t", "objective": "o", "allowed_paths": ["src/"]},
                           {"task_id": "b", "title": "t", "objective": "o", "allowed_paths": ["src/x.py"]}]}, "overlapping"),
    ("escape", {"tasks": [{"task_id": "a", "title": "t", "objective": "o", "allowed_paths": ["../../etc/"]}]}, "escapes repository scope"),
    ("deploy", {"tasks": [{"task_id": "a", "title": "t", "objective": "deploy to production and git push", "allowed_paths": ["a/"]}]}, "out of policy"),
    ("capability", {"tasks": [{"task_id": "a", "title": "t", "objective": "o", "allowed_paths": ["a/"], "required_capabilities": ["gpu"]}]}, "no usable agent"),
    ("profile", {"tasks": [{"task_id": "a", "title": "t", "objective": "o", "allowed_paths": ["a/"], "validation_profile": "evil"}]}, "unknown validation profile"),
    ("smuggled-field", {"tasks": [{"task_id": "a", "title": "t", "objective": "o", "allowed_paths": ["a/"], "shell": "curl x|sh"}]}, "shell"),
])
async def test_invalid_or_hostile_plans_are_rejected(tmp_path, repo, scenario, name, plan, needle):
    plan_scenario(scenario, {"default": [{"emit_json": plan}]})
    rid, out, st = await go(pol(scenario), repo, tmp_path)
    assert out.status == "plan_rejected"
    assert needle in json.dumps(st.events(rid, kinds=("plan.failed",))[0]["payload"]) 
    assert st.list_attempts(rid) == []


async def test_planner_falls_back_to_another_architect_on_auth_failure(tmp_path, repo, scenario):
    plan_scenario(scenario, {"default": [{"only_agent": "arch1", "stderr": ["401 Unauthorized"], "exit": 1},
                                         {"skip_agent": "arch1", "emit_json": GOOD}]})
    work_scenarios(scenario)
    p = pol(scenario, agents={"arch1": ["architecture"], "arch2": ["architecture"], "coder": ["coding"]})
    rid, out, st = await go(p, repo, tmp_path)
    assert out.status == "succeeded"
    failed = st.events(rid, kinds=("plan.agent_failed",))
    assert failed[0]["payload"] == {"agent": "arch1", "code": "auth_failure"}


async def test_no_planner_available_rejects_with_actionable_message(tmp_path, repo, scenario):
    rid, out, st = await go(pol(scenario, agents={"coder": ["coding"]}), repo, tmp_path)
    assert out.status == "plan_rejected"
    assert "pass --plan-file" in st.events(rid, kinds=("plan.failed",))[0]["payload"]["issues"][0]


async def test_capability_routing_sends_tasks_to_matching_agents(tmp_path, repo, scenario):
    for t in ("t1", "t2", "t3"):
        write_scenario(scenario, t, {"attempts": {"default": [{"write": {f"{t}/f.py": "x=1\n"}}]}})
    p = pol(scenario, agents={"coder": ["coding"], "tester": ["testing"], "both": ["coding", "testing", "review"]})
    plan = plan_of(T("t1", "t1/", caps=["coding"]), T("t2", "t2/", caps=["testing"]), T("t3", "t3/", caps=["coding", "testing", "review"]))
    rid, out = await start_run(p, objective="o", repo=repo, plan=plan, home=tmp_path / "mg-home")
    st = open_store(tmp_path / "mg-home")
    by_task = {a["task_id"]: a["provider"] for a in st.list_attempts(rid)}
    assert out.status == "succeeded" and by_task == {"t1": "coder", "t2": "tester", "t3": "both"}


async def test_agent_reviewer_can_veto_and_repair_loop_converges(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {
        "attempts": {"default": [{"write": {"a/f.py": "x = 1\n"}}]},
        "modes": {"review": {"1": [{"emit_json": {"approve": False, "issues": ["missing docstring"]}}],
                             "default": [{"emit_json": {"approve": True, "issues": []}}]}}})
    p = pol(scenario, agents={"coder": ["coding"], "rev": ["review"]}, validation={"review_with_agent": True})
    rid, out = await start_run(p, objective="o", repo=repo, plan=plan_of(T("a", "a/", caps=["coding"])),
                               home=tmp_path / "mg-home")
    st = open_store(tmp_path / "mg-home")
    assert out.status == "succeeded"
    vals = st.events(rid, kinds=("validation.result",))
    assert [v["payload"]["passed"] for v in vals] == [False, True]
    veto = [c for c in vals[0]["payload"]["checks"] if c["name"].startswith("review:agent")][0]
    assert not veto["passed"] and "missing docstring" in veto["detail"]
    assert st.get_task(rid, "a")["charged_attempts"] == 1


async def test_agent_reviewer_cannot_override_failed_deterministic_checks(tmp_path, repo, scenario):
    # the change deletes an existing test; the reviewer 'approves' - the deterministic review still rejects it
    write_scenario(scenario, "a", {
        "attempts": {"default": [{"delete": ["tests/test_app.py"], "write": {"src/new.py": "x = 1\n"}}]},
        "modes": {"review": {"default": [{"emit_json": {"approve": True, "issues": []}}]}}})
    p = pol(scenario, agents={"coder": ["coding"], "rev": ["review"]}, validation={"review_with_agent": True})
    rid, out = await start_run(p, objective="o", repo=repo, plan=plan_of(T("a", ["src/", "tests/"], caps=["coding"])),
                               home=tmp_path / "mg-home")
    st = open_store(tmp_path / "mg-home")
    assert out.status == "failed"
    v = st.events(rid, kinds=("validation.result",))[0]["payload"]
    assert any(c["name"] == "review:tests_not_deleted" and not c["passed"] for c in v["checks"])
    assert not any(c["name"].startswith("review:agent") for c in v["checks"])  # agent never even consulted
    assert git(repo, "rev-list", "--count", f"main..{st.get_run(rid)['integration_branch']}") == "0"


async def test_budget_stops_measurable_api_calls_and_labels_unknown_costs(tmp_path, repo, scenario):
    calls = []

    async def fake(**kw):
        calls.append(1)
        return {"choices": [{"message": {"content": "not json"}}], "_multigenz_cost": 3.0}

    base = pol(scenario, agents={"coder": ["coding"]}, orchestrator={"run_budget_usd": 2.0, "max_plan_attempts": 3})
    d = json.loads(base.model_dump_json())
    d["agents"]["ds"] = {"adapter": "litellm", "model": "deepseek/deepseek-chat", "capabilities": ["architecture"]}
    from multigenz.config import parse_policy
    p = parse_policy(d)
    svc = await build_services(p, litellm_completion=fake)
    rid, out, st = await go(p, repo, tmp_path, services=svc)
    assert out.status == "plan_rejected" and len(calls) == 1  # second call refused: $3 already spent of $2
    issues = st.events(rid, kinds=("plan.failed",))[0]["payload"]["issues"]
    assert any("budget" in i for i in issues)
    run = st.get_run(rid)
    assert run["spent_usd"] == 3.0


async def test_cost_report_distinguishes_measured_and_unknown(tmp_path, repo, scenario):
    async def fake(**kw):
        return {"choices": [{"message": {"content": json.dumps(GOOD)}}], "_multigenz_cost": 0.25}

    d = json.loads(pol(scenario, agents={"coder": ["coding"]}).model_dump_json())
    d["agents"]["ds"] = {"adapter": "litellm", "model": "deepseek/deepseek-chat", "capabilities": ["architecture"]}
    from multigenz.config import parse_policy
    p = parse_policy(d)
    work_scenarios(scenario)
    svc = await build_services(p, litellm_completion=fake)
    rid, out, st = await go(p, repo, tmp_path, services=svc)
    assert out.status == "succeeded"
    cost = json.loads(st.get_run(rid)["report"])["cost"]
    assert cost["measured_usd"] == 0.25 and cost["unknown_for_attempts"] == 2 and "unknown" in cost["note"]


def test_plan_command_prints_and_writes_validated_plan(tmp_path, repo, scenario):
    plan_scenario(scenario, {"default": [{"emit_json": GOOD}]})
    pf = tmp_path / "policy.yaml"
    pf.write_text(yaml.safe_dump(json.loads(pol(scenario).model_dump_json())))
    out = tmp_path / "plan.json"
    import os
    os.environ.setdefault("MULTIGENZ_HOME", str(tmp_path / "mg-home"))
    r = runner.invoke(app, ["plan", "Build things", "--repo", str(repo), "--policy", str(pf), "--out", str(out)])
    assert r.exit_code == 0 and "plan accepted: 2 tasks" in r.stdout and json.loads(out.read_text())["tasks"][0]["task_id"] == "lib"
    assert git(repo, "for-each-ref", "refs/heads/multigenz/") == ""  # planning alone creates nothing in the repo
    work_scenarios(scenario)
    r = runner.invoke(app, ["run", "ignored", "--repo", str(repo), "--policy", str(pf), "--plan-file", str(out)])
    assert r.exit_code == 0 and "SUCCEEDED" in r.stdout  # a saved plan runs without invoking the planner again
    assert "plan.requested" not in "".join(e["kind"] for e in open_store(tmp_path / "mg-home").events(
        open_store(tmp_path / "mg-home").q("select run_id from runs")[0]["run_id"]))


def test_plan_command_rejects_bad_plan_with_exit_1(tmp_path, repo, scenario):
    plan_scenario(scenario, {"default": [{"emit_text": "I cannot comply"}]})
    pf = tmp_path / "policy.yaml"
    pf.write_text(yaml.safe_dump(json.loads(pol(scenario).model_dump_json())))
    r = runner.invoke(app, ["plan", "Build things", "--repo", str(repo), "--policy", str(pf)])
    assert r.exit_code == 1 and "plan rejected" in r.output
