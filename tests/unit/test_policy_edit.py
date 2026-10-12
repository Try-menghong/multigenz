import os
import subprocess
import sys

import pytest

from multigenz.config import load_policy
from multigenz.errors import ConfigError
from multigenz.policy_edit import save_policy_edits, set_enabled_text, set_roles_text
from multigenz.presets import init_policy

FLOW = """version: 1   # top comment
agents:
    a: {adapter: cli, command: x, capabilities: [coding]}   # flow style, 4-space indent
    b:
        adapter: cli
        command: y
        # a comment inside b
        enabled: true
roles: {planner: [a]}
execution: {isolation: userns}
# trailing comment that belongs to nothing
"""


def cli(*args, policy):
    return subprocess.run([sys.executable, "-m", "multigenz", *args], capture_output=True, text=True,
                          env={**os.environ, "MULTIGENZ_POLICY": str(policy)})


def test_roles_block_replaced_or_inserted_and_comments_kept(tmp_path):
    out = set_roles_text(FLOW, {"planner": ["b", "a"], "worker": ["a"]})
    assert out.count("roles:") == 1 and "# top comment" in out and "# a comment inside b" in out
    assert "# trailing comment that belongs to nothing" in out
    assert out.index("roles:") < out.index("execution:")
    again = set_roles_text(out, {"worker": ["b"]})
    assert again.count("roles:") == 1 and again.count("Who plays which part") == 1   # no duplicated header
    no_roles = "version: 1\nagents:\n  a: {adapter: cli, command: x}\nexecution: {isolation: userns}\n"
    ins = set_roles_text(no_roles, {"planner": ["a"]})
    assert ins.index("agents:") < ins.index("roles:") < ins.index("execution:")


def test_enabled_flag_in_flow_and_block_styles(tmp_path):
    out = set_enabled_text(FLOW, "a", False)
    assert "a: {enabled: false, adapter: cli" in out
    out = set_enabled_text(out, "b", False)
    assert "        enabled: false" in out and "enabled: true" not in out
    plain = "agents:\n  c:\n    adapter: cli\n    command: z\n"
    assert set_enabled_text(plain, "c", False) == "agents:\n  c:\n    enabled: false\n    adapter: cli\n    command: z\n"
    with pytest.raises(ConfigError, match="not found"):
        set_enabled_text(plain, "ghost", True)


def test_save_validates_verifies_and_keeps_a_backup(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(FLOW)
    pol, backup = save_policy_edits(f, roles={"planner": ["b"], "worker": ["a", "b"]}, enabled={"a": False})
    assert pol.roles.worker == ["a", "b"] and not pol.agents["a"].enabled and backup.read_text() == FLOW
    assert load_policy(f).roles.planner == ["b"]
    before = f.read_text()
    with pytest.raises(ConfigError, match="unknown agent"):
        save_policy_edits(f, roles={"worker": ["ghost"]})
    assert f.read_text() == before and not (tmp_path / "p.yaml.tmp").exists()


def test_roles_cli_set_clear_and_agents_enable_disable(tmp_path):
    f = tmp_path / "p.yaml"
    init_policy(f, agents=["claude", "opencode", "cline"])
    r = cli("roles", "set", "worker", "cline", "opencode", policy=f)
    assert r.returncode == 0 and "worker    cline -> opencode" in r.stdout
    assert "conflict  same as the workers: cline -> opencode" in r.stdout
    bad = cli("roles", "set", "worker", "ghost", policy=f)
    assert bad.returncode == 2 and "unknown agent 'ghost'" in bad.stderr and "pydantic" not in bad.stderr
    assert cli("roles", "set", "boss", "cline", policy=f).returncode == 2
    assert cli("agents", "disable", "cline", policy=f).returncode == 0
    shown = cli("roles", policy=f).stdout
    assert "(disabled in policy: cline)" in shown
    assert cli("roles", "clear", "all", policy=f).returncode == 0
    p = load_policy(f)
    assert p.roles.worker == [] and not p.agents["cline"].enabled
    assert cli("agents", "enable", "cline", policy=f).returncode == 0 and load_policy(f).agents["cline"].enabled


def test_set_agent_model_text_block_flow_update_and_remove():
    from multigenz.policy_edit import set_agent_model_text

    text = ("agents:\n"
            "  claude:\n"
            "    adapter: cli   # the Claude Code CLI\n"
            "    command: claude\n"
            "  oc: {adapter: cli, command: opencode}\n"
            "roles:\n"
            "  worker: [claude]\n")
    t1 = set_agent_model_text(text, "claude", "opus")
    assert '  claude:\n    model: "opus"\n    adapter: cli   # the Claude Code CLI\n' in t1
    t2 = set_agent_model_text(t1, "claude", "sonnet")
    assert 'model: "sonnet"' in t2 and "opus" not in t2
    t3 = set_agent_model_text(t2, "oc", "opencode/big-pickle")
    assert '  oc: {model: "opencode/big-pickle", adapter: cli, command: opencode}\n' in t3
    t4 = set_agent_model_text(set_agent_model_text(t3, "oc", None), "claude", None)
    assert t4 == text
    with pytest.raises(ConfigError):
        set_agent_model_text(text, "nobody", "x")
