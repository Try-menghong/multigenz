"""planning: auto - one agent takes the whole goal and asks for subagents only when needed (scripted agents)."""

from multigenz.core.driver import start_run
from multigenz.core.runs import open_store
from multigenz.core.split import GOAL_TASK_ID
from multigenz.core.replay import replay_run
from tests.support.helpers import git, scripted_policy, write_scenario

LIB = {"task_id": "lib", "title": "lib", "objective": "build lib", "allowed_paths": ["lib/"],
       "required_capabilities": ["coding"]}
APP = {"task_id": "app", "title": "app", "objective": "build app", "allowed_paths": ["app/"], "dependencies": ["lib"],
       "required_capabilities": ["coding"]}


def pol(scenario, planning="auto", **kw):
    p = scripted_policy(scenario, isolation="none", agents={"worker": ["coding"], "planner": ["architecture"]}, **kw)
    return p.model_copy(update={"orchestrator": p.orchestrator.model_copy(update={"planning": planning})})


async def run(policy, repo, tmp_path, goal="Build a library and an app"):
    home = tmp_path / "mg-home"
    rid, out = await start_run(policy, objective=goal, repo=repo, plan=None, home=home)
    return rid, out, open_store(home)


def work(scenario):
    write_scenario(scenario, "lib", {"attempts": {"default": [{"write": {"lib/a.py": "A=1\n"}}]}})
    write_scenario(scenario, "app", {"attempts": {"default": [{"write": {"app/b.py": "B=1\n"}}]}})


async def test_small_goal_is_one_agent_run_and_no_planner_call(tmp_path, repo, scenario):
    write_scenario(scenario, GOAL_TASK_ID, {"attempts": {"default": [{"write": {"src/x.py": "X=1\n"}}]}})
    rid, out, st = await run(pol(scenario), repo, tmp_path, goal="Add X")
    assert out.status == "succeeded"
    assert [e["kind"] for e in st.events(rid, kinds=("plan.requested",))] == []        # no planning run at all
    attempts = st.list_attempts(rid)
    assert len(attempts) == 1 and attempts[0]["provider"] == "worker"                  # a worker, not the planner
    assert git(repo, "show", f"{st.get_run(rid)['integration_branch']}:src/x.py") == "X=1"


async def test_agent_asks_for_subagents_and_they_do_the_work(tmp_path, repo, scenario):
    write_scenario(scenario, GOAL_TASK_ID, {"attempts": {"default": [
        {"print": ["This is two separate pieces of work."], "emit_json": {"subtasks": [LIB, APP]}}]}})
    work(scenario)
    rid, out, st = await run(pol(scenario), repo, tmp_path)
    assert out.status == "succeeded"
    tasks = {t["task_id"]: t["status"] for t in st.list_tasks(rid)}
    assert tasks == {GOAL_TASK_ID: "split", "lib": "succeeded", "app": "succeeded"}
    split = st.events(rid, kinds=("task.split",))[0]["payload"]
    assert split == {"agent": "worker", "subtasks": ["lib", "app"]}
    branch = st.get_run(rid)["integration_branch"]
    assert git(repo, "show", f"{branch}:lib/a.py") == "A=1" and git(repo, "show", f"{branch}:app/b.py") == "B=1"
    order = [a["task_id"] for a in sorted(st.list_attempts(rid), key=lambda a: a["started_at"])]
    assert order == [GOAL_TASK_ID, "lib", "app"]                                         # app waited for lib
    assert replay_run(st, rid).ok                                                         # split is auditable


async def test_invalid_request_is_rejected_with_reasons_and_the_agent_can_correct_it(tmp_path, repo, scenario):
    cyclic = [dict(LIB, dependencies=["app"]), APP]
    write_scenario(scenario, GOAL_TASK_ID, {"attempts": {"1": [{"emit_json": {"subtasks": cyclic}}],
                                                         "default": [{"emit_json": {"subtasks": [LIB, APP]}}]}})
    work(scenario)
    rid, out, st = await run(pol(scenario), repo, tmp_path)
    assert out.status == "succeeded"
    rejected = st.events(rid, kinds=("split.rejected",))
    assert len(rejected) == 1 and any("cycle" in i for i in rejected[0]["payload"]["issues"])
    goal = st.get_task(rid, GOAL_TASK_ID)
    assert goal["status"] == "split" and goal["charged_attempts"] == 1


async def test_single_subtask_or_stolen_ids_are_refused(tmp_path, repo, scenario):
    write_scenario(scenario, GOAL_TASK_ID, {"attempts": {"default": [{"emit_json": {"subtasks": [LIB]}}]}})
    rid, out, st = await run(pol(scenario, orchestrator={"max_task_attempts": 1}), repo, tmp_path)
    assert out.status == "failed"
    assert "at least 2 subtasks" in st.events(rid, kinds=("split.rejected",))[0]["payload"]["issues"][0]
    assert st.get_task(rid, GOAL_TASK_ID)["status"] == "failed"


async def test_request_together_with_file_changes_is_ignored_and_the_work_validated(tmp_path, repo, scenario):
    write_scenario(scenario, GOAL_TASK_ID, {"attempts": {"default": [
        {"write": {"src/y.py": "Y=1\n"}, "emit_json": {"subtasks": [LIB, APP]}}]}})
    rid, out, st = await run(pol(scenario), repo, tmp_path)
    assert out.status == "succeeded"
    assert st.events(rid, kinds=("split.ignored",)) and {t["task_id"] for t in st.list_tasks(rid)} == {GOAL_TASK_ID}


async def test_planning_never_means_no_splitting(tmp_path, repo, scenario):
    write_scenario(scenario, GOAL_TASK_ID, {"attempts": {"default": [{"emit_json": {"subtasks": [LIB, APP]}}]}})
    rid, out, st = await run(pol(scenario, planning="never", orchestrator={"max_task_attempts": 1}), repo, tmp_path)
    assert out.status == "failed"                       # the agent did nothing; a request is not honoured
    assert st.events(rid, kinds=("task.split",)) == [] and len(st.list_tasks(rid)) == 1
    goal = st.get_task(rid, GOAL_TASK_ID)
    assert goal["status"] == "failed" and "no changes" in goal["failure_reason"], goal["failure_reason"]
