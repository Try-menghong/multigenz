"""Choosing an agent's model: the CLI's model option is used only once its help shows it; saving checks that first."""

import stat
from pathlib import Path

import yaml

from multigenz.adapters.codex import CodexAdapter
from multigenz.adapters.registry import build_adapters
from multigenz.config import load_policy, parse_policy
from multigenz.contracts.agent import CancellationToken, ExecutionContext
from multigenz.contracts.task import TaskSpec
from multigenz.models import check_model, claude_aliases, cli_kind, model_choices, set_model

CODEX_TOKENS = "--sandbox workspace-write read-only --skip-git-repo-check --ephemeral --color"


def fake_cli(tmp_path: Path, name: str, help_lines: list[str], extra: str = "") -> Path:
    """A shell script that prints the given help text (for any --help call) and a version."""
    p = tmp_path / "bin" / name
    p.parent.mkdir(exist_ok=True)
    help_text = "\\n".join(help_lines)
    p.write_text(f'#!/bin/sh\ncase "$*" in\n  --version) echo "{name} 9.9";;\n{extra}'
                 f'  *--help*) printf \'%b\\n\' "{help_text}";;\nesac\n')
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class FakeRunner:
    def workspace_path(self, ctx):
        return "/workspace"

    def inbox_path(self, ctx):
        return "/inbox"


def ctx(tmp_path):
    return ExecutionContext(run_id="r", task_id="t", attempt_id="a", worktree=tmp_path, sandbox_id="s",
                            sandbox_backend="none", deadline=9e12, cancel=CancellationToken(), log_dir=tmp_path,
                            mode="code")


TASK = TaskSpec(task_id="t", title="t", objective="o", allowed_paths=["**"])


async def test_model_flag_is_appended_once_the_help_shows_it(tmp_path):
    binary = fake_cli(tmp_path, "codex", [CODEX_TOKENS, "  -m, --model <MODEL>  Model the agent should use"])
    pol = parse_policy({"agents": {"codex": {"adapter": "cli", "command": str(binary), "model": "gpt-5-codex",
                                             "capabilities": ["coding"]}}})
    ad = CodexAdapter("codex", pol.agents["codex"], runner=FakeRunner())
    p = await ad.probe()
    assert p.enabled and ad.passes_model and ad.model_problem is None
    argv = ad._render_argv(ad.invocation_for("code"), TASK, ctx(tmp_path))
    assert argv[-2:] == ["--model", "gpt-5-codex"] and argv.count("--model") == 1


async def test_without_a_model_nothing_is_added(tmp_path):
    binary = fake_cli(tmp_path, "codex", [CODEX_TOKENS, "  -m, --model <MODEL>"])
    pol = parse_policy({"agents": {"codex": {"adapter": "cli", "command": str(binary), "capabilities": ["coding"]}}})
    ad = CodexAdapter("codex", pol.agents["codex"], runner=FakeRunner())
    assert (await ad.probe()).enabled and not ad.passes_model
    assert "--model" not in ad._render_argv(ad.invocation_for("code"), TASK, ctx(tmp_path))


async def test_a_model_the_cli_cannot_take_disables_the_agent_instead_of_ignoring_it(tmp_path):
    # only --model-provider: a look-alike, not the option itself
    binary = fake_cli(tmp_path, "codex", [CODEX_TOKENS, "  --model-provider <P>"])
    pol = parse_policy({"agents": {"codex": {"adapter": "cli", "command": str(binary), "model": "gpt-5-codex",
                                             "capabilities": ["coding"]}}})
    p = await build_adapters(pol)["codex"].probe()
    assert not p.enabled and "no way to choose a model" in p.reason


async def test_an_admin_invocation_can_place_the_model_itself(tmp_path):
    binary = fake_cli(tmp_path, "mycli", ["  --go  run", "  --llm NAME"])
    pol = parse_policy({"agents": {"mine": {
        "adapter": "cli", "command": str(binary), "model": "big-1", "capabilities": ["coding"],
        "invocations": [{"name": "go", "argv": ["{command}", "--go", "--llm", "{model}"], "stdin": "prompt",
                         "requires_help_tokens": ["--go", "--llm"], "modes": ["code"]}]}}})
    ad = build_adapters(pol)["mine"]
    ad.runner = FakeRunner()
    assert (await ad.probe()).enabled
    assert ad._render_argv(ad.invocation_for("code"), TASK, ctx(tmp_path))[1:] == ["--go", "--llm", "big-1"]


def test_claude_aliases_come_from_its_help():
    help_text = ("  --mcp-config <configs...>  Load MCP servers\n"
                 "  --model <model>            Model for the current session. Provide an alias for the latest model "
                 "(e.g. 'sonnet' or 'opus') or a model's full name (e.g. 'claude-sonnet-4-5').\n"
                 "  --permission-mode <mode>   Permission mode ('plan')\n")
    assert claude_aliases(help_text) == ["claude-sonnet-4-5", "opus", "sonnet"]
    assert claude_aliases("no model option here") == []


async def test_check_and_set_model_in_the_policy_file(tmp_path):
    good = fake_cli(tmp_path, "codex", [CODEX_TOKENS, "  -m, --model <MODEL>"])
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump({"agents": {
        "codex": {"adapter": "cli", "command": str(good), "capabilities": ["coding"]},
        "ds": {"adapter": "litellm", "model": "deepseek/deepseek-chat", "capabilities": ["analysis"]}}}))
    check, saved = await set_model(f, "codex", "gpt-5-codex")
    assert saved is not None and check.ok and "--model gpt-5-codex" in check.message
    assert load_policy(f).agents["codex"].model == "gpt-5-codex" and (tmp_path / "policy.yaml.bak").exists()
    check, saved = await set_model(f, "codex", None)
    assert saved is not None and load_policy(f).agents["codex"].model is None
    check, saved = await set_model(f, "ds", None)          # an API agent always needs a model
    assert saved is None and not check.ok and load_policy(f).agents["ds"].model == "deepseek/deepseek-chat"
    check, saved = await set_model(f, "nope", "x")
    assert saved is None and "no agent" in check.message


async def test_a_cli_without_a_model_option_is_refused_before_saving(tmp_path):
    bad = fake_cli(tmp_path, "codex", [CODEX_TOKENS])
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump({"agents": {"codex": {"adapter": "cli", "command": str(bad),
                                                      "capabilities": ["coding"]}}}))
    before = f.read_text()
    check, saved = await set_model(f, "codex", "gpt-5-codex")
    assert saved is None and "no way to choose a model" in check.message and f.read_text() == before


async def test_a_missing_binary_is_saved_for_later_with_a_note(tmp_path):
    cfg = parse_policy({"agents": {"codex": {"adapter": "cli", "command": "no-such-codex-xyz",
                                             "capabilities": ["coding"]}}}).agents["codex"]
    check = await check_model("codex", cfg, "gpt-5-codex")
    assert check.ok and "cannot be checked now" in check.message


async def test_choices_are_asked_from_the_tool(tmp_path):
    claude = fake_cli(tmp_path, "claude", ["  --model <model>  alias for the latest model (e.g. 'fable', 'opus')"])
    opencode = fake_cli(tmp_path, "opencode", ["  -m, --model"],
                        extra='  models) printf "opencode/big-pickle\\nanthropic/claude-x\\nnot a model line\\n";;\n')
    pol = parse_policy({"agents": {
        "claude": {"adapter": "cli", "command": str(claude), "capabilities": ["coding"]},
        "oc": {"adapter": "cli", "command": str(opencode), "capabilities": ["coding"]},
        "hermes": {"adapter": "cli", "command": str(claude), "capabilities": ["coding"]}}})
    assert cli_kind("oc", pol.agents["oc"]) == "opencode"
    got = await model_choices("claude", pol.agents["claude"], pol)
    assert got.models == ["fable", "opus"] and got.free_text
    got = await model_choices("oc", pol.agents["oc"], pol)
    assert got.models == ["opencode/big-pickle", "anthropic/claude-x"]
    got = await model_choices("hermes", pol.agents["hermes"], pol)
    assert got.models == [] and got.free_text and "type one" in got.note


def test_every_shipped_cli_lists_its_model_option_in_the_recorded_help():
    from multigenz.adapters.antigravity import AntigravityAdapter
    from multigenz.adapters.cli_adapter import has_flag
    from multigenz.adapters.cline import ClineAdapter
    from multigenz.adapters.hermes import HermesAdapter
    from multigenz.adapters.opencode import OpencodeAdapter

    help_dir = Path(__file__).parents[1] / "contract" / "fixtures" / "help"
    for fixture, cls in [("cline-3.0.70.txt", ClineAdapter), ("opencode-1.19.0-run.txt", OpencodeAdapter),
                         ("codex-0.162.1-exec.txt", CodexAdapter), ("agy-1.3.3.txt", AntigravityAdapter),
                         ("hermes-agent-0.19.0.txt", HermesAdapter)]:
        assert cls.MODEL_FLAG and has_flag((help_dir / fixture).read_text(), cls.MODEL_FLAG), fixture
    assert not has_flag("--model-provider x", "--model") and has_flag("-m, --model <M>", "--model")
