"""The interactive home screen: the pet, the team with its models, slash commands, model changes, and a goal typed at
the prompt running to completion on a run-owned branch."""

import stat
import subprocess

import yaml
from textual.widgets import DataTable, Input, OptionList, RichLog, Static

from multigenz.config import load_policy
from multigenz.core.runs import open_store
from multigenz.tui.app import MultiGenzApp, RunScreen
from multigenz.tui.home import HomeScreen
from multigenz.tui.model import ModelScreen, PickAgentScreen
from multigenz.tui.team import COLUMNS, TeamScreen
from tests.support.helpers import scripted_policy, write_scenario, writes

CLAUDE_HELP = """Usage: claude [options]
  -p, --print                    print the response and exit
  --permission-mode <mode>       (choices: "acceptEdits", "plan")
  --no-session-persistence       do not save the session
  --tools <tools...>             built-in tools
  --model <model>                Model for the session. Provide an alias for the latest model (e.g. 'sonnet' or
                                 'opus') or a model's full name.
  --settings <file>              settings
"""


def fake_claude(tmp_path):
    """A stand-in for the claude binary: just its help text and version (enough to choose and verify a model)."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    (d / "claude-help.txt").write_text(CLAUDE_HELP)
    p = d / "claude"
    p.write_text(f'#!/bin/sh\ncase "$*" in\n  --version) echo "2.9.9 (Claude Code)";;\n'
                 f'  *--help*) cat "{d / "claude-help.txt"}";;\nesac\n')
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def write_policy(tmp_path, scenario, **kw) -> object:
    pol = scripted_policy(scenario, isolation="none", **kw)
    data = pol.model_dump(mode="json")
    data["agents"]["claude"] = {"adapter": "cli", "command": str(fake_claude(tmp_path)), "capabilities": ["coding"]}
    data["agents"]["ds"] = {"adapter": "litellm", "model": "deepseek/deepseek-chat", "capabilities": ["analysis"]}
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump(data, sort_keys=False))
    return f


def log_text(screen) -> str:
    """The messages, with soft-wrapped lines joined back together."""
    lines = ["".join(seg.text for seg in line).rstrip() for line in screen.query_one("#home-log", RichLog).lines]
    return " ".join(lines)


def static(screen, wid) -> str:
    return str(screen.query_one(wid, Static).content)


async def wait_for(pilot, cond, tries=200, step=0.05):
    for _ in range(tries):
        await pilot.pause(step)
        if cond():
            return True
    return False


async def type_line(pilot, text, enter=True):
    for ch in text:
        await pilot.press("space" if ch == " " else ch)
    if enter:
        await pilot.press("enter")
    await pilot.pause()


async def test_home_shows_the_pet_the_team_and_runs_commands(tmp_path, repo, scenario):
    f = write_policy(tmp_path, scenario, orchestrator={"planning": "auto"})
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        assert isinstance(home, HomeScreen)
        assert "▄███▄ ▄███▄ ▄███▄" in str(home.query_one("#mascot", Static).content)
        info, team = static(home, "#home-info"), static(home, "#home-team")
        assert "Welcome to MultiGenz" in info and str(repo.name) in info
        assert "alpha (default model)" in team and "ds (deepseek/deepseek-chat)" in team and "auto:" in team
        # hints while typing a command, Tab completes it
        await type_line(pilot, "/he", enter=False)
        hints = home.query_one("#home-hints", Static)
        assert hints.display and "/help" in str(hints.content)
        await pilot.press("tab")
        assert home.query_one("#home-input", Input).value == "/help "
        await pilot.press("enter")
        await pilot.pause()
        assert "/model" in log_text(home) and "anything else is a goal" in log_text(home)
        await type_line(pilot, "/nonsense")
        assert "Unknown command /nonsense" in log_text(home)
        await type_line(pilot, "/planning never")
        assert load_policy(f).orchestrator.planning == "never" and "never:" in static(home, "#home-team")
        await type_line(pilot, f"/repo {tmp_path / 'home'}")
        assert "is not a git repository; nothing changed" in log_text(home) and app.repo == repo
        # history: up recalls the last line
        await pilot.press("up")
        assert home.query_one("#home-input", Input).value == f"/repo {tmp_path / 'home'}"
        await pilot.press("escape")
        await type_line(pilot, "/team")
        assert await wait_for(pilot, lambda: isinstance(app.screen, TeamScreen))
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.screen is home)
        await type_line(pilot, "/runs")
        assert await wait_for(pilot, lambda: type(app.screen).__name__ == "RunsScreen")
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.screen is home)
        await type_line(pilot, "/quit")
    assert app.return_code in (0, None)


async def test_change_models_from_the_prompt_the_picker_and_the_team_screen(tmp_path, repo, scenario):
    f = write_policy(tmp_path, scenario)
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        # 1. directly: /model AGENT MODEL (checked against the CLI's help first)
        await type_line(pilot, "/model claude sonnet")
        assert await wait_for(pilot, lambda: "--model sonnet" in log_text(home)), log_text(home)
        assert load_policy(f).agents["claude"].model == "sonnet"
        assert "claude (sonnet)" in static(home, "#home-team")
        # an agent whose CLI shows no model option is refused, and nothing is saved
        await type_line(pilot, "/model alpha big-model")
        assert await wait_for(pilot, lambda: "no way to choose a model" in log_text(home)), log_text(home)
        assert load_policy(f).agents["alpha"].model is None
        # 2. /model alone: pick the agent, then a model the tool itself lists (claude --help aliases)
        await type_line(pilot, "/model")
        assert await wait_for(pilot, lambda: isinstance(app.screen, PickAgentScreen))
        agents = app.screen.query_one("#pa-agents", OptionList)
        agents.highlighted = [agents.get_option_at_index(i).id for i in range(agents.option_count)].index("claude")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ModelScreen))
        picker = app.screen
        assert await wait_for(pilot, lambda: picker.choices.models == ["opus", "sonnet"]), picker.choices
        ol = picker.query_one("#ms-models", OptionList)
        ol.focus()
        ol.highlighted = 0
        await pilot.press("enter")                                   # opus
        assert await wait_for(pilot, lambda: app.screen is home)
        assert load_policy(f).agents["claude"].model == "opus" and "model saved: claude: opus" in log_text(home)
        # 3. from the Team screen: m on an agent, type any name, Enter
        await pilot.press("ctrl+t")
        assert await wait_for(pilot, lambda: isinstance(app.screen, TeamScreen))
        team = app.screen
        t = team.query_one("#matrix", DataTable)
        from textual.coordinate import Coordinate

        t.cursor_coordinate = Coordinate([str(k.value) for k in t.rows].index("claude"), COLUMNS.index("model"))
        await pilot.press("m")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ModelScreen))
        app.screen.query_one("#ms-input", Input).value = "claude-opus-4-1"
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.screen is team)
        assert await wait_for(pilot, lambda: str(t.get_cell("claude", "model")) == "claude-opus-4-1")
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.screen is home)
        # back to the CLI's own default
        await type_line(pilot, "/model claude default")
        assert await wait_for(pilot, lambda: "own default model" in log_text(home)), log_text(home)
    assert load_policy(f).agents["claude"].model is None
    assert "model:" not in f.read_text().split("claude:")[1].split("ds:")[0]


async def test_a_goal_typed_at_the_prompt_runs_on_a_run_owned_branch(tmp_path, repo, scenario):
    write_scenario(scenario, "goal", writes("feature/hello.py", "print('hello')\n"))
    f = write_policy(tmp_path, scenario, orchestrator={"planning": "never"})
    head_before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                 check=True).stdout.strip()
    home_dir = tmp_path / "home"
    app = MultiGenzApp(home_dir, policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        await type_line(pilot, "Add a hello module")
        assert await wait_for(pilot, lambda: isinstance(app.screen, RunScreen), tries=300, step=0.1), log_text(home)
        rid = app.screen.run_id
        assert "started on branch multigenz/run-" in log_text(home)

        def status():
            st = open_store(home_dir, readonly=True)
            try:
                return st.get_run(rid)["status"]
            finally:
                st.close()

        assert await wait_for(pilot, lambda: status() in ("succeeded", "failed"), tries=600, step=0.1), status()
        assert status() == "succeeded"
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.screen is home)
        assert await wait_for(pilot, lambda: rid in static(home, "#home-recent"))
    branch = f"multigenz/run-{rid}/integration"
    shown = subprocess.run(["git", "-C", str(repo), "show", f"{branch}:feature/hello.py"], capture_output=True,
                           text=True, check=True).stdout
    assert shown == "print('hello')\n"
    # the user's checkout is untouched
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip() == head_before
    assert not (repo / "feature").exists()


async def test_an_invalid_policy_is_shown_and_nothing_starts(tmp_path, repo):
    f = tmp_path / "policy.yaml"
    f.write_text("agents:\n  x: {adapter: cli}\nnot_a_section: 1\n")
    app = MultiGenzApp(tmp_path / "home", policy=None, policy_error="bad", policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        assert "POLICY PROBLEM" in static(home, "#home-info")
        await type_line(pilot, "/model")
        assert "The policy is invalid" in log_text(home) and app.screen is home
        await type_line(pilot, "Build something")
        assert "Cannot start: the policy is invalid" in log_text(home)
    assert not (tmp_path / "home" / "runs").exists() or not any((tmp_path / "home" / "runs").iterdir())
