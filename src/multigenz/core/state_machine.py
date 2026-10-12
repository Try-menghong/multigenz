"""Task/run state machine. Every transition is validated and recorded in the same transaction.

Structural guarantees:
* RUNNING -> SUCCEEDED does not exist: success is reachable only from VALIDATING.
* ``mark_succeeded`` additionally requires persisted evidence: a finished attempt with a clean
  exit and a *passing* ``validation.result`` event for that very attempt (and, when integration
  is enabled, a merged integration record).
"""

from __future__ import annotations

from typing import Any

from multigenz.contracts.event import RUN_TRANSITION, TASK_TRANSITION
from multigenz.contracts.task import TaskStatus
from multigenz.errors import InvalidTransition
from multigenz.storage.sqlite import Store

T = TaskStatus

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    T.PENDING: frozenset({T.READY, T.BLOCKED, T.CANCELLED}),
    T.READY: frozenset({T.RUNNING, T.BLOCKED, T.FAILED, T.CANCELLED}),
    T.RUNNING: frozenset({T.VALIDATING, T.READY, T.FAILED, T.CANCELLED, T.SPLIT}),
    T.VALIDATING: frozenset({T.SUCCEEDED, T.READY, T.FAILED, T.CANCELLED}),
    T.SUCCEEDED: frozenset(),
    T.FAILED: frozenset(),
    T.BLOCKED: frozenset(),
    T.CANCELLED: frozenset(),
    T.SPLIT: frozenset(),
}

RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"planning", "running", "plan_rejected", "cancelled"}),
    "planning": frozenset({"running", "plan_rejected", "failed", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "plan_rejected": frozenset(),
}


def can_transition(frm: TaskStatus, to: TaskStatus) -> bool:
    return to in TASK_TRANSITIONS[frm]


class StateMachine:
    def __init__(self, store: Store):
        self.store = store

    def task(self, run_id: str, task_id: str, to: TaskStatus, *, reason: str = "", attempt_id: str | None = None,
             extra: dict[str, Any] | None = None, **fields: Any) -> None:
        if to == T.SUCCEEDED:
            raise InvalidTransition("use mark_succeeded(): success requires validation evidence")
        self._apply(run_id, task_id, to, reason, attempt_id, extra, fields)

    def mark_succeeded(self, run_id: str, task_id: str, attempt_id: str, *, require_integration: bool = True,
                       reason: str = "validated", **fields: Any) -> None:
        st = self.store
        with st.tx():
            att = st.get_attempt(attempt_id)
            if att is None or att["run_id"] != run_id or att["task_id"] != task_id:
                raise InvalidTransition(f"attempt {attempt_id} does not belong to {task_id}")
            if att["status"] != "finished" or att["error_code"] is not None or att["exit_code"] != 0:
                raise InvalidTransition(
                    f"attempt {attempt_id} did not finish cleanly (status={att['status']}, "
                    f"exit={att['exit_code']}, error={att['error_code']}); cannot mark task succeeded")
            ok = [e for e in st.events(run_id, kinds=("validation.result",), task_id=task_id)
                  if e["attempt_id"] == attempt_id and e["payload"].get("passed") is True]
            if not ok:
                raise InvalidTransition(f"no passing validation recorded for attempt {attempt_id}")
            if require_integration:
                merged = [i for i in st.list_integrations(run_id)
                          if i["task_id"] == task_id and i["attempt_id"] == attempt_id and i["status"] == "merged"]
                if not merged:
                    raise InvalidTransition(f"attempt {attempt_id} has not been integrated")
            self._apply(run_id, task_id, T.SUCCEEDED, reason, attempt_id, None, fields)

    def _apply(self, run_id: str, task_id: str, to: TaskStatus, reason: str, attempt_id: str | None,
               extra: dict[str, Any] | None, fields: dict[str, Any]) -> None:
        st = self.store
        with st.tx():
            row = st.get_task(run_id, task_id)
            if row is None:
                raise InvalidTransition(f"unknown task {task_id}")
            frm = TaskStatus(row["status"])
            if to not in TASK_TRANSITIONS[frm]:
                raise InvalidTransition(f"task {task_id}: {frm.value} -> {to.value} is not allowed")
            st.update_task(run_id, task_id, status=to.value, **fields)
            st.emit(run_id, TASK_TRANSITION, task_id=task_id, attempt_id=attempt_id,
                    payload={"from": frm.value, "to": to.value, "reason": reason, **(extra or {})})

    def run(self, run_id: str, to: str, *, reason: str = "", **fields: Any) -> None:
        st = self.store
        with st.tx():
            row = st.get_run(run_id)
            if row is None:
                raise InvalidTransition(f"unknown run {run_id}")
            frm = row["status"]
            if to not in RUN_TRANSITIONS.get(frm, frozenset()):
                raise InvalidTransition(f"run {run_id}: {frm} -> {to} is not allowed")
            if to in ("succeeded", "failed", "cancelled", "plan_rejected"):
                from multigenz.storage.sqlite import now_iso
                fields.setdefault("finished_at", now_iso())
            st.update_run(run_id, status=to, **fields)
            st.emit(run_id, RUN_TRANSITION, payload={"from": frm, "to": to, "reason": reason})
