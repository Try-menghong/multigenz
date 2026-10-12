"""The interactive home screen: the pet, the team with its models, slash commands, model changes, and a goal typed at
the prompt running to completion on a run-owned branch, its progress shown inline."""

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


KEYS = {" ": "space", "?": "question_mark", "#": "number_sign", "@": "at", "\\": "backslash"}


async def type_line(pilot, text, enter=True):
    for ch in text:
        await pilot.press(KEYS.get(ch, ch))
    if enter:
        await pilot.press("enter")
    await pilot.pause()


def prompt(screen) -> str:
    return screen.query_one("#home-input").text


def run_status(home_dir, rid):
    st = open_store(home_dir, readonly=True)
    try:
        return st.get_run(rid)["status"]
    finally:
        st.close()


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
        # the command menu while typing, Tab completes, Enter runs it
        await type_line(pilot, "/he", enter=False)
        menu = home.query_one("#home-menu", OptionList)
        assert menu.display and [i.label for i in home.items] == ["/help"]
        await pilot.press("tab")
        assert prompt(home) == "/help "
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
        assert prompt(home) == f"/repo {tmp_path / 'home'}"
        await pilot.press("escape")
        assert prompt(home) == ""
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
        assert await wait_for(pilot, lambda: bool(home.watching), tries=300, step=0.1), log_text(home)
        rid = home.watching[-1]
        assert "started on branch multigenz/run-" in log_text(home)
        # progress is shown right here; the dashboard is one key away
        await pilot.press("ctrl+o")
        assert await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.run_id == rid)
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.screen is home)
        assert await wait_for(pilot, lambda: f"Run {rid} succeeded" in log_text(home), tries=600, step=0.1), \
            (run_status(home_dir, rid), log_text(home))
        assert "integrated: goal" in log_text(home) and not home.watching
        assert not home.query_one("#home-live", Static).display
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


async def test_prompt_editing_menu_files_and_shortcuts(tmp_path, repo, scenario):
    f = write_policy(tmp_path, scenario, orchestrator={"planning": "auto"})
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        # ↑/↓ move in the menu, Enter runs the highlighted command
        await type_line(pilot, "/pl", enter=False)
        assert [i.label for i in home.items] == ["/plan", "/planning"]
        await pilot.press("down")
        assert home.menu.highlighted == 1
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: "planning: auto - " in log_text(home))
        assert prompt(home) == ""
        # Esc closes the menu first, then clears the prompt
        await type_line(pilot, "/mo", enter=False)
        await pilot.press("escape")
        assert not home.menu.display and prompt(home) == "/mo"
        await pilot.press("escape")
        assert prompt(home) == ""
        # several lines: Ctrl+J, or a backslash right before Enter
        await type_line(pilot, "first line", enter=False)
        await pilot.press("ctrl+j")
        await type_line(pilot, "second line\\")
        assert prompt(home) == "first line\nsecond line\n" and "first line" not in log_text(home)
        await pilot.press("escape")
        # @ mentions a file of the repository (what the agents will see)
        await type_line(pilot, "look at @app", enter=False)
        assert [i.label for i in home.items] == ["@src/app.py", "@tests/test_app.py"]   # best match first
        await pilot.press("enter")
        assert prompt(home) == "look at @src/app.py " and "look at" not in log_text(home)
        await pilot.press("escape")
        # ? lists the shortcuts
        await pilot.press("question_mark")
        assert await wait_for(pilot, lambda: "Shortcuts" in log_text(home) and "Shift+Tab" in log_text(home))
        assert prompt(home) == ""
        # the status line shows the mode, the repository and the team
        status = static(home, "#statusline")
        assert "▶ run" in status and repo.name in status and "planning auto" in status


async def test_notes_reach_every_goal_and_stay_out_of_the_repository(tmp_path, repo, scenario, monkeypatch):
    from multigenz.notes import HEADER, notes_path, read_notes

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write_scenario(scenario, "goal", writes("feature/x.py", "X = 1\n"))
    f = write_policy(tmp_path, scenario, orchestrator={"planning": "never"})
    home_dir = tmp_path / "home"
    app = MultiGenzApp(home_dir, policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        await type_line(pilot, "# use type hints everywhere")
        assert read_notes(repo) == ["use type hints everywhere"]
        assert "1 standing note" in static(home, "#home-info")
        await type_line(pilot, "/memory")
        assert "1. use type hints everywhere" in log_text(home)
        await type_line(pilot, "Add a module")
        assert await wait_for(pilot, lambda: bool(home.watching), tries=300, step=0.1), log_text(home)
        rid = home.watching[-1]
        assert await wait_for(pilot, lambda: not home.watching, tries=600, step=0.1)
        await type_line(pilot, "/memory remove 1")
        assert read_notes(repo) == []
    st = open_store(home_dir, readonly=True)
    objective = st.get_run(rid)["objective"]
    st.close()
    assert objective.startswith("Add a module") and HEADER in objective and "- use type hints everywhere" in objective
    assert not str(notes_path(repo)).startswith(str(repo))
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
                          check=True).stdout == ""


PLAN = {"schema_version": 1, "summary": "two parts", "tasks": [
    {"task_id": "lib", "title": "the library", "objective": "build lib", "allowed_paths": ["lib/"],
     "required_capabilities": ["coding"]},
    {"task_id": "app", "title": "the app", "objective": "build app", "allowed_paths": ["app/"],
     "dependencies": ["lib"], "required_capabilities": ["coding"]}]}


async def test_plan_first_shows_the_plan_and_runs_only_after_confirmation(tmp_path, repo, scenario):
    from multigenz.tui.app import ConfirmScreen

    write_scenario(scenario, "plan", {"modes": {"plan": {"default": [{"emit_json": PLAN}]}}})
    write_scenario(scenario, "lib", writes("lib/a.py", "A = 1\n"))
    write_scenario(scenario, "app", writes("app/b.py", "B = 2\n"))
    f = write_policy(tmp_path, scenario, orchestrator={"planning": "never"},
                     agents={"alpha": ["coding", "testing", "architecture"]})
    home_dir = tmp_path / "home"
    app = MultiGenzApp(home_dir, policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        await pilot.press("shift+tab")
        assert home.mode == "plan" and "plan first" in static(home, "#statusline")
        await type_line(pilot, "Build a library and an app")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), tries=300, step=0.1), \
            log_text(home)
        assert "Plan: two parts" in log_text(home) and "1. lib  the library" in log_text(home)
        assert "after: lib" in log_text(home)
        await pilot.press("n")
        assert await wait_for(pilot, lambda: "Plan discarded; nothing ran." in log_text(home))
        assert open_store(home_dir, readonly=True).list_runs() == []
        # the same through /plan, confirmed this time (mode back to run first)
        await pilot.press("shift+tab")
        assert home.mode == "run"
        await type_line(pilot, "/plan Build a library and an app")
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), tries=300, step=0.1)
        await pilot.press("y")
        assert await wait_for(pilot, lambda: bool(home.watching), tries=300, step=0.1), log_text(home)
        rid = home.watching[-1]
        assert await wait_for(pilot, lambda: f"Run {rid} succeeded" in log_text(home), tries=900, step=0.1), \
            log_text(home)
    st = open_store(home_dir, readonly=True)
    assert [t["task_id"] for t in st.list_tasks(rid)] == ["lib", "app"]     # exactly the plan that was shown
    kinds = [e["kind"] for e in st.events(rid)]
    assert "plan.requested" not in kinds                                     # not planned a second time
    st.close()


async def test_progress_inline_and_esc_cancels_the_run(tmp_path, repo, scenario):
    from multigenz.tui.app import ConfirmScreen

    write_scenario(scenario, "goal", {"attempts": {"default": [{"print": ["thinking hard"], "sleep": 60}]}})
    f = write_policy(tmp_path, scenario, orchestrator={"planning": "never"})
    home_dir = tmp_path / "home"
    app = MultiGenzApp(home_dir, policy=load_policy(f), policy_path=f, repo=repo)
    bells = []
    app.bell = lambda: bells.append(1)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        await type_line(pilot, "Think for a long time")
        assert await wait_for(pilot, lambda: bool(home.watching), tries=300, step=0.1)
        rid = home.watching[-1]
        live = home.query_one("#home-live", Static)
        assert await wait_for(pilot, lambda: "Working…" in str(live.content) and "● goal" in str(live.content)
                              and "alpha" in str(live.content), tries=300, step=0.1), str(live.content)
        assert "1 run in progress" in static(home, "#statusline")
        assert "esc cancels" in str(live.content)
        await pilot.press("escape")                       # empty prompt + a run in progress: offer to cancel it
        assert await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        assert await wait_for(pilot, lambda: f"Run {rid} cancelled" in log_text(home), tries=600, step=0.1), \
            (run_status(home_dir, rid), log_text(home))
        assert not live.display and bells == [1]
    assert run_status(home_dir, rid) == "cancelled"


async def test_status_cost_theme_export_resume_and_history(tmp_path, repo, scenario, monkeypatch):
    from multigenz.core.driver import start_run

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    secret = "sk-export-secret-0123456789abcdef"
    monkeypatch.setenv("EXPORT_TEST_API_KEY", secret)
    write_scenario(scenario, "goal", writes("feature/r.py", "R = 1\n"))
    f = write_policy(tmp_path, scenario, orchestrator={"planning": "never"})
    home_dir = tmp_path / "home"
    # a run that was created but whose worker never started (e.g. the machine went down): /resume continues it
    rid, _ = await start_run(load_policy(f), objective="Resume me", repo=repo, plan=None, home=home_dir,
                             execute=False)
    app = MultiGenzApp(home_dir, policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(130, 45)) as pilot:
        home = app.screen
        await type_line(pilot, "/status")
        text = log_text(home)
        assert "sandbox   none" in text and "pushing   disabled" in text and f"1 not finished: {rid}" in text
        await type_line(pilot, f"/resume {rid[:6]}")
        assert f"Resuming run {rid}" in log_text(home)
        assert await wait_for(pilot, lambda: f"Run {rid} succeeded" in log_text(home), tries=600, step=0.1), \
            (run_status(home_dir, rid), log_text(home))
        await type_line(pilot, "/cost")
        assert f"{rid}  succeeded" in log_text(home) and "total $0.0000 over 1 runs" in log_text(home)
        await type_line(pilot, "/theme nord")
        assert app.theme == "nord"
        home.say(f"a line with {secret} in it")
        out = tmp_path / "session.txt"
        await type_line(pilot, f"/export {out}")
        assert "Saved" in log_text(home)
    exported = out.read_text()
    assert "/status" in exported and secret not in exported and oct(out.stat().st_mode & 0o777) == "0o600"
    assert '"theme": "nord"' in (tmp_path / "xdg" / "multigenz" / "tui.json").read_text()
    # a new session: the theme is kept and ↑ recalls the previous session's lines
    app2 = MultiGenzApp(home_dir, policy=load_policy(f), policy_path=f, repo=repo)
    async with app2.run_test(size=(130, 45)) as pilot:
        assert app2.theme == "nord"
        await pilot.press("up")
        assert prompt(app2.screen) == f"/export {out}"
