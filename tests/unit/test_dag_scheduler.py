import random
import time

import pytest

from multigenz.config import parse_policy
from multigenz.contracts.agent import AdapterProbe
from multigenz.contracts.task import PlanDocument, TaskStatus
from multigenz.core import scheduler as sched
from multigenz.core.backoff import backoff_delay
from multigenz.core.dag import assert_valid_plan, find_cycle, topological_order, validate_plan
from multigenz.core.policy import PolicyEngine
from multigenz.errors import PlanValidationError
from tests.support.helpers import T, plan_of

S = TaskStatus


def test_at01_cyclic_dependencies_rejected():
    plan = plan_of(T("a", "a/", ["c"]), T("b", "b/", ["a"]), T("c", "c/", ["b"]))
    issues = validate_plan(plan)
    assert any("cycle" in i for i in issues)
    with pytest.raises(PlanValidationError, match="cycle"):
        assert_valid_plan(plan)
    assert find_cycle(plan.tasks) == ["a", "b", "c"]


def test_self_cycle_via_two_nodes_and_valid_dag():
    assert find_cycle(plan_of(T("a", "a/", ["b"]), T("b", "b/", ["a"])).tasks) == ["a", "b"]
    p = plan_of(T("a", "a/"), T("b", "b/", ["a"]), T("c", "c/", ["a"]), T("d", "d/", ["b", "c"]))
    assert validate_plan(p) == [] and topological_order(p.tasks)[0] == "a" and topological_order(p.tasks)[-1] == "d"


def test_duplicate_ids_and_unknown_dependencies():
    assert any("duplicate" in i for i in validate_plan(plan_of(T("a", "a/"), T("a", "b/"))))
    assert any("unknown task" in i for i in validate_plan(plan_of(T("a", "a/", ["ghost"]))))
    assert validate_plan(PlanDocument(tasks=[])) == ["plan has no tasks"]


def test_exclusive_ownership_conflict_needs_dependency():
    assert any("overlapping" in i for i in validate_plan(plan_of(T("a", "src/"), T("b", "src/app.py"))))
    assert validate_plan(plan_of(T("a", "src/"), T("b", "src/app.py", ["a"]))) == []  # serialised by dependency
    assert validate_plan(plan_of(T("a", "src/"), T("b", "tests/"))) == []
    assert validate_plan(plan_of(T("a", "src/", shared_paths=["README.md"]), T("b", "tests/", shared_paths=["README.md"]))) == []
    assert any("overlapping" in i for i in validate_plan(plan_of(T("a", "docs/", shared_paths=["src/x.py"]), T("b", "src/"))))


def test_capability_and_policy_checks():
    p = plan_of(T("a", "a/", caps=["quantum"]))
    assert any("no usable agent" in i for i in validate_plan(p, available_capabilities=[{"coding"}]))
    eng = PolicyEngine(parse_policy({}))
    bad = plan_of(T("a", "a/"))
    bad.tasks[0].objective = "deploy this to production"
    assert any("out of policy" in i for i in validate_plan(bad, eng))


# ---------------------------------------------------------------- scheduler
def view(spec, status=S.PENDING, excluded=(), nb=None, provider=None):
    return sched.TaskView(spec.task_id, spec, status, frozenset(excluded), nb, provider)


def probes(**caps):
    return {n: AdapterProbe(name=n, enabled=True, capabilities=c, modes=["code"]) for n, c in caps.items()}


def pol(**orch):
    return parse_policy({"orchestrator": orch, "agents": {
        "a1": {"adapter": "cli", "command": "x", "capabilities": ["coding"]},
        "a2": {"adapter": "cli", "command": "x", "capabilities": ["coding", "review"], "max_concurrent": 1}}})


def test_promotion_requires_all_prereqs_succeeded():
    a, b, c = T("a", "a/"), T("b", "b/", ["a"]), T("c", "c/", ["a", "b"])
    vs = [view(a, S.SUCCEEDED), view(b), view(c)]
    assert sched.promotable(vs) == ["b"]
    vs = [view(a, S.VALIDATING), view(b), view(c)]
    assert sched.promotable(vs) == []  # validated but not yet integrated != prerequisite satisfied


def test_failed_prerequisite_blocks_transitively():
    a, b, c = T("a", "a/"), T("b", "b/", ["a"]), T("c", "c/", ["b"])
    vs = [view(a, S.FAILED), view(b), view(c)]
    assert sched.blocked_by_failure(vs) == [("b", "a")]
    vs = [view(a, S.FAILED), view(b, S.BLOCKED), view(c)]
    assert ("c", "b") in sched.blocked_by_failure(vs)


def dispatch(ready, active=(), policy=None, running=(), disabled=(), now=None, pr=None):
    policy = policy or pol()
    return sched.pick_dispatch(list(ready), list(active), policy=policy, probes=pr or probes(a1=["coding"], a2=["coding", "review"]),
                               klass_of={"a1": "cli", "a2": "cli"}, disabled=set(disabled), running_agents=list(running), now=now)


def test_global_concurrency_bound():
    ready = [view(T(f"t{i}", f"p{i}/"), S.READY) for i in range(6)]
    picks = dispatch(ready, policy=pol(max_parallel_agents=3))
    assert len(picks) == 3
    picks = dispatch(ready, policy=pol(max_parallel_agents=3), running=[("a1", "cli")])
    assert len(picks) == 2


def test_per_agent_limit_and_routing():
    ready = [view(T(f"t{i}", f"p{i}/", caps=["review"]), S.READY) for i in range(3)]
    picks = dispatch(ready, policy=pol(max_parallel_agents=5))
    assert picks == [("t0", "a2")]  # only a2 has 'review', max_concurrent 1
    ready = [view(T("x", "x/"), S.READY, excluded=["a1"])]
    assert dispatch(ready) == [("x", "a2")]
    assert dispatch(ready, disabled=["a2"]) == [] and sched.unroutable(ready, probes=probes(a1=["coding"], a2=["coding"]), disabled={"a2"}) == ["x"]


def test_overlapping_exclusive_paths_serialised_and_backoff_respected():
    r1 = view(T("a", "src/"), S.READY)
    r2 = view(T("b", "src/lib/"), S.READY)
    assert dispatch([r1, r2]) == [("a", "a1")]
    assert dispatch([r2], active=[view(T("z", "src/"), S.RUNNING)]) == []
    waiting = view(T("w", "w/"), S.READY, nb=time.time() + 100)
    assert dispatch([waiting]) == [] and dispatch([waiting], now=time.time() + 200) == [("w", "a1")]


def test_only_verified_modes_route():
    pr = {"a1": AdapterProbe(name="a1", enabled=True, capabilities=["coding"], modes=["plan"])}
    assert sched.candidates(T("x", "x/").model_copy(), "code", pr, excluded=frozenset(), disabled=set()) == []


def test_backoff_capped_exponential_with_jitter():
    rng = random.Random(7)
    ds = [backoff_delay(n, base=1.0, cap=8.0, rng=rng) for n in range(6)]
    assert all(0.5 * min(8, 2 ** n) <= d <= min(8, 2 ** n) for n, d in enumerate(ds)) and max(ds) <= 8.0
    assert len({round(d, 6) for d in ds}) == len(ds)
