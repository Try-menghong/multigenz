import os
import subprocess
import sys

import pytest

from multigenz.adapters.registry import build_adapters, cli_class
from multigenz.adapters.opencode import OpencodeAdapter
from multigenz.config import load_policy
from multigenz.execution.sandbox import runtime_mounts
from multigenz.presets import PRESETS, add_preset, init_policy, insert_agent, render_agent


def cli(*args, env=None):
    return subprocess.run([sys.executable, "-m", "multigenz", *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def fake_bin(d, name):
    p = d / name
    p.write_text("#!/bin/sh\necho help\n")
    p.chmod(0o755)
    return p


def test_init_writes_a_valid_policy_with_detected_agents_and_skips_codex(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    for n in ("opencode", "cline", "codex"):
        fake_bin(bins, n)
    pol = tmp_path / "cfg" / "policy.yaml"
    r = cli("init", "--policy", str(pol), env={"PATH": f"{bins}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    p = load_policy(pol)
    assert set(p.agents) == {"opencode", "cline"} and p.execution.isolation == "userns"
    assert "skipped: codex" in r.stdout
    assert oct(pol.parent.stat().st_mode & 0o777) == "0o700"
    assert cli("init", "--policy", str(pol)).returncode == 2           # never overwrites silently
    r = cli("init", "--policy", str(pol), "--overwrite", "-a", "claude", "-a", "codex", "--trust-outer-sandbox")
    assert r.returncode == 0, r.stderr
    assert set(load_policy(pol).agents) == {"claude", "codex"} and (tmp_path / "cfg" / "policy.yaml.bak").exists()


def test_agents_add_cli_validates_refuses_codex_without_opt_in_and_duplicates(tmp_path):
    pol = tmp_path / "p.yaml"
    init_policy(pol, agents=[])
    assert load_policy(pol).agents == {}
    r = cli("agents", "add", "codex", "--policy", str(pol))
    assert r.returncode == 2 and "--trust-outer-sandbox" in r.stderr and load_policy(pol).agents == {}
    assert cli("agents", "add", "codex", "--policy", str(pol), "--trust-outer-sandbox").returncode == 0
    assert cli("agents", "add", "opencode", "--policy", str(pol), "--name", "oc2", "--command", "/x/opencode").returncode == 0
    dup = cli("agents", "add", "opencode", "--policy", str(pol), "--name", "oc2")
    assert dup.returncode == 2 and "already exists" in dup.stderr
    p = load_policy(pol)
    assert [i.name for i in p.agents["codex"].invocations] == ["exec-outer-sandbox", "exec-outer-sandbox-read"]
    assert p.agents["oc2"].command == "/x/opencode" and p.agents["oc2"].invocations == []
    assert cli_class("oc2", "/x/opencode") is OpencodeAdapter                # built-ins chosen by command name
    assert cli("agents", "add", "nope", "--policy", str(pol)).returncode == 2


def test_preset_credentials_are_named_files_mounted_under_the_sandbox_home(tmp_path):
    pol = tmp_path / "p.yaml"
    init_policy(pol, agents=["opencode", "cline", "claude"])
    p = load_policy(pol)
    files = {f.src: f.dst for f in p.agents["opencode"].credentials.files}
    assert files["~/.local/share/opencode/auth.json"] == "/home/agent/.local/share/opencode/auth.json"
    assert p.agents["cline"].credentials.files[0].dst == "/home/agent/.cline/data/settings/providers.json"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in p.agents["claude"].credentials.env
    assert p.agents["claude"].invocations[0].name == "print-accept-edits-pytest"


def test_add_preset_restores_the_original_when_the_result_is_invalid(tmp_path):
    pol = tmp_path / "p.yaml"
    original = "version: 1\nagents:\n  x: {adapter: cli, command: x}\nbogus_top_level_key: 1\n"
    pol.write_text(original)
    with pytest.raises(Exception):
        add_preset(pol, "cline")
    assert pol.read_text() == original


def test_insert_agent_handles_missing_and_empty_agents_mappings():
    block = render_agent(PRESETS["cline"])
    assert "agents:\n  cline:" in insert_agent("version: 1\n", block, "cline")
    out = insert_agent("version: 1\nagents: {}\nexecution: {isolation: userns}\n", block, "cline")
    assert out.index("  cline:") < out.index("execution:")


def test_runtime_mounts_cover_node_modules_root_and_interpreter_dir_only(tmp_path):
    nm = tmp_path / "prefix" / "lib" / "node_modules" / "@scope" / "tool" / "bin"
    nm.mkdir(parents=True)
    script = nm / "tool.js"
    script.write_text("#!/usr/bin/env fakenode\nconsole.log(1)\n")
    nodedir = tmp_path / "home" / ".local" / "bin"
    nodedir.mkdir(parents=True)
    fake_bin(nodedir, "fakenode")
    dirs, path_dirs = runtime_mounts(str(script), host_path=str(nodedir))
    assert str(tmp_path / "prefix" / "lib" / "node_modules") in dirs
    assert str(nodedir) in dirs and str(nodedir) in path_dirs
    assert str(tmp_path / "home" / ".local") not in dirs                    # never a whole prefix
    assert runtime_mounts("/usr/bin/env") == ([], [])


def test_registry_builds_preset_adapters(tmp_path):
    pol = tmp_path / "p.yaml"
    init_policy(pol, agents=["opencode", "cline"])
    ads = build_adapters(load_policy(pol))
    assert type(ads["opencode"]).__name__ == "OpencodeAdapter" and type(ads["cline"]).__name__ == "ClineAdapter"
