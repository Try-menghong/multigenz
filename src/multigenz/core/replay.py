"""Audit and replay: rebuild state from the event log alone and compare with live tables (AT-16)."""

from __future__ import annotations

from dataclasses import dataclass, field

from multigenz.contracts.task import TaskStatus
from multigenz.core.state_machine import RUN_TRANSITIONS, TASK_TRANSITIONS
from multigenz.storage.sqlite import Store, event_hash, canonical


@dataclass
class ReplayResult:
    run_status: str | None = None
    task_status: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    events: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def replay_run(store: Store, run_id: str) -> ReplayResult:
    res = ReplayResult()
    rows = store.q("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))
    res.events = len(rows)
    prev_hash = ""
    for expected, e in enumerate(rows, start=1):
        if e["seq"] != expected:
            res.errors.append(f"sequence gap: expected {expected}, found {e['seq']}")
            expected = e["seq"]
        if e["prev_hash"] != prev_hash:
            res.errors.append(f"seq {e['seq']}: hash chain broken (prev_hash mismatch)")
        want = event_hash(e["prev_hash"], run_id, e["seq"], e["ts"], e["kind"], e["task_id"], e["attempt_id"],
                          e["payload"])
        if want != e["hash"]:
            res.errors.append(f"seq {e['seq']}: event hash mismatch (tampered?)")
        prev_hash = e["hash"]
        import json
        p = json.loads(e["payload"])
        kind = e["kind"]
        if kind == "run.created":
            res.run_status = p.get("status", "created")
        elif kind == "run.transition":
            if res.run_status != p.get("from"):
                res.errors.append(f"seq {e['seq']}: run transition from {p.get('from')} but replayed state is {res.run_status}")
            if p.get("to") not in RUN_TRANSITIONS.get(p.get("from"), frozenset()):
                res.errors.append(f"seq {e['seq']}: illegal run transition {p.get('from')} -> {p.get('to')}")
            res.run_status = p.get("to")
        elif kind == "task.registered":
            res.task_status[e["task_id"]] = TaskStatus.PENDING.value
        elif kind == "task.transition":
            tid = e["task_id"]
            cur = res.task_status.get(tid)
            if cur != p.get("from"):
                res.errors.append(f"seq {e['seq']}: task {tid} transition from {p.get('from')} but replayed state is {cur}")
            try:
                if TaskStatus(p["to"]) not in TASK_TRANSITIONS[TaskStatus(p["from"])]:
                    res.errors.append(f"seq {e['seq']}: illegal task transition {p['from']} -> {p['to']}")
            except (KeyError, ValueError):
                res.errors.append(f"seq {e['seq']}: malformed task transition payload")
            res.task_status[tid] = p.get("to")
    run = store.get_run(run_id)
    if run is None:
        res.errors.append("run row missing")
        return res
    if run["event_seq"] != len(rows):
        res.errors.append(f"runs.event_seq={run['event_seq']} but {len(rows)} events stored")
    if run["last_event_hash"] != prev_hash:
        res.errors.append("runs.last_event_hash does not match the last event")
    if res.run_status != run["status"]:
        res.errors.append(f"run status: replay={res.run_status} table={run['status']}")
    live = {t["task_id"]: t["status"] for t in store.list_tasks(run_id)}
    for tid in sorted(set(live) | set(res.task_status)):
        if live.get(tid) != res.task_status.get(tid):
            res.errors.append(f"task {tid}: replay={res.task_status.get(tid)} table={live.get(tid)}")
    return res
