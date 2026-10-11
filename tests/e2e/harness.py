"""Shared harness for the end-to-end scenarios: demo repo, scripted agents, real CLI invocations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from multigenz.contracts.task import PlanDocument
from tests.support.helpers import git, make_repo, pytest_profile, scripted_policy, write_scenario

FIXTURES = Path(__file__).parent / "fixtures" / "auth_service"
GOAL = "Create a REST API with authentication and tests"

AUTH_PLAN = {
    "schema_version": 1,
    "summary": "FastAPI auth service in three tasks",
    "tasks": [
        {"task_id": "security", "title": "Password hashing and tokens",
         "objective": "Implement app/security.py (PBKDF2 hashing, signed expiring tokens) with unit tests.",
         "allowed_paths": ["app/__init__.py", "app/security.py", "tests/test_security.py"],
         "required_capabilities": ["coding"], "required_artifacts": ["app/security.py"]},
        {"task_id": "api", "title": "FastAPI endpoints",
         "objective": "Implement app/main.py: /register, /login and protected /me using app.security; add API tests.",
         "dependencies": ["security"], "allowed_paths": ["app/main.py", "tests/test_api.py"],
         "required_capabilities": ["coding"], "required_artifacts": ["app/main.py", "tests/test_api.py"]},
        {"task_id": "docs", "title": "Documentation",
         "objective": "Document the endpoints in docs/AUTH.md.", "allowed_paths": ["docs/"],
         "required_capabilities": ["coding"], "required_artifacts": ["docs/AUTH.md"]},
    ],
}


def copy(dst: str, src: str) -> dict:
    return {dst: src}


def files_for(task: str, variant: str = "good") -> list[dict]:
    g, b = "good", variant
    if task == "security":
        return [{"copy_from": {"app/__init__.py": f"{g}/app/__init__.py", "app/security.py": f"{g}/app/security.py",
                               "tests/test_security.py": f"{g}/tests/test_security.py"}}]
    if task == "api":
        return [{"copy_from": {"app/main.py": f"{b}/app/main.py", "tests/test_api.py": f"{g}/tests/test_api.py"}}]
    if task == "docs":
        return [{"copy_from": {"docs/AUTH.md": f"{g}/docs/AUTH.md"}}]
    raise KeyError(task)


def build_scenario(scenario: Path, *, api_attempts: dict | None = None, extra: dict | None = None) -> None:
    shutil.copytree(FIXTURES, scenario / "files", dirs_exist_ok=True)
    write_scenario(scenario, "plan", {"modes": {"plan": {"default": [
        {"print": ["Planning..."], "emit_text": "```json\n" + json.dumps(AUTH_PLAN) + "\n```"}]}}})
    for t in ("security", "docs"):
        write_scenario(scenario, t, {"attempts": {"default": files_for(t)}})
    write_scenario(scenario, "api", {"attempts": api_attempts or {"default": files_for("api")}})
    for name, spec in (extra or {}).items():
        write_scenario(scenario, name, spec)


def e2e_policy(scenario: Path, **kw):
    return scripted_policy(
        scenario, isolation="userns",
        agents={"architect": ["architecture"], "coder1": ["coding", "testing"], "coder2": ["coding", "testing"]},
        profiles={"pytest": pytest_profile(300)}, **kw)


def demo_repo(path: Path) -> Path:
    return make_repo(path, {"README.md": "# demo-api\n", "conftest.py": "", ".gitignore": "__pycache__/\n*.pyc\n",
                            "tests/test_smoke.py": "def test_baseline_suite_exists():\n    assert True\n"})


def write_policy(path: Path, pol) -> Path:
    path.write_text(yaml.safe_dump(json.loads(pol.model_dump_json())))
    return path


def cli(*args: str, env: dict | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "multigenz", *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=timeout)


def pytest_on_commit(repo: Path, commit: str) -> tuple[bool, str]:
    """Independently test a commit on the host (outside any orchestrator machinery)."""
    with tempfile.TemporaryDirectory() as td:
        data = subprocess.run(["git", "archive", commit], cwd=repo, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", td], input=data, check=True)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=td,
                           capture_output=True, text=True)
        return r.returncode == 0, r.stdout[-600:]
