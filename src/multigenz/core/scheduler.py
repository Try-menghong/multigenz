"""Scheduling decisions as pure functions over a snapshot. The orchestrator applies the results.

Rules (docs/IMPLEMENTATION_PLAN.md "State and scheduling rules"):
* READY requires every prerequisite to be SUCCEEDED (validated + integrated).
* A failed/blocked/cancelled prerequisite blocks its dependents (transitively).
* Global and per-provider-class concurrency is bounded; one active attempt per task.
* Tasks with overlapping exclusive path ownership are serialised, never run concurrently.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from multigenz.config import Policy
from multigenz.contracts.agent import AdapterProbe
from multigenz.contracts.paths import sets_overlap
from multigenz.contracts.task import TaskSpec, TaskStatus

S = TaskStatus
ACTIVE = (S.RUNNING, S.VALIDATING)
FAILED_LIKE = (S.FAILED, S.BLOCKED, S.CANCELLED)


@dataclass(frozen=True)
class TaskView:
    task_id: str
    spec: TaskSpec
    status: TaskStatus
    excluded: frozenset[str]
    not_before: float | None
    provider: str | None = None
    kind: str = "code"  # "conflict" while a merge-conflict resolution attempt is pending

    @property
    def mode(self) -> str:
        return "conflict" if self.kind == "conflict" else "code"

    @classmethod
    def from_row(cls, row: dict) -> "TaskView":
        return cls(task_id=row["task_id"], spec=TaskSpec.model_validate_json(row["spec"]),
                   status=TaskStatus(row["status"]), excluded=frozenset(json.loads(row["excluded_providers"])),
                   not_before=row["not_before"], provider=row["provider"], kind=row.get("pending_kind") or "code")


def promotable(tasks: list[TaskView]) -> list[str]:
    st = {t.task_id: t.status for t in tasks}
    return [t.task_id for t in tasks
            if t.status == S.PENDING and all(st.get(d) == S.SUCCEEDED for d in t.spec.dependencies)]


def blocked_by_failure(tasks: list[TaskView]) -> list[tuple[str, str]]:
    """(task_id, cause) for PENDING/READY tasks with a failed-like prerequisite."""
    st = {t.task_id: t.status for t in tasks}
    out = []
    for t in tasks:
        if t.status in (S.PENDING, S.READY):
            for d in t.spec.dependencies:
                if st.get(d) in FAILED_LIKE:
                    out.append((t.task_id, d))
                    break
    return out


@dataclass
class Capacity:
    max_cli: int
    max_api: int
    per_agent: dict[str, int | None]
    running_cli: int = 0
    running_api: int = 0
    running_by_agent: dict[str, int] = field(default_factory=dict)

    def room(self, agent: str, klass: str) -> bool:
        if klass == "api":
            if self.running_api >= self.max_api:
                return False
        elif self.running_cli >= self.max_cli:
            return False
        cap = self.per_agent.get(agent)
        return cap is None or self.running_by_agent.get(agent, 0) < cap

    def take(self, agent: str, klass: str) -> None:
        if klass == "api":
            self.running_api += 1
        else:
            self.running_cli += 1
        self.running_by_agent[agent] = self.running_by_agent.get(agent, 0) + 1


def capacity_from(policy: Policy, running_agents: list[tuple[str, str]]) -> Capacity:
    cap = Capacity(max_cli=policy.orchestrator.max_parallel_agents, max_api=policy.orchestrator.max_api_requests,
                   per_agent={n: a.max_concurrent for n, a in policy.agents.items()})
    for agent, klass in running_agents:
        cap.take(agent, klass)
    return cap


def candidates(spec: TaskSpec, mode: str, probes: dict[str, AdapterProbe], *, excluded: frozenset[str],
               disabled: set[str]) -> list[str]:
    """Agents able to run ``spec`` in ``mode``: enabled, mode verified, capabilities covered."""
    out = []
    for name, p in probes.items():
        if name in excluded or name in disabled or not p.enabled or mode not in p.modes:
            continue
        if not spec.required_capabilities <= set(p.capabilities):
            continue
        out.append(name)
    return out


def pick_dispatch(ready: list[TaskView], active: list[TaskView], *, policy: Policy,
                  probes: dict[str, AdapterProbe], klass_of: dict[str, str], disabled: set[str],
                  running_agents: list[tuple[str, str]], now: float | None = None) -> list[tuple[str, str]]:
    """Choose (task_id, agent) pairs to start now, honouring all limits."""
    now = time.time() if now is None else now
    cap = capacity_from(policy, running_agents)
    claimed = [t.spec.allowed_paths for t in active]
    chosen: list[tuple[str, str]] = []
    for t in ready:
        if t.not_before is not None and t.not_before > now:
            continue
        if any(sets_overlap(t.spec.allowed_paths, other) for other in claimed):
            continue  # serialise conflicting exclusive ownership
        cands = candidates(t.spec, t.mode, probes, excluded=t.excluded, disabled=disabled)
        order = list(policy.agents)  # policy order is the tie-breaker
        cands.sort(key=lambda n: (cap.running_by_agent.get(n, 0), order.index(n)))
        for agent in cands:
            klass = klass_of.get(agent, "cli")
            if cap.room(agent, klass):
                cap.take(agent, klass)
                chosen.append((t.task_id, agent))
                claimed.append(t.spec.allowed_paths)
                break
    return chosen


def unroutable(ready: list[TaskView], *, probes: dict[str, AdapterProbe], disabled: set[str]) -> list[str]:
    """READY tasks for which no agent could *ever* run (ignoring current load)."""
    return [t.task_id for t in ready
            if not candidates(t.spec, t.mode, probes, excluded=t.excluded, disabled=disabled)]
