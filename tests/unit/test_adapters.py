import shutil

import pytest

from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.adapters.litellm_adapter import LiteLLMAdapter
from multigenz.adapters.registry import build_adapters
from multigenz.config import load_policy, parse_policy
from multigenz.contracts.agent import AgentAdapter, CancellationToken, ExecutionContext
from multigenz.contracts.task import TaskSpec
from tests.support.helpers import REPO_ROOT, SCRIPTED, scripted_invocation, scripted_policy


async def test_scripted_invocation_verified_by_help(scenario):
    pol = scripted_policy(scenario)
    ad = build_adapters(pol)["alpha"]
    p = await ad.probe()
    assert p.enabled and p.invocation == "scripted" and "code" in p.modes and p.version == "scripted-agent 1.0"


async def test_unverified_flags_disable_adapter(scenario):
    inv = scripted_invocation(scenario, "alpha")
    inv["requires_help_tokens"] = ["--definitely-not-a-flag"]
    pol = parse_policy({"agents": {"alpha": {"adapter": "cli", "command": str(SCRIPTED), "capabilities": ["coding"],
                                             "invocations": [inv]}}})
    p = await build_adapters(pol)["alpha"].probe()
    assert not p.enabled and "no invocation verified" in p.reason and "unverified" in p.warnings[0]


async def test_unknown_cli_has_no_assumed_flags():
    pol = parse_policy({"agents": {"codex": {"adapter": "cli", "command": str(SCRIPTED), "capabilities": ["coding"]}}})
    p = await build_adapters(pol)["codex"].probe()
    assert not p.enabled and "flags are never assumed" in p.reason


async def test_missing_binary_disabled():
    pol = parse_policy({"agents": {"agy": {"adapter": "cli", "command": "no-such-binary-xyz"}}})
    p = await build_adapters(pol)["agy"].probe()
    assert not p.enabled and "not found" in p.reason


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
async def test_real_claude_help_verifies_candidates():
    pol = load_policy(REPO_ROOT / "config" / "policy.example.yaml")
    p = await build_adapters(pol)["claude"].probe()
    assert p.enabled and {"code", "plan"} <= set(p.modes)


async def test_adapters_implement_contract():
    pol = load_policy(REPO_ROOT / "config" / "policy.example.yaml")
    for ad in build_adapters(pol).values():
        assert isinstance(ad, AgentAdapter)
        assert isinstance(await ad.healthcheck(), bool)


def litellm_ctx(tmp_path, mode="analysis"):
    return ExecutionContext(run_id="r", task_id="t", attempt_id="a1", worktree=tmp_path, sandbox_id="s",
                            sandbox_backend="api", deadline=9e12, cancel=CancellationToken(), log_dir=tmp_path,
                            mode=mode)


async def test_litellm_adapter_with_fake_completion(tmp_path):
    async def fake(**kw):
        assert kw["model"] == "deepseek/deepseek-chat"
        return {"choices": [{"message": {"content": "line1\nline2"}}], "_multigenz_cost": 0.002}

    pol = parse_policy({"agents": {"ds": {"adapter": "litellm", "model": "deepseek/deepseek-chat",
                                          "capabilities": ["analysis"]}}})
    ad = LiteLLMAdapter("ds", pol.agents["ds"], completion=fake)
    t = TaskSpec(task_id="t", title="t", objective="analyse", allowed_paths=["docs/"])
    evs = [e async for e in ad.execute(t, litellm_ctx(tmp_path))]
    assert [e.message for e in evs if e.kind == "stdout"] == ["line1", "line2"]
    assert ad.result("a1").success and ad.output_text("a1") == "line1\nline2"
    assert [e.sequence for e in evs] == list(range(1, len(evs) + 1))


async def test_litellm_cannot_do_code_tasks(tmp_path):
    pol = parse_policy({"agents": {"ds": {"adapter": "litellm", "model": "m", "capabilities": ["analysis"]}}})
    ad = LiteLLMAdapter("ds", pol.agents["ds"], completion=lambda **k: None)
    t = TaskSpec(task_id="t", title="t", objective="x", allowed_paths=["a"])
    [e async for e in ad.execute(t, litellm_ctx(tmp_path, mode="code"))]
    assert ad.result("a1").error_code == "mode_unsupported"


async def test_litellm_rate_limit_classified(tmp_path):
    class RateLimitError(Exception):
        pass

    async def boom(**kw):
        raise RateLimitError("429 Too Many Requests")

    pol = parse_policy({"agents": {"ds": {"adapter": "litellm", "model": "m", "capabilities": ["analysis"]}}})
    ad = LiteLLMAdapter("ds", pol.agents["ds"], completion=boom)
    t = TaskSpec(task_id="t", title="t", objective="x", allowed_paths=["a"])
    [e async for e in ad.execute(t, litellm_ctx(tmp_path))]
    assert ad.result("a1").error_code == "rate_limited"
