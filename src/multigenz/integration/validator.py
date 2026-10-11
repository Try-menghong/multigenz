"""Independent validation of an agent's result. Never trusts exit codes or event streams."""

from __future__ import annotations

import posixpath

from multigenz.config import Policy
from multigenz.contracts.paths import path_matches
from multigenz.contracts.result import CheckResult
from multigenz.contracts.task import TaskSpec
from multigenz.core.redaction import Redactor, contains_secret
from multigenz.execution.git import Git, RawChange


class DiffValidator:
    """Path ownership, diff hygiene, artifact presence and credential checks on a commit range."""

    def __init__(self, git: Git, policy: Policy, redactor: Redactor | None = None):
        self.git = git
        self.policy = policy
        self.redactor = redactor or Redactor()

    def validate(self, task: TaskSpec, base: str, head: str, *, allow_empty: bool = False
                 ) -> tuple[list[CheckResult], list[str]]:
        checks: list[CheckResult] = []
        git, vcfg = self.git, self.policy.validation

        if git.rev_parse(head) is None:
            return [CheckResult(name="commit_exists", passed=False, detail=f"{head} is not a commit")], []
        checks.append(CheckResult(name="commit_exists", passed=True))
        if not git.is_ancestor(base, head):
            checks.append(CheckResult(name="descends_from_base", passed=False,
                                      detail="result does not descend from the task base commit"))
            return checks, []
        checks.append(CheckResult(name="descends_from_base", passed=True))

        changes = git.diff_raw(base, head)
        paths = sorted({c.path for c in changes})
        if not changes and not allow_empty:
            checks.append(CheckResult(name="non_empty_diff", passed=False,
                                      detail="agent produced no changes"))
        else:
            checks.append(CheckResult(name="non_empty_diff", passed=True))
        checks.append(CheckResult(
            name="max_changed_files", passed=len(paths) <= vcfg.max_changed_files,
            detail=f"{len(paths)} files changed (limit {vcfg.max_changed_files})"))

        writable = task.writable_paths
        outside = [c.path for c in changes if not path_matches(c.path, writable)]
        checks.append(CheckResult(name="allowed_paths", passed=not outside,
                                  detail=f"changes outside allowed paths: {outside[:10]}" if outside else ""))
        forbidden = [c.path for c in changes if self._forbidden(c.path)]
        checks.append(CheckResult(name="forbidden_paths", passed=not forbidden,
                                  detail=f"forbidden paths touched: {forbidden[:10]}" if forbidden else ""))
        checks.append(self._check_modes(changes))
        checks.append(self._check_sizes(changes))
        if self.policy.validation.require_policy_check:
            checks.append(self._check_secrets(base, head))
        missing = [a for a in task.required_artifacts if not git.path_exists_in(head, a)]
        checks.append(CheckResult(name="required_artifacts", passed=not missing,
                                  detail=f"missing required artifacts: {missing}" if missing else ""))
        return checks, paths

    def _forbidden(self, path: str) -> bool:
        f = self.policy.validation.forbidden_paths
        return path_matches(path, f) or any(path == x.rstrip("/") for x in f) or \
            ".git" in path.lower().split("/")

    def _check_modes(self, changes: list[RawChange]) -> CheckResult:
        bad: list[str] = []
        for c in changes:
            if c.status == "D":
                continue
            if c.new_mode == "160000":
                bad.append(f"{c.path}: submodule/gitlink (embedded repository)")
            elif c.new_mode == "120000":
                target = self.git.read_blob(c.new_sha, 4096).decode("utf-8", "replace")
                if target.startswith("/") or posixpath.normpath(
                        posixpath.join(posixpath.dirname(c.path), target)).startswith(".."):
                    bad.append(f"{c.path}: symlink escapes repository ({target!r})")
            elif c.new_mode not in ("100644", "100755"):
                bad.append(f"{c.path}: unsupported file mode {c.new_mode}")
        return CheckResult(name="file_modes", passed=not bad, detail="; ".join(bad[:10]))

    def _check_sizes(self, changes: list[RawChange]) -> CheckResult:
        shas = [c.new_sha for c in changes if c.status != "D" and c.new_mode in ("100644", "100755")]
        sizes = self.git.blob_sizes(shas)
        limit = self.policy.validation.max_file_bytes
        big = [c.path for c in changes if sizes.get(c.new_sha, 0) > limit]
        return CheckResult(name="max_file_bytes", passed=not big,
                           detail=f"files over {limit} bytes: {big[:10]}" if big else "")

    def _check_secrets(self, base: str, head: str) -> CheckResult:
        hits = []
        for path, line in self.git.added_lines(base, head):
            why = contains_secret(line, self.redactor)
            if why:
                hits.append(f"{path}: {why}")
        return CheckResult(name="no_credentials_added", passed=not hits, detail="; ".join(hits[:5]))
