"""Real coding-agent CLIs driven end to end by MultiGenz, offline.

Each test runs the actual opencode / Cline / Codex binary inside the userns sandbox, configured exactly like a user's
login would be (a config file mounted read-only under the sandbox home), but pointed at tests/support/mock_llm.py
instead of a real provider. `multigenz run` then has to plan, run the coding attempt, validate the commit with an
administrator profile and integrate it. No account, API key or network beyond 127.0.0.1 is needed.

Binaries are looked up on $MULTIGENZ_TEST_CLI_PATH (default: $PATH plus /opt/agentclis/bin); missing ones are skipped.
Codex runs with its OWN sandbox (bubblewrap, workspace-write) nested inside MultiGenz's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import mock_llm

pytestmark = [pytest.mark.cli_agents, pytest.mark.needs_userns, pytest.mark.e2e]

SEARCH = os.environ.get("MULTIGENZ_TEST_CLI_PATH") or os.environ.get("PATH", "") + ":/opt/agentclis/bin"


def which(name: str) -> str | None:
    return shutil.which(name, path=SEARCH)


@pytest.fixture
def mock(tmp_path):
    log = tmp_path / "mock.jsonl"
    srv = mock_llm.serve(0, log)
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", log
    srv.shutdown()


def provider_files(tmp_path: Path, base_url: str) -> dict[str, tuple[str, str]]:
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "opencode.json").write_text(json.dumps({
        "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "Mock",
                              "options": {"baseURL": base_url, "apiKey": "dummy"},
                              "models": {"mock-model": {"name": "Mock"}}}},
        "model": "mock/mock-model"}))
    if which("cline"):  # written by `cline auth` itself, exactly as a user's login would be
        home = tmp_path / "cline-home"
        home.mkdir()
        subprocess.run([which("cline"), "auth", "-p", "openai", "-k", "dummy", "-m", "mock-model", "-b", base_url],
                       env={"PATH": os.environ["PATH"] + ":" + SEARCH, "HOME": str(home)}, check=True,
                       capture_output=True, timeout=120)
        shutil.copy(home / ".cline" / "data" / "settings" / "providers.json", cfg / "cline.json")
    (cfg / "codex.toml").write_text(
        f'model = "mock-model"\nmodel_provider = "mock"\n[model_providers.mock]\nname = "mock"\n'
        f'base_url = "{base_url}"\nwire_api = "responses"\nenv_key = "MOCK_API_KEY"\n')
    return {
        "opencode": (str(cfg / "opencode.json"), "/home/agent/.config/opencode/opencode.json"),
        "cline": (str(cfg / "cline.json"), "/home/agent/.cline/data/settings/providers.json"),
        "codex": (str(cfg / "codex.toml"), "/home/agent/.codex/config.toml"),
    }


def write_policy(tmp_path: Path, agents: dict[str, list[str]], files, extra: dict | None = None) -> Path:
    pol = {
        "version": 1,
        "orchestrator": {"mode": "autonomous", "max_parallel_agents": 1, "max_task_attempts": 2,
                         "task_timeout_seconds": 240},
        "agents": {},
        "execution": {"isolation": "userns", "network": "host"},
        "validation": {"require_tests": True, "default_profile": "exists", "integration_profile": "exists",
                       "profiles": {"exists": {"commands": [{"argv": ["/usr/bin/test", "-f", mock_llm.FILE_NAME],
                                                             "timeout_seconds": 30}]}}},
    }
    for name, caps in agents.items():
        src, dst = files[name]
        entry = {"adapter": "cli", "command": which(name), "capabilities": caps,
                 "credentials": {"files": [{"src": src, "dst": dst}]}}
        if name == "codex":
            entry["credentials"]["env"] = ["MOCK_API_KEY"]
        pol["agents"][name] = entry | (extra or {}).get(name, {})
    path = tmp_path / "policy.yaml"
    path.write_text(json.dumps(pol))  # JSON is YAML
    return path


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def run_mg(tmp_path: Path, policy: Path, repo: Path, *args: str, env: dict | None = None):
    e = {**os.environ, "MULTIGENZ_POLICY": str(policy), "MULTIGENZ_HOME": str(tmp_path / "state"), **(env or {})}
    return subprocess.run([sys.executable, "-m", "multigenz", *args], cwd=repo, capture_output=True, text=True,
                          env=e, timeout=600)


def assert_integrated(tmp_path, policy, repo, out: str, provider: str):
    assert ": SUCCEEDED" in out, out
    assert f"provider={provider}" in out, out
    rid = out.split("run ", 1)[1].split(":", 1)[0]
    branch = f"multigenz/run-{rid}/integration"
    shown = subprocess.run(["git", "show", f"{branch}:{mock_llm.FILE_NAME}"], cwd=repo, capture_output=True, text=True)
    assert shown.returncode == 0 and mock_llm.FILE_TEXT in shown.stdout
    # the user's checkout is untouched and nothing was pushed
    assert not (repo / mock_llm.FILE_NAME).exists()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == ""


@pytest.mark.parametrize("cli", ["opencode", "cline"])
def test_single_cli_plans_codes_and_integrates(tmp_path, mock, cli):
    if not which(cli):
        pytest.skip(f"{cli} not installed")
    base_url, log = mock
    files = provider_files(tmp_path, base_url)
    policy = write_policy(tmp_path, {cli: ["architecture", "coding"]}, files)
    repo = make_repo(tmp_path)
    listed = run_mg(tmp_path, policy, repo, "agents", "list")
    assert "enabled" in listed.stdout, listed.stdout + listed.stderr
    r = run_mg(tmp_path, policy, repo, "run", f"Create the file {mock_llm.FILE_NAME}", "--repo", str(repo))
    assert_integrated(tmp_path, policy, repo, r.stdout, cli)
    assert log.read_text().count("\n") >= 2  # the CLI really talked to the (mock) model


def test_different_clis_plan_and_code_in_one_run(tmp_path, mock):
    """Multi-agent coordination across vendors: Cline plans (architecture), opencode writes the code."""
    if not (which("opencode") and which("cline")):
        pytest.skip("needs opencode and cline")
    base_url, _ = mock
    files = provider_files(tmp_path, base_url)
    policy = write_policy(tmp_path, {"cline": ["architecture"], "opencode": ["coding"]}, files)
    repo = make_repo(tmp_path)
    r = run_mg(tmp_path, policy, repo, "run", f"Create the file {mock_llm.FILE_NAME}", "--repo", str(repo))
    assert_integrated(tmp_path, policy, repo, r.stdout, "opencode")
    events = run_mg(tmp_path, policy, repo, "runs", "events", r.stdout.split("run ", 1)[1].split(":", 1)[0]).stdout
    assert '"kind": "plan.accepted"' in events and '"agent": "cline"' in events


def test_codex_plans_codes_and_integrates_with_its_own_sandbox(tmp_path, mock):
    """Codex plans (exec --sandbox read-only) and codes (exec --sandbox workspace-write), its bubblewrap nested inside
    the MultiGenz sandbox; its shell command must really succeed (exit 0), not merely be attempted."""
    if not which("codex"):
        pytest.skip("codex not installed")
    base_url, log = mock
    files = provider_files(tmp_path, base_url)
    policy = write_policy(tmp_path, {"codex": ["architecture", "coding"]}, files)
    repo = make_repo(tmp_path)
    r = run_mg(tmp_path, policy, repo, "run", f"Create the file {mock_llm.FILE_NAME}", "--repo", str(repo),
               env={"MOCK_API_KEY": "dummy"})
    assert_integrated(tmp_path, policy, repo, r.stdout, "codex")
    outputs = [i.get("output", "") for row in map(json.loads, log.read_text().splitlines())
               for i in row["body"].get("input", []) if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert outputs and all("exited with code 0" in o for o in outputs), outputs
    assert not any("bwrap" in o for o in outputs)
