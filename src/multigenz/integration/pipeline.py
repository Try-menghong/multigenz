"""Validation + integration pipeline applied to every agent result by the orchestrator."""

from __future__ import annotations

from pathlib import Path

from multigenz.config import Policy
from multigenz.contracts.result import CheckResult, TestReport, ValidationResult
from multigenz.contracts.task import TaskSpec
from multigenz.core.redaction import Redactor
from multigenz.execution.git import Git
from multigenz.execution.manager import ExecutionRunner
from multigenz.integration.merger import IntegrationResult, Merger
from multigenz.integration.profiles import ProfileRunner
from multigenz.integration.reviewer import AgentReviewer, DiffReviewer
from multigenz.integration.validator import DiffValidator

MARKERS = ("<<<<<<< ", ">>>>>>> ", "\n=======\n")


class IntegrationPipeline:
    def __init__(self, git: Git, policy: Policy, redactor: Redactor, merger: Merger, *,
                 runner: ExecutionRunner | None = None, paths=None, agent_reviewer: AgentReviewer | None = None):
        self.git = git
        self.policy = policy
        self.redactor = redactor
        self.merger = merger
        self.diff = DiffValidator(git, policy, redactor)
        self.reviewer = DiffReviewer(git)
        self.agent_reviewer = agent_reviewer
        self.profiles = ProfileRunner(runner, policy, paths.run_dir / "tests") if runner and paths else None

    # -- helpers --------------------------------------------------------------------------
    def _profile_for(self, task: TaskSpec | None) -> str | None:
        v = self.policy.validation
        return (task.validation_profile if task and task.validation_profile else v.default_profile)

    def _tests_check(self, profile: str | None, what: str) -> CheckResult | None:
        """Fail closed: required tests that cannot be run are a validation failure."""
        if profile is not None and self.profiles is not None:
            return None
        if self.policy.validation.require_tests:
            return CheckResult(name="tests_configured", passed=False,
                               detail=f"{what}: require_tests is true but no validation profile is available; "
                                      "an agent's result cannot be verified")
        return CheckResult(name="tests_configured", passed=True, detail="tests not required by policy")

    # -- validation -----------------------------------------------------------------------------
    async def validate(self, task: TaskSpec, base: str, head: str, *, attempt_id: str,
                       cancel=None) -> ValidationResult:
        # 1. everything that does not execute candidate code (reported together for useful feedback)
        checks, paths = self.diff.validate(task, base, head)
        if self.policy.validation.require_diff_review and self.git.rev_parse(head) is not None \
                and self.git.is_ancestor(base, head):
            checks += self.reviewer.review(task, base, head)
        reports: list[TestReport] = []
        # 2. only a policy-clean result is ever executed (tests), then optionally shown to a reviewer agent
        if all(c.passed for c in checks):
            profile = self._profile_for(task)
            miss = self._tests_check(profile, f"task {task.task_id}")
            if miss is not None:
                checks.append(miss)
            elif profile is not None:
                rep = await self.profiles.run_on_commit(self.git, profile, head, label=f"{task.task_id}-validate",
                                                        cancel=cancel)
                reports.append(rep)
                checks.append(CheckResult(name=f"tests[{profile}]", passed=rep.passed,
                                          detail="" if rep.passed else "test profile failed"))
        agent_review = self.policy.validation.review_with_agent or bool(self.policy.roles.reviewer)
        if all(c.passed for c in checks) and self.policy.validation.require_diff_review \
                and agent_review and self.agent_reviewer:
            verdict = await self.agent_reviewer.review(task, base, head)
            if verdict is not None:
                checks.append(verdict)
        return ValidationResult(passed=all(c.passed for c in checks), checks=checks, changed_paths=paths,
                                test_reports=reports)

    async def validate_conflict_resolution(self, task: TaskSpec, target: str, candidate: str, result: str, *,
                                           attempt_id: str, cancel=None) -> ValidationResult:
        """A conflict resolution must be a real merge of both sides, with no markers left behind."""
        g = self.git
        checks: list[CheckResult] = []
        parents = g.out("rev-list", "--parents", "-n", "1", result).split()[1:]
        both = (g.is_ancestor(candidate, result) and g.is_ancestor(target, result))
        checks.append(CheckResult(name="preserves_both_sides", passed=both and len(parents) >= 2,
                                  detail="" if both and len(parents) >= 2 else
                                  "resolution is not a merge containing both the task commit and the integration head"))
        diff_checks, paths = self.diff.validate(task, target, result, allow_empty=True)
        checks += diff_checks
        left = []
        for c in g.diff_raw(target, result):
            if c.status != "D" and c.new_mode in ("100644", "100755"):
                blob = g.read_blob(c.new_sha, 2_000_000).decode("utf-8", "replace")
                if any(m in "\n" + blob for m in ("\n<<<<<<< ", "\n>>>>>>> ")) :
                    left.append(c.path)
        checks.append(CheckResult(name="no_conflict_markers", passed=not left,
                                  detail=f"conflict markers remain in {left}" if left else ""))
        reports: list[TestReport] = []
        if all(c.passed for c in checks):
            profile = self._profile_for(task)
            miss = self._tests_check(profile, f"task {task.task_id}")
            if miss is not None:
                checks.append(miss)
            elif profile is not None:
                rep = await self.profiles.run_on_commit(self.git, profile, result, label=f"{task.task_id}-resolve",
                                                        cancel=cancel)
                reports.append(rep)
                checks.append(CheckResult(name=f"tests[{profile}]", passed=rep.passed,
                                          detail="" if rep.passed else "test profile failed"))
        return ValidationResult(passed=all(c.passed for c in checks), checks=checks, changed_paths=paths,
                                test_reports=reports)

    # -- integration ------------------------------------------------------------------------------
    async def integrate(self, task: TaskSpec, head: str, *, attempt_id: str, cancel=None) -> IntegrationResult:
        """Merge only if the *merged tree* also passes the integration profile."""
        profile = self.policy.validation.integration_profile or self._profile_for(task)

        async def check(tree: Path):
            miss = self._tests_check(profile, "integration")
            if miss is not None and not miss.passed:
                return False, miss.detail, []
            if profile is None or self.profiles is None:
                return True, "", []
            rep = await self.profiles.run(profile, tree, label=f"{task.task_id}-integration", cancel=cancel)
            return rep.passed, ("" if rep.passed else "integration tests failed on the merged tree:\n"
                                + "\n".join(f"{c.argv}: exit={c.exit_code}\n{c.output_tail}" for c in rep.commands
                                            if c.exit_code != 0)), [rep]

        return await self.merger.integrate(task.task_id, attempt_id, head, check=check)

    async def final_check(self, head: str, *, cancel=None) -> TestReport | None:
        profile = self.policy.validation.integration_profile or self.policy.validation.default_profile
        if profile is None or self.profiles is None:
            return None
        return await self.profiles.run_on_commit(self.git, profile, head, label="final", cancel=cancel)
