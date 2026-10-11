"""Validation and report result models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class CommandReport(BaseModel):
    argv: list[str]
    exit_code: int | None
    timed_out: bool = False
    duration_seconds: float = 0.0
    output_tail: str = ""
    log_path: str | None = None


class TestReport(BaseModel):
    profile: str
    passed: bool
    commands: list[CommandReport] = Field(default_factory=list)


class ValidationResult(BaseModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    test_reports: list[TestReport] = Field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def feedback(self, limit: int = 4000) -> str:
        lines = [f"- {c.name}: {c.detail}" for c in self.failures]
        for tr in self.test_reports:
            if not tr.passed:
                for c in tr.commands:
                    if c.exit_code != 0:
                        lines.append(f"- test profile {tr.profile!r} command {c.argv} exit={c.exit_code}"
                                     f"{' (timeout)' if c.timed_out else ''}:\n{c.output_tail}")
        return "\n".join(lines)[:limit]
