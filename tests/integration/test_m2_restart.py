"""AT-07: restart reconciles unfinished runs without duplicating unsafe execution."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
import yaml

from multigenz.core.replay import replay_run
from multigenz.core.runs import open_store
from tests.support.helpers import SUPPORT, T, git, plan_of, scripted_policy, write_scenario


def alive(pid: int) -> bool:
    try:
        return open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except OSError:
        return False


def cli(*args, env=None, **kw):
    return subprocess.Popen([sys.executable, "-m", "multigenz", *args], env={**os.environ, **(env or {})},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def wait_for(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_at07_sigkill_midrun_then_resume_reconciles(tmp_path, repo, scenario):
    marks = tmp_path / "marks"
    marks.mkdir()
    pids = tmp_path / "pids"
    pids.mkdir()
    # t1: quick. t2/t3: first attempt hangs after doing partial work (and leaves a background child),
    # the resumed attempt finishes quickly.
    write_scenario(scenario, "t1", {"attempts": {"default": [{"write": {"one/f.py": "1\n"}, "marker_file": str(marks / "t1")}]}})
    for t in ("t2", "t3"):
        write_scenario(scenario, t, {"attempts": {
            "1": [{"write": {f"{t}/partial.py": "partial = 1\n"}, "marker_file": str(marks / t),
                   "spawn_background": ["/bin/sleep", "300"], "background_pid_file": str(pids / t), "sleep": 300}],
            "default": [{"write": {f"{t}/done.py": "done = 1\n"}, "marker_file": str(marks / t)}]}})
    pol = scripted_policy(scenario, isolation="none")
    pol_file = tmp_path / "policy.yaml"
    pol_file.write_text(yaml.safe_dump(json.loads(pol.model_dump_json())))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("t1", "one/"), T("t2", "t2/", ["t1"]), T("t3", "t3/", ["t1"])).model_dump_json())

    p = cli("run", "restart test", "--repo", str(repo), "--policy", str(pol_file), "--plan-file", str(plan_file))
    home = os.environ["MULTIGENZ_HOME"]
    from pathlib import Path
    store = open_store(Path(home))

    def midrun():
        rows = store.q("select run_id from runs")
        if not rows:
            return False
        rid = rows[0]["run_id"]
        atts = [a for a in store.list_attempts(rid) if a["task_id"] in ("t2", "t3") and a["pid"]]
        return len(atts) == 2 and store.get_task(rid, "t1")["status"] == "succeeded" and (pids / "t2").exists() and (pids / "t3").exists()

    assert wait_for(midrun), "run never reached the concurrent phase"
    rid = store.q("select run_id from runs")[0]["run_id"]
    bg = {t: int((pids / t).read_text()) for t in ("t2", "t3")}
    agent_pids = {a["task_id"]: a["pid"] for a in store.list_attempts(rid) if a["task_id"] in ("t2", "t3")}

    p.send_signal(signal.SIGKILL)  # orchestrator dies without any cleanup
    p.wait()
    # the orchestrator's children are orphaned and still running
    assert all(alive(pid) for pid in bg.values()) and all(alive(pid) for pid in agent_pids.values())
    assert store.get_run(rid)["status"] == "running"
    listing = subprocess.run([sys.executable, "-m", "multigenz", "runs", "list"], capture_output=True, text=True).stdout
    assert "orphaned" in listing
    t1_before = store.list_attempts(rid, "t1")

    r = subprocess.run([sys.executable, "-m", "multigenz", "runs", "resume", rid, "--json"], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert store.get_run(rid)["status"] == "succeeded"

    # orphans were found and terminated before anything was re-executed
    assert not any(alive(pid) for pid in bg.values()) and not any(alive(pid) for pid in agent_pids.values())
    # completed work is never re-executed
    assert store.list_attempts(rid, "t1") == t1_before
    assert len((marks / "t1").read_text().split()) == 1
    # interrupted tasks: exactly one resumed execution each, not charged against retries
    for t in ("t2", "t3"):
        atts = store.list_attempts(rid, t)
        assert [a["status"] for a in atts] == ["interrupted", "finished"]
        assert atts[0]["error_code"] == "interrupted" and len((marks / t).read_text().split()) == 2
        task = store.get_task(rid, t)
        assert task["charged_attempts"] == 0 and task["interruptions"] == 1
        # partial work preserved in git and carried into the resumed attempt
        assert git(repo, "show", f"{atts[0]['branch']}:{t}/partial.py") == "partial = 1"
        assert atts[1]["base_commit"] == atts[0]["commit_sha"]
        integ = store.get_run(rid)["integration_branch"]
        assert git(repo, "show", f"{integ}:{t}/partial.py") == "partial = 1"
        assert git(repo, "show", f"{integ}:{t}/done.py") == "done = 1"
    keys = [a["idempotency_key"] for a in store.list_attempts(rid)]
    assert len(keys) == len(set(keys))
    recon = [e for e in store.events(rid, kinds=("reconcile",)) if e["payload"].get("action") == "summary"]
    assert recon and recon[-1]["payload"]["attempts_interrupted"] == 2 and recon[-1]["payload"]["processes_killed"] == 2
    res = replay_run(store, rid)
    assert res.ok, res.errors  # the audit log is consistent across the crash
    assert git(repo, "rev-parse", "main") == store.get_run(rid)["base_commit"]


def test_resume_refused_while_another_process_owns_the_run(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"a/f.py": "1\n"}, "sleep": 20}]}})
    pol = scripted_policy(scenario, isolation="none")
    pol_file = tmp_path / "policy.yaml"
    pol_file.write_text(yaml.safe_dump(json.loads(pol.model_dump_json())))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("a", "a/")).model_dump_json())
    p = cli("run", "x", "--repo", str(repo), "--policy", str(pol_file), "--plan-file", str(plan_file))
    from pathlib import Path
    store = open_store(Path(os.environ["MULTIGENZ_HOME"]))
    assert wait_for(lambda: store.q("select 1 from attempts where pid is not null"))
    rid = store.q("select run_id from runs")[0]["run_id"]
    r = subprocess.run([sys.executable, "-m", "multigenz", "runs", "resume", rid], capture_output=True, text=True)
    assert r.returncode != 0 and "owned by another process" in (r.stdout + r.stderr)
    # graceful cancel from another terminal: owner stops the agent, run ends cancelled with work preserved
    c = subprocess.run([sys.executable, "-m", "multigenz", "runs", "cancel", rid], capture_output=True, text=True)
    assert "cancellation requested" in c.stdout
    p.wait(timeout=60)
    assert store.get_run(rid)["status"] == "cancelled"
    assert store.list_attempts(rid)[0]["status"] == "cancelled"
    assert replay_run(store, rid).ok


def test_sigint_interrupts_resumably(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"1": [{"write": {"a/f.py": "1\n"}, "sleep": 60}],
                                                "default": [{"write": {"a/g.py": "2\n"}}]}})
    pol = scripted_policy(scenario, isolation="none")
    pol_file = tmp_path / "policy.yaml"
    pol_file.write_text(yaml.safe_dump(json.loads(pol.model_dump_json())))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("a", "a/")).model_dump_json())
    p = cli("run", "x", "--repo", str(repo), "--policy", str(pol_file), "--plan-file", str(plan_file))
    from pathlib import Path
    store = open_store(Path(os.environ["MULTIGENZ_HOME"]))
    assert wait_for(lambda: store.q("select 1 from attempts where pid is not null"))
    time.sleep(0.5)
    p.send_signal(signal.SIGINT)
    p.communicate(timeout=60)
    rid = store.q("select run_id from runs")[0]["run_id"]
    assert p.returncode == 130 and store.get_run(rid)["status"] == "running"
    assert store.get_task(rid, "a")["status"] == "ready" and store.list_attempts(rid)[0]["status"] == "interrupted"
    r = subprocess.run([sys.executable, "-m", "multigenz", "runs", "resume", rid], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert store.get_run(rid)["status"] == "succeeded" and replay_run(store, rid).ok


def test_crash_between_git_merge_and_db_record_is_settled_without_remerge(tmp_path, repo, scenario):
    from pathlib import Path

    from multigenz.core.driver import resume_run

    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"a/f.py": "1\n"}, "marker_file": str(tmp_path / "m")}]}})
    pol = scripted_policy(scenario, isolation="none")
    pol_file = tmp_path / "policy.yaml"
    pol_file.write_text(yaml.safe_dump(json.loads(pol.model_dump_json())))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("a", "a/")).model_dump_json())
    r = subprocess.run([sys.executable, str(SUPPORT / "crash_runner.py"), str(pol_file), str(repo), str(plan_file)], capture_output=True, text=True)
    assert r.returncode == 9, r.stderr
    home = Path(os.environ["MULTIGENZ_HOME"])
    st = open_store(home)
    rid = st.q("select run_id from runs")[0]["run_id"]
    integ = st.get_run(rid)["integration_branch"]
    merged_head = git(repo, "rev-parse", integ)
    assert merged_head != st.get_run(rid)["base_commit"]  # git moved...
    assert st.get_task(rid, "a")["status"] == "validating"  # ...but the DB never learned about it
    assert [i["status"] for i in st.list_integrations(rid)] == ["merging"]

    import asyncio
    out = asyncio.run(resume_run(home, rid))
    assert out.status == "succeeded"
    assert git(repo, "rev-parse", integ) == merged_head  # no second merge commit
    assert git(repo, "rev-list", "--count", f"main..{integ}") == git(repo, "rev-list", "--count", f"main..{merged_head}")
    assert len(st.list_attempts(rid, "a")) == 1 and len((tmp_path / "m").read_text().split()) == 1  # agent not re-run
    assert [i["status"] for i in st.list_integrations(rid)] == ["merged", "already_merged"]
    assert replay_run(st, rid).ok
