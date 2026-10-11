"""Thin, hardened wrapper over the Git CLI.

Safety properties (docs/ARCHITECTURE.md "Git invariants"):
* repo hooks, fsmonitor, external diff drivers and pagers are disabled for every call;
* ref-creating/updating helpers only accept this run's namespaced branches;
* there is deliberately NO reset --hard, force push, branch delete or forced worktree removal;
* inside agent-writable worktrees all commands pass an explicit --git-dir so a tampered
  ``.git`` pointer file cannot redirect the orchestrator to another repository/branch.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import io
from dataclasses import dataclass, field
from pathlib import Path

from multigenz.core.policy import PolicyEngine
from multigenz.errors import GitError

IDENTITY = {"GIT_AUTHOR_NAME": "MultiGenz", "GIT_AUTHOR_EMAIL": "multigenz@localhost",
            "GIT_COMMITTER_NAME": "MultiGenz", "GIT_COMMITTER_EMAIL": "multigenz@localhost"}
HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "gc.auto=0",
          "-c", "protocol.ext.allow=never", "-c", "core.autocrlf=false"]
NULL_SHA = "0" * 40


@dataclass(frozen=True)
class RawChange:
    status: str  # A M D T U ...
    path: str
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str


@dataclass
class MergeTreeResult:
    clean: bool
    tree: str
    conflicts: list[str] = field(default_factory=list)
    output: str = ""


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_PAGER": "cat", "GIT_EDITOR": "true", "GIT_ASKPASS": "true", **IDENTITY}
    if extra:
        env.update(extra)
    return env


class Git:
    def __init__(self, repo: Path, *, run_id: str | None = None, git_bin: str = "git"):
        self.repo = Path(repo)
        self.run_id = run_id
        self.git_bin = git_bin

    # -- low level -------------------------------------------------------------------
    def run(self, *args: str, check: bool = True, input: bytes | str | None = None,
            git_dir: Path | None = None, work_tree: Path | None = None, cwd: Path | None = None,
            env: dict[str, str] | None = None, text: bool = True, timeout: float = 120.0):
        cmd = [self.git_bin, *HARDEN]
        if git_dir is not None:
            cmd += ["--git-dir", str(git_dir)]
            if work_tree is not None:
                cmd += ["--work-tree", str(work_tree)]
            run_cwd = work_tree or cwd or self.repo
        else:
            run_cwd = cwd or self.repo
        cmd += list(args)
        data = input.encode() if isinstance(input, str) else input
        proc = subprocess.run(cmd, cwd=str(run_cwd), input=data, capture_output=True, env=_env(env),
                              timeout=timeout, check=False)
        out = proc.stdout.decode("utf-8", "replace") if text else proc.stdout
        err = proc.stderr.decode("utf-8", "replace")
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])} failed (rc={proc.returncode}): {err.strip()[:500]}",
                           returncode=proc.returncode, stderr=err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def out(self, *args: str, **kw) -> str:
        return self.run(*args, **kw).stdout.strip()

    # -- refs ------------------------------------------------------------------------
    def rev_parse(self, ref: str, **kw) -> str | None:
        r = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False, **kw)
        return r.stdout.strip() or None if r.returncode == 0 else None

    def ref_exists(self, ref: str) -> bool:
        return self.run("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0

    def current_branch(self) -> str | None:
        r = self.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return r.stdout.strip() or None if r.returncode == 0 else None

    def head(self) -> str:
        sha = self.rev_parse("HEAD")
        if sha is None:
            raise GitError("repository has no commits")
        return sha

    def toplevel(self) -> Path:
        return Path(self.out("rev-parse", "--show-toplevel")).resolve()

    def common_dir(self) -> Path:
        return Path(self.out("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()

    def repo_identity(self) -> str:
        import hashlib
        roots = self.out("rev-list", "--max-parents=0", "HEAD").split()
        h = hashlib.sha256(f"{self.common_dir()}|{','.join(sorted(roots))}".encode()).hexdigest()
        return h[:32]

    def status_porcelain(self) -> str:
        return self.run("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout

    def is_ancestor(self, a: str, b: str, **kw) -> bool:
        return self.run("merge-base", "--is-ancestor", a, b, check=False, **kw).returncode == 0

    def merge_base(self, a: str, b: str) -> str | None:
        r = self.run("merge-base", a, b, check=False)
        return r.stdout.strip() or None if r.returncode == 0 else None

    def _assert_ref(self, branch: str) -> None:
        if self.run_id is None:
            raise GitError("ref-mutating git operations require a run-scoped Git instance")
        PolicyEngine.assert_run_ref(self.run_id, branch)

    # -- worktrees ---------------------------------------------------------------------
    def worktree_paths(self) -> list[Path]:
        out = self.out("worktree", "list", "--porcelain")
        return [Path(line[9:]).resolve() for line in out.splitlines() if line.startswith("worktree ")]

    def worktree_add(self, path: Path, branch: str, start: str) -> Path:
        """Create a *new* branch + worktree. Refuses to touch anything that already exists."""
        self._assert_ref(branch)
        if self.ref_exists(f"refs/heads/{branch}"):
            raise GitError(f"branch {branch} already exists; refusing to reuse or reset it")
        self.run("worktree", "add", "--quiet", "-b", branch, str(path), start)
        return Path(self.out("rev-parse", "--path-format=absolute", "--git-dir", cwd=path)).resolve()

    # -- commits -----------------------------------------------------------------------
    def stage_all(self, git_dir: Path, work_tree: Path) -> None:
        self.run("add", "-A", "--", ".", git_dir=git_dir, work_tree=work_tree)

    def has_staged(self, git_dir: Path, work_tree: Path) -> bool:
        return self.run("diff", "--cached", "--quiet", "--no-ext-diff", check=False,
                        git_dir=git_dir, work_tree=work_tree).returncode == 1

    def commit(self, git_dir: Path, work_tree: Path, message: str, *, allow_empty: bool = False) -> str:
        args = ["commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        self.run(*args, git_dir=git_dir, work_tree=work_tree)
        return self.out("rev-parse", "HEAD", git_dir=git_dir, work_tree=work_tree)

    def commit_tree(self, tree: str, parents: list[str], message: str) -> str:
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        args += ["-m", message]
        return self.out(*args)

    def merge_tree(self, ours: str, theirs: str) -> MergeTreeResult:
        r = self.run("merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs, check=False)
        lines = r.stdout.splitlines()
        if r.returncode not in (0, 1) or not lines:
            raise GitError(f"git merge-tree failed (rc={r.returncode}): {r.stderr.strip()[:300]}",
                           returncode=r.returncode, stderr=r.stderr)
        tree = lines[0].strip()
        conflicts: list[str] = []
        if r.returncode == 1:
            for line in lines[1:]:
                if line.strip() == "":
                    break
                conflicts.append(line.strip())
        return MergeTreeResult(clean=r.returncode == 0, tree=tree, conflicts=conflicts, output=r.stdout)

    def find_commit_with_trailer(self, ref: str, key: str, value: str) -> str | None:
        """First commit reachable from ``ref`` whose message has the exact trailer ``key: value``."""
        r = self.run("log", ref, "--format=%H", "-n", "1", "--fixed-strings", f"--grep={key}: {value}", check=False)
        return r.stdout.strip() or None if r.returncode == 0 else None

    def tree_of(self, commit: str) -> str:
        return self.out("rev-parse", f"{commit}^{{tree}}")

    def ff_merge(self, git_dir: Path, work_tree: Path, sha: str) -> None:
        """Fast-forward a checked-out run branch (never rewrites history)."""
        self.run("merge", "--ff-only", "--quiet", sha, git_dir=git_dir, work_tree=work_tree)

    def merge_no_commit(self, git_dir: Path, work_tree: Path, other: str) -> bool:
        """Start a merge leaving conflicts in the (run-owned) worktree. True when clean."""
        r = self.run("merge", "--no-commit", "--no-ff", "--no-verify", other, check=False,
                     git_dir=git_dir, work_tree=work_tree)
        return r.returncode == 0

    def merge_abort(self, git_dir: Path, work_tree: Path) -> None:
        self.run("merge", "--abort", check=False, git_dir=git_dir, work_tree=work_tree)

    def unmerged_paths(self, git_dir: Path, work_tree: Path) -> list[str]:
        out = self.run("diff", "--name-only", "--diff-filter=U", "-z", git_dir=git_dir, work_tree=work_tree).stdout
        return [p for p in out.split("\0") if p]

    def merge_in_progress(self, git_dir: Path) -> bool:
        return (git_dir / "MERGE_HEAD").exists()

    # -- inspection -------------------------------------------------------------------
    def diff_raw(self, base: str, head: str) -> list[RawChange]:
        out = self.run("diff", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-ext-diff", "--no-textconv",
                       "--no-color", base, head).stdout
        parts = out.split("\0")
        changes: list[RawChange] = []
        i = 0
        while i < len(parts) - 1:
            meta = parts[i]
            if not meta.startswith(":"):
                i += 1
                continue
            m = meta[1:].split()
            changes.append(RawChange(status=m[4][0], path=parts[i + 1], old_mode=m[0], new_mode=m[1],
                                     old_sha=m[2], new_sha=m[3]))
            i += 2
        return changes

    def diff_lines(self, base: str, head: str, limit_bytes: int = 2_000_000) -> list[tuple[str, str, str]]:
        """(path, '+'|'-', text) for every added/removed line."""
        out = self.run("diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv",
                       "--no-renames", base, head).stdout[:limit_bytes]
        res: list[tuple[str, str, str]] = []
        path = ""
        for line in out.splitlines():
            if line.startswith("+++ "):
                path = line[4:].removeprefix("b/")
            elif line.startswith("--- "):
                if line[4:] != "/dev/null":
                    path = line[4:].removeprefix("a/")
            elif line.startswith("+") :
                res.append((path, "+", line[1:]))
            elif line.startswith("-"):
                res.append((path, "-", line[1:]))
        return res

    def added_lines(self, base: str, head: str, limit_bytes: int = 2_000_000) -> list[tuple[str, str]]:
        return [(p, t) for p, k, t in self.diff_lines(base, head, limit_bytes) if k == "+"]

    def diff_text(self, base: str, head: str, limit: int = 60_000) -> str:
        return self.run("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--stat", "-p", base, head).stdout[:limit]

    def blob_sizes(self, shas: list[str]) -> dict[str, int]:
        if not shas:
            return {}
        out = self.run("cat-file", "--batch-check=%(objectname) %(objectsize)", input="\n".join(shas) + "\n").stdout
        res: dict[str, int] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                res[parts[0]] = int(parts[1])
        return res

    def read_blob(self, sha: str, limit: int = 1_000_000) -> bytes:
        return self.run("cat-file", "blob", sha, text=False).stdout[:limit]

    def path_exists_in(self, commit: str, path: str) -> bool:
        return self.run("cat-file", "-e", f"{commit}:{path}", check=False).returncode == 0

    def ls_tree(self, commit: str) -> list[str]:
        out = self.run("ls-tree", "-r", "-z", "--name-only", commit).stdout
        return [p for p in out.split("\0") if p]

    def export_tree(self, treeish: str, dest: Path) -> None:
        """Materialise a commit/tree into ``dest`` (no .git) for independent test runs."""
        data = self.run("archive", "--format=tar", treeish, text=False).stdout
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(dest, filter="data")
