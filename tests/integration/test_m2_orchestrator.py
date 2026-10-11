"""M2: durable scheduler - parallelism, dependencies, leases, reassignment, timeouts, cancellation."""

import asyncio
import json
import os
import time

import pytest

from multigenz.core.bootstrap import build_services
from multigenz.core.driver import start_run
from multigenz.core.orchestrator import Orchestrator
from multigenz.core.replay import replay_run
from multigenz.core.runs import create_run, open_store
from tests.support.helpers import (T, git, max_concurrency, overlap_seconds, plan_of, scripted_policy,
                                   write_scenario, writes)


async def run(policy, repo, plan, tmp_path, **kw):
    home = tmp_path / "mg-home"
    rid, out = await start_run(policy, objective="test objective", repo=repo, plan=plan, home=home, **kw)
    store = open_store(home)
    return rid, out, store


def statuses(store, rid):
    return {t["task_id"]: t["status"] for t in store.list_tasks(rid)}


def pol(scenario, **kw):
    kw.setdefault("isolation", "none")
    return scripted_policy(scenario, **kw)


async def test_single_task_success_and_original_repo_untouched(tmp_path, repo, scenario):
    write_scenario(scenario, "a", writes("src/a.py", "A = 1\n"))
    (repo / "dirty.txt").write_text("user's uncommitted work")
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"), git(repo, "branch", "--show-current"))
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "src/a.py")), tmp_path)
    assert out.status == "succeeded" and out.final_commit
    run_row = st.get_run(rid)
    assert git(repo, "show", f"{run_row['integration_branch']}:src/a.py") == "A = 1"
    assert (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"), git(repo, "branch", "--show-current")) == before  # AT-12
    assert (repo / "dirty.txt").read_text() == "user's uncommitted work"
    assert not (repo / "src" / "a.py").exists()  # agent output never lands in the user's working tree
    assert git(repo, "rev-parse", "main") == before[0]


async def test_at02_independent_tasks_run_concurrently(tmp_path, repo, scenario):
    tasks = []
    for i in "abc":
        write_scenario(scenario, i, {"attempts": {"default": [{"write": {f"{i}/f.py": f"{i}=1\n"}, "sleep": 1.5}]}})
        tasks.append(T(i, f"{i}/"))
    rid, out, st = await run(pol(scenario), repo, plan_of(*tasks), tmp_path)
    assert out.status == "succeeded"
    atts = st.list_attempts(rid)
    assert max_concurrency(atts) == 3
    assert overlap_seconds(atts[0], atts[1]) > 0.5


async def test_concurrency_never_exceeds_limit(tmp_path, repo, scenario):
    tasks = []
    for i in range(5):
        write_scenario(scenario, f"t{i}", {"attempts": {"default": [{"write": {f"d{i}/f.py": "x=1\n"}, "sleep": 0.6}]}})
        tasks.append(T(f"t{i}", f"d{i}/"))
    rid, out, st = await run(pol(scenario, orchestrator={"max_parallel_agents": 2}), repo, plan_of(*tasks), tmp_path)
    assert out.status == "succeeded" and max_concurrency(st.list_attempts(rid)) == 2


async def test_at03_dependents_wait_for_validated_prerequisites_and_build_on_snapshot(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"lib/a.py": "A = 1\n"}, "sleep": 1.0}]}})
    write_scenario(scenario, "b", {"attempts": {"default": [{"read_files": ["lib/a.py"], "write": {"app/b.py": "B = 2\n"}}]}})
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "lib/"), T("b", "app/", ["a"])), tmp_path)
    assert out.status == "succeeded"
    a, b = st.list_attempts(rid, "a")[0], st.list_attempts(rid, "b")[0]
    assert b["started_at"] >= a["finished_at"]
    # b's worktree was created from the integration snapshot that contains a's validated commit
    assert git(repo, "show", f"{b['base_commit']}:lib/a.py") == "A = 1"
    out_log = open(f"{b['log_dir']}/stdout.log").read()
    assert "READ-OK lib/a.py: 'A = 1" in out_log
    ev = [e for e in st.events(rid, kinds=("task.transition",), task_id="b")]
    assert ev[0]["payload"]["to"] == "ready" and ev[0]["seq"] > [e for e in st.events(rid, kinds=("task.transition",), task_id="a")][-1]["seq"]


async def test_failed_prerequisite_blocks_dependents(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"exit": 3}]}})
    write_scenario(scenario, "b", writes("b/f.py", "x\n"))
    write_scenario(scenario, "c", writes("c/f.py", "x\n"))
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/"), T("b", "b/", ["a"]), T("c", "c/", ["b"])), tmp_path)
    assert out.status == "failed"
    assert statuses(st, rid) == {"a": "failed", "b": "blocked", "c": "blocked"}
    assert st.list_attempts(rid, "b") == []  # never executed
    assert git(repo, "rev-list", "--count", f"main..{st.get_run(rid)['integration_branch']}") == "0"


async def test_at08_failed_agent_cannot_produce_success(tmp_path, repo, scenario):
    # agent writes perfectly valid-looking changes but exits non-zero, on every attempt
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"a/ok.py": "ok = True\n"}, "print": ["DONE: all tests pass"], "exit": 1}]}})
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/")), tmp_path)
    assert out.status == "failed" and statuses(st, rid)["a"] == "failed"
    atts = st.list_attempts(rid, "a")
    assert len(atts) == 2 and all(a["status"] == "failed" and a["error_code"] == "nonzero_exit" for a in atts)
    assert git(repo, "rev-list", "--count", f"main..{st.get_run(rid)['integration_branch']}") == "0"
    assert st.events(rid, kinds=("validation.result",)) == []  # never even reached validation


async def test_validation_failure_then_bounded_repair(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {
        "1": [{"write": {"a/x.py": "x=1\n", "OUTSIDE.md": "nope\n"}}],
        "2": [{"feedback_to_stdout": True, "write": {"a/x.py": "x=2\n"}, "delete": ["OUTSIDE.md"]}]}})
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/")), tmp_path)
    assert out.status == "succeeded"
    first, second = st.list_attempts(rid, "a")
    assert first["status"] == "finished"
    t = st.get_task(rid, "a")
    assert t["charged_attempts"] == 1 and t["total_attempts"] == 2
    assert "SAW-FEEDBACK" in open(f"{second['log_dir']}/stdout.log").read()
    vals = st.events(rid, kinds=("validation.result",))
    assert [v["payload"]["passed"] for v in vals] == [False, True]
    assert any(c["name"] == "allowed_paths" and not c["passed"] for c in vals[0]["payload"]["checks"])
    assert second["base_commit"] == first["commit_sha"]  # repair continues from the failed attempt's commit
    assert git(repo, "show", f"{st.get_run(rid)['integration_branch']}:a/x.py") == "x=2"


async def test_at04_auth_failure_reassigns_to_compatible_provider(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [
        {"only_agent": "alpha", "stderr": ["error: 401 Unauthorized - not logged in"], "exit": 1},
        {"skip_agent": "alpha", "write": {"a/f.py": "ok=1\n"}}]}})
    p = pol(scenario, agents={"alpha": ["coding"], "beta": ["coding"]})
    rid, out, st = await run(p, repo, plan_of(T("a", "a/")), tmp_path)
    assert out.status == "succeeded"
    atts = st.list_attempts(rid, "a")
    assert [(a["provider"], a["error_code"]) for a in atts] == [("alpha", "auth_failure"), ("beta", None)]
    assert st.get_task(rid, "a")["charged_attempts"] == 0  # provider failure is not the task's fault
    assert st.provider_states(rid)["alpha"]["status"] == "disabled"
    assert json.loads(st.get_task(rid, "a")["excluded_providers"]) == ["alpha"]


async def test_at04_missing_binary_after_probe_reassigns(tmp_path, repo, scenario):
    write_scenario(scenario, "a", writes("a/f.py", "ok=1\n"))
    p = pol(scenario, agents={"alpha": ["coding"], "beta": ["coding"]})
    svc = await build_services(p)
    svc.adapters["alpha"]._binary = "/nonexistent/vanished-binary"  # binary disappears after the health probe
    rid, out, st = await run(p, repo, plan_of(T("a", "a/")), tmp_path, services=svc)
    assert out.status == "succeeded"
    assert [(a["provider"], a["error_code"]) for a in st.list_attempts(rid, "a")] == [("alpha", "binary_missing"), ("beta", None)]


async def test_no_compatible_provider_left_fails_task(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"stderr": ["401 unauthorized"], "exit": 1}]}})
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/")), tmp_path)
    t = st.get_task(rid, "a")
    assert out.status == "failed" and t["status"] == "failed" and t["failure_reason"] == "no_compatible_provider"


async def test_rate_limit_uses_capped_backoff_then_succeeds(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {
        "1": [{"stderr": ["HTTP 429 Too Many Requests: rate limit exceeded"], "exit": 1}],
        "2": [{"stderr": ["429 Too Many Requests"], "exit": 1}],
        "3": [{"write": {"a/f.py": "ok=1\n"}}]}})
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/")), tmp_path)
    assert out.status == "succeeded"
    t = st.get_task(rid, "a")
    assert t["rate_limit_retries"] == 2 and t["charged_attempts"] == 0
    delays = [e["payload"]["backoff_seconds"] for e in st.events(rid, kinds=("task.transition",)) if "backoff_seconds" in e["payload"]]
    assert len(delays) == 2 and all(0 < d <= 0.2 for d in delays)


async def test_at05_timeout_terminates_process_group_in_orchestrator(tmp_path, repo, scenario):
    pidfile = tmp_path / "bg.pid"
    write_scenario(scenario, "a", {"attempts": {"default": [
        {"spawn_background": ["/bin/sleep", "300"], "background_pid_file": str(pidfile)}, {"sleep": 60}]}})
    t0 = time.monotonic()
    rid, out, st = await run(pol(scenario, orchestrator={"max_task_attempts": 1}), repo,
                             plan_of(T("a", "a/", timeout_seconds=2, max_attempts=1)), tmp_path)
    assert time.monotonic() - t0 < 30
    a = st.list_attempts(rid, "a")[0]
    assert a["status"] == "timed_out" and a["error_code"] == "timeout" and out.status == "failed"
    await asyncio.sleep(0.3)
    pid = int(pidfile.read_text())
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"


async def test_at06_cancellation_preserves_commits_and_diagnostics(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"a/partial.py": "partial = True\n"},
                                                          "print": ["diagnostic: started work"], "sleep": 60}]}})
    p = pol(scenario)
    home = tmp_path / "mg-home"
    svc = await build_services(p)
    store = open_store(home, svc.redactor)
    rid = await create_run(store, svc, objective="o", repo=repo, plan=plan_of(T("a", "a/")), home=home)
    orch = Orchestrator(store, svc, rid, home)

    async def canceller():
        for _ in range(100):
            if any(a["pid"] for a in store.list_attempts(rid)):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.7)
        orch.request_cancel()

    task = asyncio.create_task(canceller())
    out = await orch.execute()
    await task
    assert out.status == "cancelled"
    a = store.list_attempts(rid, "a")[0]
    assert a["status"] == "cancelled" and a["error_code"] == "cancelled"
    assert a["commit_sha"] and git(repo, "show", f"{a['branch']}:a/partial.py") == "partial = True"
    assert "diagnostic: started work" in open(f"{a['log_dir']}/stdout.log").read()
    assert store.get_task(rid, "a")["status"] == "cancelled"
    assert git(repo, "rev-list", "--count", f"main..{store.get_run(rid)['integration_branch']}") == "0"
    assert (tmp_path / "mg-home" / "runs" / rid / "report.json").exists()


async def test_at16_every_transition_auditable_and_replayable(tmp_path, repo, scenario):
    write_scenario(scenario, "a", writes("a/f.py", "1\n"))
    write_scenario(scenario, "b", {"attempts": {"default": [{"exit": 1}]}})
    write_scenario(scenario, "c", writes("c/f.py", "1\n"))
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/"), T("b", "b/"), T("c", "c/", ["b"])), tmp_path)
    res = replay_run(st, rid)
    assert res.ok, res.errors
    assert res.task_status == statuses(st, rid) == {"a": "succeeded", "b": "failed", "c": "blocked"}
    assert res.run_status == "failed" == st.get_run(rid)["status"]
    kinds = {e["kind"] for e in st.events(rid)}
    assert {"run.created", "task.registered", "task.transition", "attempt.started", "attempt.finished",
            "validation.result", "integration", "run.transition"} <= kinds


async def test_report_lists_unknown_costs_and_no_push(tmp_path, repo, scenario):
    write_scenario(scenario, "a", writes("a/f.py", "1\n"))
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/")), tmp_path)
    rep = json.loads(st.get_run(rid)["report"])
    assert rep["pushed"] is False and rep["final_integration_commit"] == out.final_commit
    assert rep["cost"]["unknown_for_attempts"] == 1 and "unknown" in rep["cost"]["note"]
    assert rep["attempts"][0]["cost_usd"] == "unknown"
    assert git(repo, "remote").strip() == ""


async def test_task_that_changes_nothing_fails_with_an_accurate_reason(tmp_path, repo, scenario):
    # e.g. a planner-invented "review the suite" task: the agent honestly makes no edit
    write_scenario(scenario, "review", {"attempts": {"default": [{"print": ["Everything looks fine, nothing to change."]}]}})
    write_scenario(scenario, "a", writes("a/f.py", "1\n"))
    rid, out, st = await run(pol(scenario), repo, plan_of(T("a", "a/"), T("review", "a/f.py", ["a"])), tmp_path)
    t = st.get_task(rid, "review")
    assert out.status == "failed" and st.get_task(rid, "a")["status"] == "succeeded"
    assert t["status"] == "failed" and "agent produced no changes" in t["failure_reason"]
    assert len(st.list_attempts(rid, "review")) == 2  # one repair attempt, bounded
    assert git(repo, "rev-list", "--count", f"main..{st.get_run(rid)['integration_branch']}") == "1"


async def test_failure_blocks_the_whole_dependency_chain_with_the_right_reason(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"exit": 1}]}})
    plan = plan_of(T("a", "a/", max_attempts=1), T("b", "b/", ["a"]), T("c", "c/", ["b"]), T("d", "d/", ["c"]))
    rid, out, st = await run(pol(scenario), repo, plan, tmp_path)
    reasons = {t["task_id"]: t["failure_reason"] for t in st.list_tasks(rid)}
    assert reasons["b"] == "blocked by a" and reasons["c"] == "blocked by b" and reasons["d"] == "blocked by c"
