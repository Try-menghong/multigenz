import json
import sqlite3

import pytest

from multigenz.contracts.task import TaskStatus
from multigenz.core.redaction import Redactor
from multigenz.core.replay import replay_run
from multigenz.core.state_machine import StateMachine, TASK_TRANSITIONS, can_transition
from multigenz.errors import InvalidTransition
from multigenz.storage.sqlite import Store

S = TaskStatus


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "t.db", Redactor(["SUPERSECRETVALUE"]))
    s.create_run(run_id="r1", objective="o", repo_path="/x", repo_identity="i", base_commit="b", mode="autonomous",
                 policy_snapshot="{}", policy_sha256="s", status="created", integration_branch="multigenz/run-r1/integration")
    s.emit("r1", "run.created", payload={"status": "created"})
    return s


def reg(st, tid="t1"):
    st.insert_task("r1", tid, "{}", "pending")
    st.emit("r1", "task.registered", task_id=tid, payload={})


def test_wal_mode_and_pragmas(st):
    assert st.q1("PRAGMA journal_mode")["journal_mode"] == "wal"
    assert st.q1("PRAGMA foreign_keys")["foreign_keys"] == 1


def test_transition_table_forbids_shortcuts():
    assert not can_transition(S.RUNNING, S.SUCCEEDED) and not can_transition(S.PENDING, S.RUNNING)
    assert not can_transition(S.READY, S.SUCCEEDED) and can_transition(S.VALIDATING, S.SUCCEEDED)
    for terminal in (S.SUCCEEDED, S.FAILED, S.BLOCKED, S.CANCELLED):
        assert TASK_TRANSITIONS[terminal] == frozenset()


def test_illegal_transition_rejected_and_nothing_recorded(st):
    reg(st)
    sm = StateMachine(st)
    n = st.q1("select event_seq from runs")["event_seq"]
    with pytest.raises(InvalidTransition):
        sm.task("r1", "t1", S.RUNNING)
    assert st.get_task("r1", "t1")["status"] == "pending" and st.q1("select event_seq from runs")["event_seq"] == n


def test_success_requires_validation_evidence_at08(st):
    reg(st)
    sm = StateMachine(st)
    sm.task("r1", "t1", S.READY)
    st.insert_attempt(attempt_id="a1", run_id="r1", task_id="t1", number=1, kind="code", provider="p", status="leased",
                      started_at="x", idempotency_key="k1")
    sm.task("r1", "t1", S.RUNNING, attempt_id="a1")
    with pytest.raises(InvalidTransition, match="mark_succeeded"):
        sm.task("r1", "t1", S.SUCCEEDED)
    sm.task("r1", "t1", S.VALIDATING, attempt_id="a1")
    st.update_attempt("a1", status="failed", exit_code=1, error_code="nonzero_exit")
    with pytest.raises(InvalidTransition, match="did not finish cleanly"):
        sm.mark_succeeded("r1", "t1", "a1", require_integration=False)
    st.update_attempt("a1", status="finished", exit_code=0, error_code=None)
    with pytest.raises(InvalidTransition, match="no passing validation"):
        sm.mark_succeeded("r1", "t1", "a1", require_integration=False)
    st.emit("r1", "validation.result", task_id="t1", attempt_id="a1", payload={"passed": False})
    with pytest.raises(InvalidTransition, match="no passing validation"):
        sm.mark_succeeded("r1", "t1", "a1", require_integration=False)
    st.emit("r1", "validation.result", task_id="t1", attempt_id="a1", payload={"passed": True})
    with pytest.raises(InvalidTransition, match="not been integrated"):
        sm.mark_succeeded("r1", "t1", "a1")
    iid = st.add_integration("r1", "t1", "a1", "merged", "c", "h")
    sm.mark_succeeded("r1", "t1", "a1")
    assert st.get_task("r1", "t1")["status"] == "succeeded"


def test_one_active_lease_per_task_enforced_by_database(st):
    reg(st)
    kw = dict(run_id="r1", task_id="t1", number=1, kind="code", provider="p", started_at="x")
    st.insert_attempt(attempt_id="a1", status="leased", idempotency_key="k1", **kw)
    with pytest.raises(sqlite3.IntegrityError):
        st.insert_attempt(attempt_id="a2", status="running", idempotency_key="k2", **kw)
    st.update_attempt("a1", status="finished")
    st.insert_attempt(attempt_id="a2", status="leased", idempotency_key="k2", **kw)  # ok after release
    with pytest.raises(sqlite3.IntegrityError):
        st.insert_attempt(attempt_id="a3", status="finished", idempotency_key="k2", **kw)  # idempotency key unique


def test_transition_and_event_are_atomic(st):
    reg(st)
    sm = StateMachine(st)
    seq = st.q1("select event_seq from runs")["event_seq"]
    with pytest.raises(RuntimeError):
        with st.tx():
            sm.task("r1", "t1", S.READY)
            raise RuntimeError("crash mid-transaction")
    assert st.get_task("r1", "t1")["status"] == "pending"
    assert st.q1("select event_seq from runs")["event_seq"] == seq
    assert len(st.events("r1")) == seq


def test_events_append_only_redacted_and_chained(st):
    reg(st)
    st.emit("r1", "x", payload={"msg": "token=SUPERSECRETVALUE here", "nested": ["SUPERSECRETVALUE"]})
    raw = json.dumps(st.q("select payload from events"))
    assert "SUPERSECRETVALUE" not in raw and "[REDACTED]" in raw
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.con.execute("UPDATE events SET kind='evil'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.con.execute("DELETE FROM events")
    ev = st.events("r1")
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))
    assert all(ev[i]["prev_hash"] == ev[i - 1]["hash"] for i in range(1, len(ev)))


def test_at16_replay_reproduces_state_and_detects_tampering(st, tmp_path):
    reg(st)
    reg(st, "t2")
    sm = StateMachine(st)
    sm.run("r1", "running")
    sm.task("r1", "t1", S.READY)
    sm.task("r1", "t1", S.RUNNING)
    sm.task("r1", "t1", S.FAILED, reason="boom")
    sm.task("r1", "t2", S.BLOCKED)
    res = replay_run(st, "r1")
    assert res.ok, res.errors
    assert res.task_status == {"t1": "failed", "t2": "blocked"} and res.run_status == "running"
    # tamper with the live table -> replay disagrees
    st.con.execute("UPDATE tasks SET status='succeeded' WHERE task_id='t1'")
    assert any("t1" in e for e in replay_run(st, "r1").errors)
    # tamper with an event payload by bypassing the trigger -> hash chain breaks
    st.con.execute("UPDATE tasks SET status='failed' WHERE task_id='t1'")
    st.con.execute("DROP TRIGGER events_no_update")
    st.con.execute("UPDATE events SET payload='{\"from\":\"pending\",\"to\":\"ready\",\"reason\":\"forged\"}' WHERE seq=5")
    assert any("hash mismatch" in e for e in replay_run(st, "r1").errors)


def test_run_state_machine(st):
    sm = StateMachine(st)
    sm.run("r1", "running")
    with pytest.raises(InvalidTransition):
        sm.run("r1", "created")
    sm.run("r1", "succeeded")
    with pytest.raises(InvalidTransition):
        sm.run("r1", "failed")
    assert st.get_run("r1")["finished_at"]
