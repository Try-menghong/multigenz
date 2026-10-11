"""Exception hierarchy. Every failure the orchestrator can reason about has a type."""

from __future__ import annotations


class MultiGenzError(Exception):
    """Base class for all expected orchestrator errors."""


class ConfigError(MultiGenzError):
    """Policy/config file is invalid. Fails closed: nothing runs on invalid policy."""


class PolicyViolation(MultiGenzError):
    """An action or artifact violates host policy."""


class IsolationUnavailableError(MultiGenzError):
    """Required isolation is not available. Never silently downgraded."""


class PlanValidationError(MultiGenzError):
    def __init__(self, issues: list[str]):
        self.issues = list(issues)
        super().__init__("; ".join(self.issues))


class GitError(MultiGenzError):
    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)


class WorktreeError(MultiGenzError):
    """Worktree creation/removal refused (e.g. would overwrite an existing one)."""


class InvalidTransition(MultiGenzError):
    """A state-machine transition that is not allowed."""


class LeaseError(MultiGenzError):
    """Attempt lease could not be acquired (another active attempt exists)."""


class RunLockedError(MultiGenzError):
    """Another orchestrator process owns this run."""


class AdapterUnavailable(MultiGenzError):
    """Adapter is disabled (missing binary, no verified non-interactive mode, ...)."""
