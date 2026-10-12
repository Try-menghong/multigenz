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

SEARCH = os.environ.get("MULTIGENZ_TEST_CLI_PATH") or os.environ.get("PATH", "") + ":/opt/agentclis/bin:/opt/hermes-venv/bin"


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
    (cfg / "hermes.yaml").write_text(f"model:\n  provider: custom\n  base_url: {base_url}\n  default: mock-model\n")
    return {
        "hermes": (str(cfg / "hermes.yaml"), "/home/agent/.hermes/config.yaml"),
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
        if name == "hermes":
            entry["credentials"]["env"] = ["OPENAI_API_KEY"]
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
    e = {**os.environ, "MULTIGENZ_POLICY": str(policy), "MULTIGENZ_HOME": str(tmp_path / "state"),
         "OPENAI_API_KEY": "dummy", **(env or {})}
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


@pytest.mark.parametrize("cli", ["opencode", "cline", "hermes"])
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


def test_roles_pick_planner_worker_and_reviewer_across_vendors(tmp_path, mock):
    """Every agent could do everything; the roles alone decide: Cline plans, Codex codes, opencode reviews."""
    if not (which("opencode") and which("cline") and which("codex")):
        pytest.skip("needs codex, opencode and cline")
    base_url, _ = mock
    files = provider_files(tmp_path, base_url)
    full = ["architecture", "coding", "testing", "review"]
    policy = write_policy(tmp_path, {"opencode": full, "cline": full, "codex": full}, files)
    pol = json.loads(policy.read_text())
    pol["roles"] = {"planner": ["cline"], "worker": ["codex"], "reviewer": ["opencode"], "conflict": []}
    policy.write_text(json.dumps(pol))
    repo = make_repo(tmp_path)
    shown = run_mg(tmp_path, policy, repo, "roles").stdout
    assert "planner   cline" in shown and "worker    codex" in shown and "reviewer  opencode" in shown
    r = run_mg(tmp_path, policy, repo, "run", f"Create the file {mock_llm.FILE_NAME}", "--repo", str(repo),
               env={"MOCK_API_KEY": "dummy"})
    assert_integrated(tmp_path, policy, repo, r.stdout, "codex")
    rid = r.stdout.split("run ", 1)[1].split(":", 1)[0]
    events = [json.loads(line) for line in run_mg(tmp_path, policy, repo, "runs", "events", rid).stdout.splitlines()]
    assert next(e for e in events if e["kind"] == "plan.accepted")["payload"]["agent"] == "cline"
    checks = [c for e in events if e["kind"] == "validation.result" for c in e["payload"]["checks"]]
    assert {"name": "review:agent[opencode]", "passed": True} in [{k: c[k] for k in ("name", "passed")} for c in checks]


def _auto_policy(tmp_path, files, cli, profile_argv):
    policy = write_policy(tmp_path, {cli: ["architecture", "coding", "testing", "review"]}, files)
    pol = json.loads(policy.read_text())
    pol["orchestrator"]["planning"] = "auto"
    pol["validation"]["profiles"]["exists"]["commands"][0]["argv"] = profile_argv
    policy.write_text(json.dumps(pol))
    return policy


def test_auto_planning_small_goal_is_one_agent_run(tmp_path, mock):
    """planning: auto - no planning run; one opencode run does the goal."""
    if not which("opencode"):
        pytest.skip("opencode not installed")
    base_url, log = mock
    files = provider_files(tmp_path, base_url)
    policy = _auto_policy(tmp_path, files, "opencode", ["/usr/bin/test", "-f", mock_llm.FILE_NAME])
    repo = make_repo(tmp_path)
    r = run_mg(tmp_path, policy, repo, "run", f"Create the file {mock_llm.FILE_NAME}", "--repo", str(repo))
    assert_integrated(tmp_path, policy, repo, r.stdout, "opencode")
    assert "agent runs: 0 planning + 1 coding + 0 review" in r.stdout
    assert mock_llm.PLANNER_MARKER not in log.read_text()        # the model never saw a planning prompt


def test_auto_planning_agent_asks_for_subagents(tmp_path, mock, monkeypatch):
    """planning: auto - opencode answers the whole goal with two subtasks; two more opencode runs build them."""
    if not which("opencode"):
        pytest.skip("opencode not installed")
    monkeypatch.setattr(mock_llm, "SPLIT", True)
    base_url, _ = mock
    files = provider_files(tmp_path, base_url)
    policy = _auto_policy(tmp_path, files, "opencode", ["/usr/bin/test", "-d", "."])
    repo = make_repo(tmp_path)
    r = run_mg(tmp_path, policy, repo, "run", "Create part-a.txt and part-b.txt", "--repo", str(repo))
    assert ": SUCCEEDED" in r.stdout, r.stdout + r.stderr
    assert "handed the work to subtasks: part-a, part-b" in r.stdout
    assert "agent runs: 0 planning + 3 coding + 0 review" in r.stdout
    rid = r.stdout.split("run ", 1)[1].split(":", 1)[0]
    branch = f"multigenz/run-{rid}/integration"
    for f in ("part-a.txt", "part-b.txt"):
        shown = subprocess.run(["git", "show", f"{branch}:{f}"], cwd=repo, capture_output=True, text=True)
        assert shown.returncode == 0 and mock_llm.FILE_TEXT in shown.stdout, f


def test_opencode_plugins_from_your_config_dir_load_inside_the_sandbox(tmp_path, mock):
    """Plugins are opencode's own: a local plugin in ~/.config/opencode/plugin runs inside the sandbox."""
    if not which("opencode"):
        pytest.skip("opencode not installed")
    base_url, _ = mock
    files = provider_files(tmp_path, base_url)
    plugin_dir = tmp_path / "opencode-plugins"
    plugin_dir.mkdir()
    (plugin_dir / "marker.js").write_text(
        "export const Marker = async ({ directory }) => {\n"
        "  await Bun.write(`${directory}/plugin-ran.txt`, 'plugin loaded\\n');\n  return {};\n};\n")
    policy = write_policy(tmp_path, {"opencode": ["coding"]}, files)
    pol = json.loads(policy.read_text())
    pol["agents"]["opencode"]["credentials"]["files"].append(
        {"src": str(plugin_dir), "dst": "/home/agent/.config/opencode/plugin"})
    pol["validation"] = {"require_tests": False}
    policy.write_text(json.dumps(pol))
    repo = make_repo(tmp_path)
    r = run_mg(tmp_path, policy, repo, "agents", "run", "opencode", f"Create the file {mock_llm.FILE_NAME}",
               "--repo", str(repo), "--allow", mock_llm.FILE_NAME, "--allow", "plugin-ran.txt")
    assert "VALIDATED" in r.stdout, r.stdout + r.stderr
    branch = next(ln.split()[1] for ln in r.stdout.splitlines() if ln.startswith("branch "))
    shown = subprocess.run(["git", "show", f"{branch}:plugin-ran.txt"], cwd=repo, capture_output=True, text=True)
    assert shown.stdout.strip() == "plugin loaded"
