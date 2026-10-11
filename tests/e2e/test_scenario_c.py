"""Scenario C: terminate the orchestrator during concurrent tasks, restart, reconcile - full stack."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from multigenz.core.replay import replay_run
from multigenz.core.runs import open_store
from tests.e2e.harness import AUTH_PLAN, FIXTURES, GOAL, cli, demo_repo, e2e_policy, files_for, pytest_on_commit, write_policy
from tests.support.helpers import git, sleeping_procs, write_scenario

pytestmark = [pytest.mark.e2e, pytest.mark.needs_userns]


def test_scenario_c_interrupted_run_reconciles_without_losing_or_duplicating_work(tmp_path, scenario):
    import shutil
    shutil.copytree(FIXTURES, scenario / "files", dirs_exist_ok=True)
    repo = demo_repo(tmp_path / "demo-api")
    write_scenario(scenario, "plan", {"modes": {"plan": {"default": [{"emit_json": AUTH_PLAN}]}}})
    write_scenario(scenario, "security", {"attempts": {"default": files_for("security")}})
    # the two tasks that run concurrently after `security`: their first execution hangs after doing real work
    hang = {"spawn_background": ["/bin/sleep", "40117"], "sleep": 600}
    api_first = [{**files_for("api")[0], **hang}]
    docs_first = [{**files_for("docs")[0], **hang}]
    write_scenario(scenario, "api", {"attempts": {"1": api_first, "default": files_for("api")}})
    write_scenario(scenario, "docs", {"attempts": {"1": docs_first, "default": files_for("docs")}})
    pf = write_policy(tmp_path / "policy.yaml", e2e_policy(scenario))

    p = subprocess.Popen([sys.executable, "-m", "multigenz", "run", GOAL, "--repo", str(repo), "--policy", str(pf)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    home = Path(os.environ["MULTIGENZ_HOME"])
    st = open_store(home)

    def concurrent_phase():
        rows = st.q("select * from attempts where task_id in ('api','docs') and pid is not null")
        return (len(rows) == 2 and st.q("select 1 from tasks where task_id='security' and status='succeeded'")
                and len(sleeping_procs("40117")) == 2)

    end = time.monotonic() + 90
    while time.monotonic() < end and not concurrent_phase():
        time.sleep(0.2)
    assert concurrent_phase(), "never reached the concurrent phase"
    rid = st.q("select run_id from runs")[0]["run_id"]
    security_before = st.list_attempts(rid, "security")
    p.send_signal(signal.SIGKILL)
    p.wait()
    assert len(sleeping_procs("40117")) == 2  # sandboxed children survived their orchestrator
    assert st.get_run(rid)["status"] == "running"
    listing = cli("runs", "list")
    assert "orphaned" in listing.stdout

    r = cli("runs", "resume", rid, "--json", timeout=600)
    assert r.returncode == 0, r.stdout[-2500:] + r.stderr[-1500:]
    rep = json.loads(r.stdout)
    assert rep["status"] == "succeeded"
    assert sleeping_procs("40117") == []  # leftovers were terminated by reconciliation first

    # no work lost: partial output of the interrupted attempts was preserved and reused
    for t, marker in (("api", "app/main.py"), ("docs", "docs/AUTH.md")):
        atts = st.list_attempts(rid, t)
        assert [a["status"] for a in atts] == ["interrupted", "finished"]
        assert git(repo, "show", f"{atts[0]['branch']}:{marker}") != ""
        assert atts[1]["base_commit"] == atts[0]["commit_sha"]
        assert st.get_task(rid, t)["charged_attempts"] == 0
    # no duplicated execution: the finished task was untouched; exactly one resumed attempt per interrupted task
    assert st.list_attempts(rid, "security") == security_before
    assert len(st.list_attempts(rid)) == 5
    keys = [a["idempotency_key"] for a in st.list_attempts(rid)]
    assert len(set(keys)) == 5
    # the end result is correct and independently verified
    ok, tail = pytest_on_commit(repo, rep["final_integration_commit"])
    assert ok and "10 passed" in tail, tail
    assert replay_run(st, rid).ok
    assert git(repo, "rev-parse", "main") == st.get_run(rid)["base_commit"] and git(repo, "status", "--porcelain") == ""
    integ_msgs = git(repo, "log", f"main..{rep['integration_branch']}", "--format=%(trailers:key=Multigenz-Task,valueonly)").split()
    assert sorted(integ_msgs) == ["api", "docs", "security"]  # each task integrated exactly once
