"""Shared helpers: scripted-agent policies, scenario writers, demo repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from multigenz.config import Policy, parse_policy

SUPPORT = Path(__file__).resolve().parent
SCRIPTED = SUPPORT / "scripted_agent"
REPO_ROOT = SUPPORT.parent.parent


def git(repo: Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=False)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "dev@example.com")
    git(path, "config", "user.name", "Dev")
    files = files if files is not None else {
        "README.md": "# demo\n",
        "src/app.py": "VALUE = 1\n",
        "tests/test_app.py": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                             "    def test_ok(self):\n        self.assertTrue(True)\n",
    }
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def write_scenario(directory: Path, task_id: str, spec: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{task_id}.json").write_text(json.dumps(spec))


def scripted_invocation(scenario: Path, agent: str, *, unattended: bool = True) -> dict:
    return {
        "name": "scripted",
        "argv": ["{command}", "--non-interactive", "--task-file", "{task_file}", "--prompt-file", "{prompt_file}",
                 "--scenario", str(scenario), "--agent-name", agent],
        "stdin": "none",
        "requires_help_tokens": ["--task-file", "--prompt-file", "--scenario"],
        "unattended": unattended,
    }


UNITTEST_PROFILE = {"commands": [{"argv": ["/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests", "-q"],
                                  "timeout_seconds": 120}]}


def pytest_profile(timeout: int = 300) -> dict:
    return {"commands": [{"argv": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          "timeout_seconds": timeout}],
            "readonly_paths": [sys.prefix, sys.base_prefix]}


def scripted_policy(scenario: Path, *, isolation: str = "userns", agents: dict[str, list[str]] | None = None,
                    profiles: dict | None = None, orchestrator: dict | None = None,
                    validation: dict | None = None, execution: dict | None = None,
                    unattended: bool | None = None, agent_extra: dict | None = None) -> Policy:
    agents = agents or {"alpha": ["coding", "testing", "review", "architecture"]}
    if unattended is None:
        unattended = isolation != "none"
    ag = {}
    for name, caps in agents.items():
        ag[name] = {"adapter": "cli", "command": str(SCRIPTED), "capabilities": caps,
                    "invocations": [scripted_invocation(scenario, name, unattended=unattended)],
                    **((agent_extra or {}).get(name, {}))}
    profiles = profiles if profiles is not None else {"unit": UNITTEST_PROFILE}
    ex = {"isolation": isolation, "network": "none", "readonly_paths": [str(SUPPORT), str(scenario)]}
    if isolation == "none":
        ex["allow_unsandboxed"] = True
    ex.update(execution or {})
    val = {"require_tests": True, "default_profile": next(iter(profiles)) if profiles else None,
           "integration_profile": next(iter(profiles)) if profiles else None, "profiles": profiles}
    val.update(validation or {})
    orch = {"max_parallel_agents": 3, "max_task_attempts": 2, "task_timeout_seconds": 60, "kill_grace_seconds": 1.0,
            "backoff_base_seconds": 0.05, "backoff_cap_seconds": 0.2}
    orch.update(orchestrator or {})
    return parse_policy({"version": 1, "orchestrator": orch, "agents": ag, "execution": ex, "validation": val,
                         "git": {"auto_integrate": True, "auto_push": False, "protect_default_branch": True}})


# ---------------------------------------------------------------------------------------------
from multigenz.contracts.task import PlanDocument, TaskSpec  # noqa: E402


def T(task_id: str, paths: list[str] | str, deps: list[str] | None = None, caps: list[str] | None = None,
      **kw) -> TaskSpec:
    paths = [paths] if isinstance(paths, str) else paths
    return TaskSpec(task_id=task_id, title=f"Task {task_id}", objective=f"Implement {task_id}",
                    allowed_paths=paths, dependencies=deps or [], required_capabilities=set(caps or []), **kw)


def plan_of(*tasks: TaskSpec) -> PlanDocument:
    return PlanDocument(tasks=list(tasks))


def writes(path: str, content: str, **extra) -> dict:
    return {"attempts": {"default": [{"write": {path: content}, **extra}]}}


def overlap_seconds(a: dict, b: dict) -> float:
    from datetime import datetime

    def t(x):
        return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()

    return min(t(a["finished_at"]), t(b["finished_at"])) - max(t(a["started_at"]), t(b["started_at"]))


def max_concurrency(attempts: list[dict]) -> int:
    from datetime import datetime

    def t(x):
        return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()

    edges = []
    for a in attempts:
        edges += [(t(a["started_at"]), 1), (t(a["finished_at"]), -1)]
    cur = best = 0
    for _, d in sorted(edges):
        cur += d
        best = max(best, cur)
    return best
