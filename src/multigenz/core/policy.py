"""Policy engine. Lives in the orchestrator, outside every agent sandbox.

No model output can grant permissions: models only ever produce *proposals* that this module
validates against the administrator-owned :class:`multigenz.config.Policy`.
"""

from __future__ import annotations

import re

from multigenz.config import Policy
from multigenz.contracts.paths import path_matches
from multigenz.contracts.task import TaskSpec
from multigenz.errors import PolicyViolation

# Actions the orchestrator may take without separate authorization.
AUTONOMOUS_ACTIONS = frozenset(
    {
        "worktree.create",
        "branch.create_run_owned",
        "merge.run_integration",
        "sandbox.exec",
        "validation.run_profile",
    }
)

# Actions that always require separate, out-of-band authorization (not implemented in v1).
RESTRICTED_ACTIONS = frozenset(
    {"git.push", "deploy", "infra.modify", "branch.write_protected", "branch.delete", "git.force"}
)

_RUN_REF_RE = re.compile(
    r"^(?:refs/heads/)?multigenz/run-(?P<run>[a-z0-9]+)/(?P<kind>integration|task-[a-z0-9][a-z0-9_-]{0,47}(?:\.(?:a\d{1,2}|c\d{1,2}))?)$"
)


class PolicyEngine:
    def __init__(self, policy: Policy):
        self.policy = policy
        self._denied = [re.compile(p, re.I) for p in policy.goals.denied_patterns]

    # -- goals ---------------------------------------------------------------------
    def check_text(self, text: str, *, what: str = "goal") -> None:
        if len(text) > self.policy.goals.max_objective_chars:
            raise PolicyViolation(f"{what} exceeds {self.policy.goals.max_objective_chars} characters")
        for rx in self._denied:
            m = rx.search(text)
            if m:
                raise PolicyViolation(f"{what} is out of policy (matched {rx.pattern!r}: {m.group(0)!r})")

    def check_task(self, task: TaskSpec) -> list[str]:
        issues: list[str] = []
        for what, text in (("title", task.title), ("objective", task.objective)):
            try:
                self.check_text(text, what=f"task {task.task_id} {what}")
            except PolicyViolation as exc:
                issues.append(str(exc))
        forbidden = self.policy.validation.forbidden_paths
        for p in task.writable_paths:
            probe = p if not p.endswith("/") else p
            if path_matches(probe, forbidden) or any(
                f.rstrip("/") == probe.rstrip("/") or probe.startswith(f) for f in forbidden
            ):
                issues.append(f"task {task.task_id}: path {p!r} is forbidden by policy")
        prof = task.validation_profile
        if prof is not None and prof not in self.policy.validation.profiles:
            issues.append(f"task {task.task_id}: unknown validation profile {prof!r}")
        return issues

    # -- actions -------------------------------------------------------------------
    def authorize(self, action: str) -> None:
        if action in AUTONOMOUS_ACTIONS:
            return
        if action in RESTRICTED_ACTIONS:
            raise PolicyViolation(f"action {action!r} requires separate authorization and is disabled")
        raise PolicyViolation(f"unknown action {action!r} is denied (fail closed)")

    # -- refs ----------------------------------------------------------------------
    @staticmethod
    def assert_run_ref(run_id: str, ref: str) -> None:
        """Only branches in this run's namespace may ever be created or updated."""
        m = _RUN_REF_RE.match(ref)
        if not m or m.group("run") != run_id:
            raise PolicyViolation(f"ref {ref!r} is outside run namespace multigenz/run-{run_id}/")

    def path_forbidden(self, path: str) -> bool:
        f = self.policy.validation.forbidden_paths
        return path_matches(path, f) or any(path == x.rstrip("/") for x in f)
