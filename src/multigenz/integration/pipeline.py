"""Validation + integration pipeline applied to every agent result by the orchestrator."""

from __future__ import annotations

from pathlib import Path

from multigenz.config import Policy
from multigenz.contracts.result import CheckResult, ValidationResult
from multigenz.contracts.task import TaskSpec
from multigenz.core.redaction import Redactor
from multigenz.execution.git import Git
from multigenz.integration.merger import IntegrationResult, Merger
from multigenz.integration.validator import DiffValidator


class IntegrationPipeline:
    def __init__(self, git: Git, policy: Policy, redactor: Redactor, merger: Merger):
        self.git = git
        self.policy = policy
        self.redactor = redactor
        self.merger = merger
        self.diff = DiffValidator(git, policy, redactor)

    async def validate(self, task: TaskSpec, base: str, head: str, *, attempt_id: str) -> ValidationResult:
        checks, paths = self.diff.validate(task, base, head)
        return ValidationResult(passed=all(c.passed for c in checks), checks=checks, changed_paths=paths)

    async def integrate(self, task: TaskSpec, head: str, *, attempt_id: str) -> IntegrationResult:
        return await self.merger.integrate(task.task_id, attempt_id, head)
