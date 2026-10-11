"""Plan/DAG validation. Runs before any execution (docs/ARCHITECTURE.md "Task contract")."""

from __future__ import annotations

from collections import defaultdict, deque

from multigenz.contracts.paths import sets_overlap
from multigenz.contracts.task import PlanDocument, TaskSpec
from multigenz.core.policy import PolicyEngine
from multigenz.errors import PlanValidationError

MAX_TASKS = 64


def find_cycle(tasks: list[TaskSpec]) -> list[str] | None:
    """Return the task ids of one cycle (Kahn leftover) or None."""
    ids = {t.task_id for t in tasks}
    indeg = {t.task_id: 0 for t in tasks}
    children: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for d in t.dependencies:
            if d in ids:
                indeg[t.task_id] += 1
                children[d].append(t.task_id)
    q = deque(i for i, n in indeg.items() if n == 0)
    seen = 0
    while q:
        cur = q.popleft()
        seen += 1
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    if seen == len(tasks):
        return None
    return sorted(i for i, n in indeg.items() if n > 0)


def ancestors(tasks: list[TaskSpec]) -> dict[str, set[str]]:
    by_id = {t.task_id: t for t in tasks}
    memo: dict[str, set[str]] = {}

    def anc(i: str, stack: frozenset[str] = frozenset()) -> set[str]:
        if i in memo:
            return memo[i]
        out: set[str] = set()
        for d in by_id[i].dependencies:
            if d in by_id and d not in stack:
                out |= {d} | anc(d, stack | {i})
        memo[i] = out
        return out

    return {t.task_id: anc(t.task_id) for t in tasks}


def topological_order(tasks: list[TaskSpec]) -> list[str]:
    order: list[str] = []
    placed: set[str] = set()
    remaining = list(tasks)
    while remaining:
        progressed = False
        for t in list(remaining):
            if all(d in placed for d in t.dependencies):
                order.append(t.task_id)
                placed.add(t.task_id)
                remaining.remove(t)
                progressed = True
        if not progressed:
            raise PlanValidationError([f"dependency cycle among {sorted(t.task_id for t in remaining)}"])
    return order


def validate_plan(plan: PlanDocument, engine: PolicyEngine | None = None, *,
                  available_capabilities: list[set[str]] | None = None) -> list[str]:
    """All problems with a plan; empty list means valid.

    ``available_capabilities`` is one capability set per usable code-capable agent; every task's
    required capabilities must be covered by at least one of them.
    """
    issues: list[str] = []
    tasks = plan.tasks
    if not tasks:
        issues.append("plan has no tasks")
        return issues
    if len(tasks) > MAX_TASKS:
        issues.append(f"plan has {len(tasks)} tasks (limit {MAX_TASKS})")
    seen: set[str] = set()
    for t in tasks:
        if t.task_id in seen:
            issues.append(f"duplicate task id {t.task_id!r}")
        seen.add(t.task_id)
    for t in tasks:
        for d in t.dependencies:
            if d not in seen:
                issues.append(f"task {t.task_id!r} depends on unknown task {d!r}")
    if issues:
        return issues  # structural problems make the remaining checks meaningless
    cycle = find_cycle(tasks)
    if cycle:
        issues.append(f"dependency cycle detected among tasks {cycle}")
        return issues
    anc = ancestors(tasks)
    for i, a in enumerate(tasks):
        for b in tasks[i + 1 :]:
            if a.task_id in anc[b.task_id] or b.task_id in anc[a.task_id]:
                continue  # ordered by dependency: serialised, no ownership conflict
            clash = sets_overlap(a.allowed_paths, b.allowed_paths) or \
                sets_overlap(a.shared_paths, b.allowed_paths) or sets_overlap(a.allowed_paths, b.shared_paths)
            if clash:
                issues.append(f"tasks {a.task_id!r} and {b.task_id!r} claim overlapping exclusive paths "
                              f"without a dependency between them")
    if engine is not None:
        for t in tasks:
            issues += engine.check_task(t)
    if available_capabilities is not None:
        for t in tasks:
            if not any(t.required_capabilities <= caps for caps in available_capabilities):
                issues.append(f"task {t.task_id!r} requires capabilities {sorted(t.required_capabilities)} "
                              f"which no usable agent provides")
    return issues


def assert_valid_plan(plan: PlanDocument, engine: PolicyEngine | None = None, **kw) -> None:
    issues = validate_plan(plan, engine, **kw)
    if issues:
        raise PlanValidationError(issues)
