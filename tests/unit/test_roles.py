"""roles: which agent plans, which code, which review, which resolve conflicts."""

from types import SimpleNamespace

import pytest

from multigenz.config import parse_policy
from multigenz.contracts.agent import AdapterProbe
from multigenz.contracts.task import TaskStatus
from multigenz.core import scheduler as sched
from multigenz.core.planner import AgentPlanner
from multigenz.errors import ConfigError, PlanValidationError
from multigenz.integration.reviewer import AgentReviewer
from tests.support.helpers import T
from tests.unit.test_dag_scheduler import view

ALL_MODES = ["code", "conflict", "plan", "review", "analysis"]
AGENTS = {n: {"adapter": "cli", "command": "x", "capabilities": ["architecture", "coding", "testing", "review"]}
          for n in ("claude", "codex", "opencode", "cline")}


def pol(roles=None, **extra):
    return parse_policy({"agents": AGENTS, "roles": roles or {}, "orchestrator": {"max_parallel_agents": 4}, **extra})


def probes(**overrides):
    out = {n: AdapterProbe(name=n, enabled=True, capabilities=AGENTS[n]["capabilities"], modes=ALL_MODES)
           for n in AGENTS}
    out.update(overrides)
    return out


def dispatch(policy, ready, running=(), pr=None):
    return sched.pick_dispatch(ready, [], policy=policy, probes=pr or probes(), klass_of={n: "cli" for n in AGENTS},
                               disabled=set(), running_agents=list(running))


def test_policy_rejects_unknown_duplicate_and_text_only_workers():
    with pytest.raises(ConfigError, match="unknown agent 'ghost'"):
        pol({"worker": ["ghost"]})
    with pytest.raises(ConfigError, match="more than once"):
        pol({"planner": ["claude", "claude"]})
    with pytest.raises(ConfigError, match="cannot change files"):
        parse_policy({"agents": {"ds": {"adapter": "litellm", "model": "m"}}, "roles": {"worker": ["ds"]}})
    p = parse_policy({"agents": {"ds": {"adapter": "litellm", "model": "m"}}, "roles": {"planner": ["ds"]}})
    assert p.roles.planner == ["ds"]                         # a text-only agent may plan or review
    assert parse_policy({}).roles.for_mode("code") == []     # no roles = automatic


def test_workers_restrict_routing_and_their_order_is_the_preference():
    ready = [view(T("t", "t/"), TaskStatus.READY)]
    assert dispatch(pol(), ready) == [("t", "claude")]                       # automatic: policy order
    assert dispatch(pol({"worker": ["opencode", "codex"]}), ready) == [("t", "opencode")]
    # parallel tasks spread over the workers (least loaded first), never to a non-worker
    many = [view(T(f"t{i}", f"p{i}/"), TaskStatus.READY) for i in range(4)]
    picks = dispatch(pol({"worker": ["opencode", "codex"]}), many)
    assert [a for _, a in picks] == ["opencode", "codex", "opencode", "codex"]
    # the preferred worker unavailable -> the next one; none left -> unroutable (reported, not silently rerouted)
    pr = probes(opencode=AdapterProbe(name="opencode", enabled=False))
    assert dispatch(pol({"worker": ["opencode", "codex"]}), ready, pr=pr) == [("t", "codex")]
    pr = probes(opencode=AdapterProbe(name="opencode", enabled=False), codex=AdapterProbe(name="codex", enabled=False))
    assert dispatch(pol({"worker": ["opencode", "codex"]}), ready, pr=pr) == []
    assert sched.unroutable(ready, probes=pr, disabled=set(), roles=pol({"worker": ["opencode", "codex"]}).roles) == ["t"]


def test_conflict_resolvers_default_to_the_workers():
    conflict = view(T("t", "t/"), TaskStatus.READY)
    conflict = conflict.__class__(**{**conflict.__dict__, "kind": "conflict"})
    assert conflict.mode == "conflict"
    assert dispatch(pol({"worker": ["codex"]}), [conflict]) == [("t", "codex")]
    assert dispatch(pol({"worker": ["codex"], "conflict": ["cline"]}), [conflict]) == [("t", "cline")]


def svc(policy, pr=None):
    return SimpleNamespace(policy=policy, probes=pr or probes())


def test_planner_role_picks_and_orders_planners_without_needing_the_capability():
    p = pol({"planner": ["cline", "claude"]})
    pr = probes(cline=AdapterProbe(name="cline", enabled=True, capabilities=["coding"], modes=["plan", "code"]))
    assert AgentPlanner(svc(p, pr), runner=None)._planner_agents() == ["cline", "claude"]
    assert AgentPlanner(svc(pol(), pr), runner=None)._planner_agents() == ["claude", "codex", "opencode"]  # automatic


async def test_planner_role_with_no_usable_planner_says_which_role_failed():
    p = pol({"planner": ["cline"]})
    pr = probes(cline=AdapterProbe(name="cline", enabled=True, modes=["code"]))  # no verified plan invocation
    services = SimpleNamespace(policy=p, probes=pr, engine=SimpleNamespace(check_text=lambda *a, **k: None))
    planner = AgentPlanner(services, runner=None)
    with pytest.raises(PlanValidationError, match=r"roles.planner \(cline\)"):
        await planner.plan("goal", "HEAD", [])


def test_reviewer_role_selects_reviewers_in_order():
    r = AgentReviewer(svc(pol({"reviewer": ["cline", "codex"]})), runner=None, git=None, redactor=None)
    assert r._agents() == ["cline", "codex"]
    r = AgentReviewer(svc(pol()), runner=None, git=None, redactor=None)
    assert r._agents() == list(AGENTS)                     # automatic: every review-capable agent
