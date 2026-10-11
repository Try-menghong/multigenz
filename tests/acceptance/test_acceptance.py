"""Acceptance tests AT-01 .. AT-16 (docs/IMPLEMENTATION_PLAN.md).

Each test drives the real stack: CLI / orchestrator, adapters, the user-namespace sandbox, Git worktrees,
SQLite state and the validation pipeline. Agents are scripted stand-ins (tests/support/scripted_agent);
no LLM is involved, which is stated plainly in the README.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from multigenz.contracts.task import TaskStatus
from multigenz.core.bootstrap import build_services
from multigenz.core.dag import validate_plan
from multigenz.core.driver import start_run
from multigenz.core.orchestrator import Orchestrator
from multigenz.core.replay import replay_run
from multigenz.core.runs import create_run, open_store
from multigenz.core.state_machine import StateMachine
from multigenz.errors import InvalidTransition, WorktreeError
from multigenz.execution.git import Git
from multigenz.execution.worktree import WorktreeManager
from tests.support.helpers import (SUPPORT, T, git, max_concurrency, overlap_seconds, plan_of, scripted_policy,
                                   sleeping_procs, write_scenario, writes)

pytestmark = [pytest.mark.acceptance, pytest.mark.needs_userns]

HOME_ENV = "MULTIGENZ_HOME"


def home() -> Path:
    return Path(os.environ[HOME_ENV])


def pol(scenario, **kw):
    kw.setdefault("isolation", "userns")
    return scripted_policy(scenario, **kw)


async def run_plan(policy, repo, plan, **kw):
    rid, out = await start_run(policy, objective="acceptance", repo=repo, plan=plan, home=home(), **kw)
    return rid, out, open_store(home())


def policy_file(tmp_path, policy) -> Path:
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump(json.loads(policy.model_dump_json())))
    return f


def cli(*args, timeout=300):
    return subprocess.run([sys.executable, "-m", "multigenz", *args], capture_output=True, text=True, timeout=timeout)


def popen_cli(*args):
    return subprocess.Popen([sys.executable, "-m", "multigenz", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def wait_for(pred, timeout=40.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def mount_ancestors(*paths) -> dict[str, set[str]]:
    """Directories the sandbox must create only as mount points: {parent dir: {child names on the way to a mount}}."""
    out: dict[str, set[str]] = {}
    for p in paths:
        parts = Path(p).resolve().parts
        for i in range(len(parts) - 1):  # parent = parts[:i+1], child = parts[i+1]
            out.setdefault(str(Path(*parts[: i + 1])), set()).add(parts[i + 1])
    return out


def assert_only_mount_ancestors(log: str, listed: str, *mounts) -> None:
    """`LS <listed>:` shows nothing but ancestors of explicitly mounted paths (no dotfiles, no credentials dirs)."""
    anc = mount_ancestors(*mounts).get(listed, set())
    line = next((l for l in log.splitlines() if l.startswith(f"LS {listed}: ") or l.startswith(f"LS-FAIL {listed}")), "")
    if line.startswith("LS-FAIL"):
        return
    entries = eval(line.split(": ", 1)[1])  # noqa: S307 - our own test output
    assert set(entries) <= anc and not any(e.startswith(".") for e in entries), (listed, entries, anc)


GOOD_TEST = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
BAD_TEST = "import unittest\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.assertEqual(1, 2)\n"


# ----------------------------------------------------------------------------------------------------------
def test_at_01_planner_rejects_cyclic_dependencies(tmp_path, repo, scenario):
    cyc = plan_of(T("a", "a/", ["c"]), T("b", "b/", ["a"]), T("c", "c/", ["b"]))
    assert any("cycle" in i for i in validate_plan(cyc))
    pf = policy_file(tmp_path, pol(scenario))
    plan_file = tmp_path / "cyc.json"
    plan_file.write_text(cyc.model_dump_json())
    r = cli("run", "goal", "--repo", str(repo), "--policy", str(pf), "--plan-file", str(plan_file))
    assert r.returncode != 0 and "cycle" in r.stderr + r.stdout
    assert open_store(home()).list_runs() == []  # rejected before anything was created
    assert git(repo, "for-each-ref", "refs/heads/multigenz/") == ""
    assert not (home() / "runs").exists() or list((home() / "runs").iterdir()) == []


async def test_at_02_independent_tasks_execute_concurrently(tmp_path, repo, scenario):
    tasks = []
    for t in "abc":
        write_scenario(scenario, t, {"attempts": {"default": [{"write": {f"{t}/f.py": "x=1\n"}, "sleep": 1.5}]}})
        tasks.append(T(t, f"{t}/"))
    t0 = time.monotonic()
    rid, out, st = await run_plan(pol(scenario), repo, plan_of(*tasks))
    wall = time.monotonic() - t0
    atts = st.list_attempts(rid)
    assert out.status == "succeeded" and max_concurrency(atts) == 3
    assert min(overlap_seconds(atts[0], atts[1]), overlap_seconds(atts[1], atts[2])) > 0.8
    assert wall < 3 * 1.5 + 6  # three 1.5s tasks did not run back-to-back


async def test_at_03_dependents_wait_for_validated_prerequisites(tmp_path, repo, scenario):
    # a's first result violates path ownership (validation fails), the repair succeeds. b must not start before then.
    write_scenario(scenario, "a", {"attempts": {
        "1": [{"write": {"a/f.py": "v=1\n", "STRAY.txt": "x\n"}}],
        "2": [{"write": {"a/f.py": "v=2\n"}, "delete": ["STRAY.txt"]}]}})
    write_scenario(scenario, "b", {"attempts": {"default": [{"read_files": ["a/f.py"], "write": {"b/g.py": "w=1\n"}}]}})
    rid, out, st = await run_plan(pol(scenario), repo, plan_of(T("a", "a/"), T("b", "b/", ["a"])))
    assert out.status == "succeeded"
    ev = st.events(rid, kinds=("task.transition",))
    a_ok = next(e["seq"] for e in ev if e["task_id"] == "a" and e["payload"]["to"] == "succeeded")
    b_ready = next(e["seq"] for e in ev if e["task_id"] == "b" and e["payload"]["to"] == "ready")
    first_b_start = next(e["seq"] for e in st.events(rid, kinds=("attempt.started",)) if e["task_id"] == "b")
    assert a_ok < b_ready < first_b_start
    assert [v["payload"]["passed"] for v in st.events(rid, kinds=("validation.result",)) if v["task_id"] == "a"] == [False, True]
    b = st.list_attempts(rid, "b")[0]
    assert "READ-OK a/f.py: 'v=2" in open(f"{b['log_dir']}/stdout.log").read()  # saw the validated result, not attempt 1


async def test_at_04_provider_failure_triggers_compatible_reassignment(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [
        {"only_agent": "alpha", "stderr": ["401 Unauthorized: not logged in"], "exit": 1},
        {"skip_agent": "alpha", "write": {"a/f.py": "ok=1\n"}}]}})
    write_scenario(scenario, "b", writes("b/f.py", "ok=1\n"))
    p = pol(scenario, agents={"alpha": ["coding"], "beta": ["coding"], "gamma": ["review"]})
    rid, out, st = await run_plan(p, repo, plan_of(T("a", "a/", caps=["coding"]), T("b", "b/", ["a"], caps=["coding"])))
    assert out.status == "succeeded"
    atts = st.list_attempts(rid, "a")
    assert [(a["provider"], a["error_code"]) for a in atts] == [("alpha", "auth_failure"), ("beta", None)]
    assert st.get_task(rid, "a")["charged_attempts"] == 0
    assert st.provider_states(rid)["alpha"]["status"] == "disabled"
    assert st.list_attempts(rid, "b")[0]["provider"] == "beta"  # alpha stays stopped: no repeated login retries
    assert "gamma" not in {a["provider"] for a in st.list_attempts(rid)}  # incompatible capability never chosen
    # missing binary variant
    write_scenario(scenario, "c", writes("c/f.py", "ok=1\n"))
    p2 = pol(scenario, agents={"alpha": ["coding"], "beta": ["coding"]})
    svc = await build_services(p2)
    svc.adapters["alpha"]._binary = str(tmp_path / "vanished")
    rid2, out2, st2 = await run_plan(p2, repo, plan_of(T("c", "c/")), services=svc)
    assert out2.status == "succeeded"
    assert [(a["provider"], a["error_code"]) for a in st2.list_attempts(rid2, "c")] == [("alpha", "binary_missing"), ("beta", None)]


async def test_at_05_timeout_terminates_subprocess_process_group(tmp_path, repo, scenario):
    marker = "31337"  # unique sleep duration to find strays on the host
    write_scenario(scenario, "a", {"attempts": {"default": [
        {"spawn_background": ["/bin/sleep", marker]}, {"spawn_background": ["/bin/sleep", marker]}, {"sleep": 120}]}})
    p = pol(scenario, orchestrator={"kill_grace_seconds": 1.0})
    t0 = time.monotonic()
    rid, out, st = await run_plan(p, repo, plan_of(T("a", "a/", timeout_seconds=3, max_attempts=1)))
    assert time.monotonic() - t0 < 20
    a = st.list_attempts(rid, "a")[0]
    assert a["status"] == "timed_out" and a["error_code"] == "timeout" and out.status == "failed"
    await asyncio.sleep(0.5)
    assert sleeping_procs(marker) == []  # every descendant is gone, sandbox pid namespace included
    assert not os.path.exists(f"/proc/{a['pgid']}") or open(f"/proc/{a['pgid']}/stat").read().split()[2] == "Z"
    assert "BACKGROUND-PID" in open(f"{a['log_dir']}/stdout.log").read()  # diagnostics preserved


def test_at_06_cancellation_preserves_committed_and_diagnostic_artifacts(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"a/partial.py": "partial = 1\n"},
                                                          "print": ["diagnostic: halfway"], "sleep": 120}]}})
    write_scenario(scenario, "b", writes("b/done.py", "done = 1\n"))
    pf = policy_file(tmp_path, pol(scenario))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("a", "a/"), T("b", "b/")).model_dump_json())
    p = popen_cli("run", "goal", "--repo", str(repo), "--policy", str(pf), "--plan-file", str(plan_file))
    st = open_store(home())
    assert wait_for(lambda: any(a["task_id"] == "a" and a["pid"] for a in st.q("select * from attempts"))
                    and st.q("select 1 from tasks where task_id='b' and status='succeeded'"))
    time.sleep(0.5)
    rid = st.q("select run_id from runs")[0]["run_id"]
    c = cli("runs", "cancel", rid)
    assert "cancellation requested" in c.stdout
    p.communicate(timeout=60)
    assert st.get_run(rid)["status"] == "cancelled"
    a = st.list_attempts(rid, "a")[0]
    assert a["status"] == "cancelled" and a["commit_sha"]
    assert git(repo, "show", f"{a['branch']}:a/partial.py") == "partial = 1"  # committed work preserved
    assert "diagnostic: halfway" in Path(a["log_dir"], "stdout.log").read_text()  # diagnostics preserved
    assert (Path(a["worktree"]) / "a" / "partial.py").exists()  # worktree retained, never force-removed
    assert st.get_task(rid, "b")["status"] == "succeeded"  # already-integrated work stays
    integ = st.get_run(rid)["integration_branch"]
    assert git(repo, "show", f"{integ}:b/done.py") == "done = 1"
    assert (home() / "runs" / rid / "report.json").exists() and replay_run(st, rid).ok


def test_at_07_restart_reconciles_without_duplicate_unsafe_execution(tmp_path, repo, scenario):
    for t in ("t2", "t3"):
        write_scenario(scenario, t, {"attempts": {
            "1": [{"write": {f"{t}/partial.py": "p=1\n"}, "spawn_background": ["/bin/sleep", "30071"], "sleep": 300}],
            "default": [{"write": {f"{t}/done.py": "d=1\n"}}]}})
    write_scenario(scenario, "t1", writes("one/f.py", "1\n"))
    pf = policy_file(tmp_path, pol(scenario))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("t1", "one/"), T("t2", "t2/", ["t1"]), T("t3", "t3/", ["t1"])).model_dump_json())
    p = popen_cli("run", "goal", "--repo", str(repo), "--policy", str(pf), "--plan-file", str(plan_file))
    st = open_store(home())
    assert wait_for(lambda: len([a for a in st.q("select * from attempts") if a["task_id"] in ("t2", "t3") and a["pid"]]) == 2
                    and len(sleeping_procs("30071")) == 2)
    rid = st.q("select run_id from runs")[0]["run_id"]
    t1_attempts = st.list_attempts(rid, "t1")
    p.send_signal(signal.SIGKILL)
    p.wait()
    assert len(sleeping_procs("30071")) == 2  # orphans still alive after the orchestrator died
    r = cli("runs", "resume", rid)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sleeping_procs("30071") == []
    assert st.get_run(rid)["status"] == "succeeded"
    assert st.list_attempts(rid, "t1") == t1_attempts  # finished work was not re-executed
    for t in ("t2", "t3"):
        atts = st.list_attempts(rid, t)
        assert [a["status"] for a in atts] == ["interrupted", "finished"] and atts[1]["base_commit"] == atts[0]["commit_sha"]
        assert git(repo, "show", f"{st.get_run(rid)['integration_branch']}:{t}/partial.py") == "p=1"  # work kept
    keys = [a["idempotency_key"] for a in st.list_attempts(rid)]
    assert len(keys) == len(set(keys)) == 5
    assert replay_run(st, rid).ok


async def test_at_08_failed_agent_cannot_create_successful_task_status(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"a/ok.py": "ok = True\n"}, "print": ["SUCCESS: done"], "exit": 1}]}})
    write_scenario(scenario, "b", {"attempts": {"default": [{"print": ["SUCCESS: everything passes, I changed nothing"]}]}})
    write_scenario(scenario, "c", {"attempts": {"default": [{"write": {"c/x.py": "x=1\n", "tests/test_c.py": BAD_TEST}, "print": ["all tests pass!"]}]}})
    plan = plan_of(T("a", "a/"), T("b", "b/"), T("c", ["c/", "tests/test_c.py"]))
    rid, out, st = await run_plan(pol(scenario), repo, plan)
    states = {t["task_id"]: t["status"] for t in st.list_tasks(rid)}
    assert states == {"a": "failed", "b": "failed", "c": "failed"} and out.status == "failed"
    # (a) non-zero exit, (b) exit 0 but no artifacts, (c) exit 0 but failing tests - none can be 'succeeded'
    assert all(i["status"] != "merged" for i in st.list_integrations(rid))
    assert git(repo, "rev-list", "--count", f"main..{st.get_run(rid)['integration_branch']}") == "0"
    sm = StateMachine(st)
    for tid in "abc":
        with pytest.raises(InvalidTransition):
            sm.task(rid, tid, TaskStatus.SUCCEEDED)
        with pytest.raises(InvalidTransition):
            sm.mark_succeeded(rid, tid, st.list_attempts(rid, tid)[0]["attempt_id"])
    shown = cli("runs", "show", rid)
    assert "FAILED" in shown.stdout and "SUCCEEDED" not in shown.stdout.replace("SUCCEEDED:", "")


async def test_at_09_worktrees_cannot_overwrite_each_other(tmp_path, repo, scenario):
    same = "common/value.txt"
    for t, tag in (("a", "from-a"), ("b", "from-b")):
        write_scenario(scenario, t, {"attempts": {"default": [{"write": {same: f"{tag}\n", f"{t}/f": "1"}, "listdir": ["/", "/workspace"],
                                                              "sleep": 1.0}]},
                                     "modes": {"conflict": [{"write": {same: "from-a\nfrom-b\n"}}]}})
    plan = plan_of(T("a", "a/", shared_paths=[same]), T("b", "b/", shared_paths=[same]))
    rid, out, st = await run_plan(pol(scenario), repo, plan)
    assert out.status == "succeeded"
    ia, ib = st.list_attempts(rid, "a")[0], st.list_attempts(rid, "b")[0]
    assert ia["worktree"] != ib["worktree"] and ia["branch"] != ib["branch"]
    assert Path(ia["worktree"], same).read_text() == "from-a\n" and Path(ib["worktree"], same).read_text() == "from-b\n"
    for x, other in ((ia, ib), (ib, ia)):  # the sandbox exposes only the agent's own worktree
        log = Path(x["log_dir"], "stdout.log").read_text()
        assert str(Path(other["worktree"]).parent) not in log and "LS /: [" in log
        top = eval(log.split("LS /: ")[1].splitlines()[0])  # noqa: S307 - our own test output
        std = {"bin", "dev", "etc", "home", "lib", "lib64", "lib32", "proc", "sbin", "tmp", "usr", "workspace", "mg"}
        extra = set(top) - std
        assert "workspace" in top and "var" not in top
        assert extra <= set(mount_ancestors(SUPPORT, scenario).get("/", set())), extra  # only mount-point ancestors
    g = Git(repo, run_id=rid)
    wm = WorktreeManager(g, Path(ia["worktree"]).parent)
    with pytest.raises(WorktreeError):
        wm.create(Path(ia["worktree"]).name, "multigenz/run-%s/task-zz" % rid, g.head())  # same path
    with pytest.raises(WorktreeError):
        wm.create("other-name", ia["branch"], g.head())  # same branch
    assert Path(ia["worktree"], same).read_text() == "from-a\n"


async def test_at_10_merge_conflict_preserves_original_commits(tmp_path, repo, scenario):
    (repo / "config.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "config")
    res = {"modes": {"conflict": [{"write": {"config.txt": "base\nfrom-a\nfrom-b\n"}}]}}
    write_scenario(scenario, "a", {**res, "attempts": {"default": [{"write": {"config.txt": "base\nfrom-a\n", "a/f": "1"}}]}})
    write_scenario(scenario, "b", {**res, "attempts": {"default": [{"sleep": 1.5, "write": {"config.txt": "base\nfrom-b\n", "b/f": "1"}}]}})
    rid, out, st = await run_plan(pol(scenario), repo, plan_of(T("a", "a/", shared_paths=["config.txt"]), T("b", "b/", shared_paths=["config.txt"])))
    assert out.status == "succeeded"
    ia, ib = st.list_attempts(rid, "a"), st.list_attempts(rid, "b")
    originals = {x["branch"]: x["commit_sha"] for x in (ia[0], ib[0])}
    for br, sha in originals.items():
        assert git(repo, "rev-parse", br) == sha  # both original commits untouched on their branches
        assert git(repo, "show", f"{sha}:config.txt") in ("base\nfrom-a", "base\nfrom-b")
    assert [x["kind"] for x in ib] == ["code", "conflict"]
    assert git(repo, "show", f"{st.get_run(rid)['integration_branch']}:config.txt") == "base\nfrom-a\nfrom-b"  # neither side lost
    # a failed resolution must not lose either side either
    write_scenario(scenario, "b", {"modes": {"conflict": [{"print": ["gave up"]}]}, "attempts": {"default": [{"sleep": 1.5, "write": {"config.txt": "base\nfrom-b\n", "b/f": "1"}}]}})
    rid2, out2, st2 = await run_plan(pol(scenario), repo, plan_of(T("a", "a/", shared_paths=["config.txt"]), T("b", "b/", shared_paths=["config.txt"])))
    assert out2.status == "failed" and st2.get_task(rid2, "b")["failure_reason"] == "unresolved merge conflict"
    b0 = st2.list_attempts(rid2, "b")[0]
    assert git(repo, "rev-parse", b0["branch"]) == b0["commit_sha"] and git(repo, "show", f"{b0['commit_sha']}:config.txt") == "base\nfrom-b"
    assert git(repo, "show", f"{st2.get_run(rid2)['integration_branch']}:config.txt") == "base\nfrom-a"


async def test_at_11_failing_tests_prevent_successful_integration(tmp_path, repo, scenario):
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"lib/a.py": "A=1\n", "tests/test_a.py": BAD_TEST}}]}})
    rid, out, st = await run_plan(pol(scenario), repo, plan_of(T("a", ["lib/", "tests/test_a.py"])))
    assert out.status == "failed" and st.get_task(rid, "a")["status"] == "failed"
    assert git(repo, "rev-list", "--count", f"main..{st.get_run(rid)['integration_branch']}") == "0"
    assert st.list_integrations(rid) == []
    tr = st.events(rid, kinds=("validation.result",))[0]["payload"]["test_reports"][0]
    assert not tr["passed"] and "AssertionError" in tr["commands"][0]["output_tail"]
    # passes alone, fails when merged with the other task -> rejected at integration, still never merged
    only_without_a = ("import os, unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n"
                      "        self.assertFalse(os.path.exists('lib/a.py'))\n")
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"lib/a.py": "A=1\n", "tests/test_a.py": GOOD_TEST}}]}})
    write_scenario(scenario, "b", {"attempts": {"default": [{"sleep": 1.2, "write": {"libb/b.py": "B=1\n", "tests/test_b.py": only_without_a}}]}})
    rid2, out2, st2 = await run_plan(pol(scenario, orchestrator={"max_task_attempts": 1}), repo,
                                     plan_of(T("a", ["lib/", "tests/test_a.py"]), T("b", ["libb/", "tests/test_b.py"], max_attempts=1)))
    assert {t["task_id"]: t["status"] for t in st2.list_tasks(rid2)} == {"a": "succeeded", "b": "failed"}
    assert [i["status"] for i in st2.list_integrations(rid2) if i["task_id"] == "b"] == ["rejected"]
    tree = git(repo, "ls-tree", "-r", "--name-only", st2.get_run(rid2)["integration_branch"]).split()
    assert "lib/a.py" in tree and not any(p.startswith("libb/") or p == "tests/test_b.py" for p in tree)


def snapshot_original(repo: Path) -> dict:
    files = {}
    for p in sorted(repo.rglob("*")):
        if ".git" in p.relative_to(repo).parts or not p.is_file():
            continue
        s = p.stat()
        files[str(p.relative_to(repo))] = (p.read_bytes(), s.st_mtime_ns)
    return {
        "head": git(repo, "rev-parse", "HEAD"), "branch": git(repo, "branch", "--show-current"),
        "status": git(repo, "status", "--porcelain=v2", "--untracked-files=all", "--ignored"),
        "index_tree": git(repo, "write-tree"), "index_bytes": (repo / ".git" / "index").read_bytes(),
        "stash": git(repo, "stash", "list"), "reflog": git(repo, "reflog", "show", "HEAD"),
        "branches": sorted(b for b in git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/").splitlines()
                           if "multigenz/" not in b),
        "files": files,
    }


def make_messy_repo(path: Path) -> Path:
    from tests.support.helpers import make_repo
    r = make_repo(path, {"README.md": "# r\n", "src/app.py": "VALUE = 1\n", ".gitignore": "*.log\n",
                         "tests/test_app.py": GOOD_TEST})
    git(r, "branch", "feature/user-work")
    (r / "stashed.txt").write_text("s"); git(r, "add", "stashed.txt"); git(r, "stash", "push", "-q", "-m", "wip")
    (r / "src" / "app.py").write_text("VALUE = 'uncommitted edit'\n")          # unstaged modification
    (r / "README.md").write_text("# staged edit\n")
    git(r, "add", "README.md")                                                     # staged modification
    (r / "notes.txt").write_text("untracked")                                      # untracked
    (r / "debug.log").write_text("ignored")                                        # ignored
    assert "uncommitted edit" in (r / "src" / "app.py").read_text() and git(r, "stash", "list") != ""
    return r


async def test_at_12_original_branch_and_dirty_files_remain_unchanged(tmp_path, scenario):
    repo = make_messy_repo(tmp_path / "messy")
    before = snapshot_original(repo)
    # success, failure and a hostile agent in the same repo
    write_scenario(scenario, "good", writes("lib/g.py", "G=1\n"))
    write_scenario(scenario, "bad", {"attempts": {"default": [{"write": {"lib2/b.py": "B=1\n"}, "exit": 1}]}})
    write_scenario(scenario, "evil", {"attempts": {"default": [{"write_files": ["/workspace/../../../../etc/pwn", str(repo / "src" / "app.py"),
                                                                              str(repo / "notes.txt"), str(repo / ".git" / "HEAD")],
                                                              "write": {"lib3/e.py": "E=1\n"}}]}})
    plan = plan_of(T("good", "lib/"), T("bad", "lib2/", max_attempts=1), T("evil", "lib3/"))
    rid, out, st = await run_plan(pol(scenario), repo, plan)
    assert {t["task_id"]: t["status"] for t in st.list_tasks(rid)} == {"good": "succeeded", "bad": "failed", "evil": "succeeded"}
    log = Path(st.list_attempts(rid, "evil")[0]["log_dir"], "stdout.log").read_text()
    assert "WRITE-OK" not in log or "WRITE-OK /workspace/../../../../etc/pwn" not in log
    assert all(f"WRITE-FAIL {p}" in log for p in (str(repo / "src" / "app.py"), str(repo / ".git" / "HEAD")))
    after = snapshot_original(repo)
    assert after == before
    assert (repo / "src" / "app.py").read_text() == "VALUE = 'uncommitted edit'\n"
    # and again after a cancelled run
    rid2, _ = await start_run(pol(scenario), objective="x", repo=repo, plan=plan_of(T("good", "lib/")), home=home(), execute=False)
    svc = await build_services(pol(scenario))
    orch = Orchestrator(open_store(home(), svc.redactor), svc, rid2, home())
    orch.request_cancel()
    assert (await orch.execute()).status == "cancelled"
    assert snapshot_original(repo) == before


async def test_at_13_agent_cannot_read_host_credentials(tmp_path, repo, scenario, monkeypatch):
    fake_home = tmp_path / "fakehome"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".aws").mkdir()
    (fake_home / ".ssh" / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nHOSTKEY\n-----END OPENSSH PRIVATE KEY-----\n")
    (fake_home / ".aws" / "credentials").write_text("aws_secret_access_key = HOSTAWSSECRET\n")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-HOSTSECRET0123456789abcdef")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "H" * 36)
    monkeypatch.setenv("INNOCUOUS_NAME", "innocuous-value-xyz")
    monkeypatch.setenv("GRANTED_KEY", "granted-only-to-alpha-5566")
    targets = [str(fake_home / ".ssh" / "id_rsa"), str(fake_home / ".aws" / "credentials"), "/root/.ssh/id_rsa",
               "/etc/shadow", f"/proc/{os.getpid()}/environ", f"/proc/{os.getppid()}/environ",
               str(home() / "multigenz.db"), str(tmp_path / "policy.yaml"), str(repo / ".git" / "config")]
    spec = {"attempts": {"default": [{"dump_env": True, "read_files": targets, "listdir": [str(fake_home), "/home", "/root", "/run", "/var"],
                                      "write": {"a/f.py": "x=1\n"}}]}}
    write_scenario(scenario, "a", spec)
    write_scenario(scenario, "b", spec | {"attempts": {"default": [{"dump_env": True, "write": {"b/f.py": "x=1\n"}}]}})
    pol_ = pol(scenario, agents={"alpha": ["coding"], "beta": ["coding"]},
               agent_extra={"alpha": {"credentials": {"env": ["GRANTED_KEY"]}}})
    pf = policy_file(tmp_path, pol_)
    rid, out, st = await run_plan(pol_, repo, plan_of(T("a", "a/", caps=["coding"]), T("b", "b/", caps=["coding"])))
    log_a = Path(st.list_attempts(rid, "a")[0]["log_dir"], "stdout.log").read_text()
    for t in targets:
        assert f"READ-FAIL {t}" in log_a, t
    for secret in ("HOSTKEY", "HOSTAWSSECRET", "sk-ant-HOSTSECRET", "ghp_HHHH", "innocuous-value-xyz"):
        assert secret not in log_a
    assert "ENV ANTHROPIC_API_KEY" not in log_a and "ENV GITHUB_TOKEN" not in log_a and "ENV INNOCUOUS_NAME" not in log_a
    assert_only_mount_ancestors(log_a, "/root", SUPPORT, scenario)  # absent, or just the path down to a mounted dir
    assert "LS-FAIL " + str(fake_home) in log_a and "LS-FAIL /var" in log_a
    env_names = {line.split("=")[0][4:] for line in log_a.splitlines() if line.startswith("ENV ")}
    # (PYTHONDONTWRITEBYTECODE / PYTEST_ADDOPTS are the sandbox's own non-secret hygiene defaults)
    assert env_names <= {"HOME", "LANG", "PATH", "PWD", "TMPDIR", "GRANTED_KEY", "OLDPWD", "SHLVL", "_",
                         "PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS"}
    # per-agent credential grant: only the agent that was granted the variable sees it (and it is redacted in logs)
    prov = {a["task_id"]: a["provider"] for a in st.list_attempts(rid)}
    granted_task = next(t for t, p in prov.items() if p == "alpha")
    other_task = next((t for t, p in prov.items() if p == "beta"), None)
    assert "granted-only-to-alpha-5566" not in Path(st.list_attempts(rid, granted_task)[0]["log_dir"], "stdout.log").read_text()
    if other_task:
        assert "GRANTED_KEY" not in Path(st.list_attempts(rid, other_task)[0]["log_dir"], "stdout.log").read_text()


async def test_at_14_agent_cannot_write_outside_authorized_sandbox(tmp_path, repo, scenario):
    outside = tmp_path / "host-outside"
    outside.mkdir()
    marker_tmp = Path("/tmp") / f"mg-at14-{os.getpid()}"
    import socket
    host_name = socket.gethostname()
    swappiness = Path("/proc/sys/vm/swappiness")
    swap_before = swappiness.read_text() if swappiness.exists() else None
    targets = ["/usr/pwn", "/etc/pwn", "/bin/pwn", "/pwn", "/proc/sys/kernel/hostname", "/proc/sys/vm/swappiness",
               "/proc/sys/kernel/printk", "/dev/pwn", "/workspace/../pwn",
               str(outside / "pwn"), str(repo / "pwn"), str(home() / "pwn"), str(marker_tmp)]
    write_scenario(scenario, "a", {"attempts": {"default": [{"write_files": targets, "dump_status": True,
                                                          "write": {"a/ok.py": "ok=1\n"}}]}})
    rid, out, st = await run_plan(pol(scenario), repo, plan_of(T("a", "a/")))
    log = Path(st.list_attempts(rid, "a")[0]["log_dir"], "stdout.log").read_text()
    for t in ("/usr/pwn", "/etc/pwn", "/bin/pwn", "/pwn", "/proc/sys/kernel/hostname", "/proc/sys/vm/swappiness",
              "/proc/sys/kernel/printk", "/dev/pwn", str(outside / "pwn"), str(repo / "pwn"), str(home() / "pwn")):
        assert f"WRITE-FAIL {t}" in log, (t, log)
    # /tmp inside the sandbox is private tmpfs: the write may "succeed" there but must not exist on the host
    assert socket.gethostname() == host_name  # host kernel state untouched
    assert swap_before is None or swappiness.read_text() == swap_before
    assert not marker_tmp.exists() and not (outside / "pwn").exists() and not (repo / "pwn").exists()
    assert not (home() / "pwn").exists() and not Path("/usr/pwn").exists() and not Path("/etc/pwn").exists()
    assert "CapEff:\t0000000000000000" in log and "NoNewPrivs:\t1" in log
    # a symlink planted in the workspace to escape the repo is rejected by validation, never merged
    write_scenario(scenario, "b", {"attempts": {"default": [{"symlink": {"b/escape": str(outside)}, "write": {"b/ok.py": "1\n"}}]}})
    rid2, out2, st2 = await run_plan(pol(scenario, orchestrator={"max_task_attempts": 1}), repo, plan_of(T("b", "b/", max_attempts=1)))
    v = st2.events(rid2, kinds=("validation.result",))[0]["payload"]
    assert out2.status == "failed" and any(c["name"] == "file_modes" and not c["passed"] for c in v["checks"])
    assert "b/escape" not in git(repo, "ls-tree", "-r", "--name-only", st2.get_run(rid2)["integration_branch"])
    # the agent rewriting its worktree's .git pointer cannot redirect the orchestrator's commit (explicit --git-dir)
    write_scenario(scenario, "c", {"attempts": {"default": [{"write": {".git": f"gitdir: {repo}/.git\n", "c/ok.py": "1\n"}}]}})
    rid3, out3, st3 = await run_plan(pol(scenario), repo, plan_of(T("c", "c/")))
    assert out3.status == "succeeded"
    integ = st3.get_run(rid3)["integration_branch"]
    assert git(repo, "rev-parse", "main") == st3.get_run(rid3)["base_commit"]  # user's branch untouched
    assert git(repo, "ls-tree", "-r", "--name-only", integ).split().count("c/ok.py") == 1
    assert ".git" not in git(repo, "ls-tree", "-r", "--name-only", integ).split()
    assert git(repo, "rev-parse", st3.list_attempts(rid3, "c")[0]["branch"]) == st3.list_attempts(rid3, "c")[0]["commit_sha"]


async def test_at_15_logs_redact_known_credentials(tmp_path, repo, scenario, monkeypatch):
    secret = "live-credential-Zx9Qw7Lm3Vb5Nc1"
    monkeypatch.setenv("PROVIDER_API_KEY", secret)
    monkeypatch.setenv("OTHER_TOKEN", "orchestrator-only-token-8841")
    b64 = base64.b64encode(secret.encode()).decode()
    write_scenario(scenario, "a", {"attempts": {"default": [{
        "dump_env": True, "print": [f"using key {secret}", f"b64 {b64}", f"url https://x?key={secret}",
                                    "pem", "-----BEGIN PRIVATE KEY-----", "MIIEvQIBADANBg", "-----END PRIVATE KEY-----",
                                    "orchestrator-only-token-8841", "token ghp_" + "Q" * 36],
        "stderr": [f"stderr {secret}"], "write": {"a/f.py": "x=1\n"}}]}})
    write_scenario(scenario, "b", {"attempts": {"default": [{"write": {"b/leak.py": f"KEY = '{secret}'\n"}}]}})
    pol_ = pol(scenario, agents={"alpha": ["coding"]}, agent_extra={"alpha": {"credentials": {"env": ["PROVIDER_API_KEY"]}}},
               orchestrator={"max_task_attempts": 1})
    rid, out, st = await run_plan(pol_, repo, plan_of(T("a", "a/"), T("b", "b/", max_attempts=1)))
    states = {t["task_id"]: t["status"] for t in st.list_tasks(rid)}
    assert states == {"a": "succeeded", "b": "failed"}  # committing the credential into code is rejected
    v = [e for e in st.events(rid, kinds=("validation.result",)) if e["task_id"] == "b"][0]["payload"]
    assert any(c["name"] == "no_credentials_added" and not c["passed"] for c in v["checks"])
    forbidden = [secret, b64, "orchestrator-only-token-8841", "ghp_" + "Q" * 36, "MIIEvQIBADANBg"]
    leaked = []
    # Logs, events, reports, the database (+WAL) and every other state file. The preserved *worktrees* are
    # excluded on purpose: they hold the agent's own work product (here: the leaking file it wrote and which
    # validation refused), kept for diagnosis inside a 0700 directory.
    assert (home().stat().st_mode & 0o077) == 0 and ((home() / "runs").stat().st_mode & 0o077) == 0
    assert Path(st.list_attempts(rid, "b")[0]["worktree"], "b", "leak.py").exists()
    for p in home().rglob("*"):
        if p.is_file() and "worktrees" not in p.relative_to(home()).parts:
            data = p.read_bytes()
            leaked += [(str(p.relative_to(home())), f) for f in forbidden if f.encode() in data]
    assert leaked == []
    out_log = Path(st.list_attempts(rid, "a")[0]["log_dir"], "stdout.log").read_text()
    assert "[REDACTED]" in out_log and "using key [REDACTED]" in out_log
    for cmd in (["runs", "show", rid], ["runs", "events", rid, "--limit", "1000"], ["runs", "logs", rid, "a"], ["runs", "show", rid, "--json"]):
        r = cli(*cmd)
        assert not any(f in r.stdout + r.stderr for f in forbidden), cmd
    # even the committed history of the (rejected) attempt branch is the only place the raw text exists, and it was refused
    assert git(repo, "ls-tree", "-r", "--name-only", st.get_run(rid)["integration_branch"]).count("b/leak.py") == 0


async def test_at_16_every_state_transition_is_auditable_and_replayable(tmp_path, repo, scenario):
    # attempt 1: alpha loses its login (provider failure); attempt 2 (beta): stray file -> validation fails;
    # attempt 3: repair. Attempt numbers count every attempt, including the provider failure.
    write_scenario(scenario, "a", {"attempts": {"1": [{"stderr": ["401 unauthorized"], "exit": 1}],
                                                "2": [{"write": {"a/f.py": "1\n", "STRAY": "x"}}],
                                                "default": [{"write": {"a/f.py": "2\n"}, "delete": ["STRAY"]}]}})
    write_scenario(scenario, "b", {"attempts": {"default": [{"exit": 1}]}})
    write_scenario(scenario, "c", writes("c/f.py", "1\n"))
    write_scenario(scenario, "d", writes("d/f.py", "1\n"))
    plan = plan_of(T("a", "a/"), T("b", "b/", max_attempts=1), T("c", "c/", ["b"]), T("d", "d/", ["a"]))
    rid, out, st = await run_plan(pol(scenario, agents={"alpha": ["coding"], "beta": ["coding"]}), repo, plan)
    res = replay_run(st, rid)
    assert res.ok, res.errors
    final = {t["task_id"]: t["status"] for t in st.list_tasks(rid)}
    assert res.task_status == final == {"a": "succeeded", "b": "failed", "c": "blocked", "d": "succeeded"}
    assert res.run_status == "failed"
    # every task's recorded path is a legal walk from PENDING to its final state, with reasons
    from multigenz.core.state_machine import TASK_TRANSITIONS
    for tid in final:
        steps = [e["payload"] for e in st.events(rid, kinds=("task.transition",), task_id=tid)]
        assert steps and all(s["reason"] for s in steps)
        cur = "pending"
        for s in steps:
            assert s["from"] == cur and TaskStatus(s["to"]) in TASK_TRANSITIONS[TaskStatus(cur)]
            cur = s["to"]
        assert cur == final[tid]
    seqs = [e["seq"] for e in st.events(rid)]
    assert seqs == list(range(1, len(seqs) + 1))
    # the CLI exposes the same verification
    a = cli("runs", "audit", rid)
    assert a.returncode == 0 and "audit OK" in a.stdout
    # tampering is detected: altering history (bypassing the append-only trigger) or the live state
    st.con.execute("UPDATE tasks SET status='succeeded' WHERE task_id='b'")
    bad = cli("runs", "audit", rid)
    assert bad.returncode == 1 and "AUDIT ERROR" in bad.stdout
    st.con.execute("UPDATE tasks SET status='failed' WHERE task_id='b'")
    with pytest.raises(Exception, match="append-only"):
        st.con.execute("DELETE FROM events WHERE seq=3")
