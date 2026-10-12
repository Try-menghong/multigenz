"""roles decide who plans, who codes and who reviews in a real orchestrated run (scripted stand-in agents)."""

import json

from multigenz.core.driver import start_run
from multigenz.core.runs import open_store
from tests.support.helpers import git, scripted_policy, write_scenario

FULL = ["architecture", "coding", "testing", "review"]
PLAN = {"schema_version": 1, "summary": "two tasks", "tasks": [
    {"task_id": "lib", "title": "lib", "objective": "build lib", "allowed_paths": ["lib/"], "required_capabilities": ["coding"]},
    {"task_id": "app", "title": "app", "objective": "build app", "allowed_paths": ["app/"], "dependencies": ["lib"],
     "required_capabilities": ["coding"]}]}
APPROVE = {"default": [{"emit_json": {"approve": True, "issues": []}}]}


def scenarios(scenario):
    write_scenario(scenario, "plan", {"modes": {"plan": {"default": [{"emit_text": json.dumps(PLAN)}]}}})
    write_scenario(scenario, "lib", {"attempts": {"default": [{"write": {"lib/a.py": "A=1\n"}}]}, "modes": {"review": APPROVE}})
    write_scenario(scenario, "app", {"attempts": {"default": [{"write": {"app/b.py": "B=1\n"}}]}, "modes": {"review": APPROVE}})


async def run(policy, repo, tmp_path):
    home = tmp_path / "mg-home"
    rid, out = await start_run(policy, objective="Create a small library and an app", repo=repo, plan=None, home=home)
    return rid, out, open_store(home)


async def test_without_roles_the_first_capable_agent_does_everything(tmp_path, repo, scenario):
    scenarios(scenario)
    p = scripted_policy(scenario, isolation="none", agents={"alpha": FULL, "beta": FULL, "gamma": FULL})
    rid, out, st = await run(p, repo, tmp_path)
    assert out.status == "succeeded"
    assert st.events(rid, kinds=("plan.accepted",))[0]["payload"]["agent"] == "alpha"
    assert {a["provider"] for a in st.list_attempts(rid)} == {"alpha"}


async def test_roles_choose_planner_workers_and_reviewer(tmp_path, repo, scenario):
    scenarios(scenario)
    p = scripted_policy(scenario, isolation="none", agents={"alpha": FULL, "beta": FULL, "gamma": FULL})
    p = p.model_copy(update={"roles": p.roles.model_copy(update={"planner": ["gamma"], "worker": ["beta"],
                                                                  "reviewer": ["alpha"]})})
    rid, out, st = await run(p, repo, tmp_path)
    assert out.status == "succeeded"
    assert st.events(rid, kinds=("plan.accepted",))[0]["payload"]["agent"] == "gamma"   # the chosen planner
    assert {a["provider"] for a in st.list_attempts(rid)} == {"beta"}                    # only the chosen worker coded
    reviews = [c for v in st.events(rid, kinds=("validation.result",)) for c in v["payload"]["checks"]
               if c["name"].startswith("review:agent")]
    assert reviews and {c["name"] for c in reviews} == {"review:agent[alpha]"}           # reviewer role turned review on
    assert git(repo, "show", f"{st.get_run(rid)['integration_branch']}:app/b.py") == "B=1"


async def test_preferred_worker_failing_falls_back_to_the_next_one(tmp_path, repo, scenario):
    scenarios(scenario)
    p = scripted_policy(scenario, isolation="none", agents={"alpha": FULL, "beta": FULL},
                        agent_extra={"beta": {"command": str(tmp_path / "no-such-binary")}})
    p = p.model_copy(update={"roles": p.roles.model_copy(update={"planner": ["alpha"], "worker": ["beta", "alpha"]})})
    rid, out, st = await run(p, repo, tmp_path)
    assert out.status == "succeeded"
    assert {a["provider"] for a in st.list_attempts(rid)} == {"alpha"}                   # beta unavailable -> alpha
