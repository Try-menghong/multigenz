"""TUI Team screen: assign planner / workers / reviewer / conflict roles, enable/disable agents, save to the policy."""

import yaml
from textual.widgets import DataTable, Static

from multigenz.config import load_policy
from multigenz.tui.app import ConfirmScreen, MultiGenzApp, RunsScreen
from multigenz.tui.team import AddAgentScreen, TeamScreen
from tests.support.helpers import scripted_policy


def write_policy(tmp_path, scenario, extra_agents=None):
    pol = scripted_policy(scenario, isolation="none",
                          agents={"alpha": ["coding", "architecture"], "beta": ["coding"], "gamma": ["coding", "review"]})
    data = pol.model_dump(mode="json")
    data["agents"].update(extra_agents or {})
    f = tmp_path / "policy.yaml"
    f.write_text("# my policy - keep this comment\n" + yaml.safe_dump(data, sort_keys=False))
    return f


async def open_team(pilot):
    screen = pilot.app.screen
    assert isinstance(screen, TeamScreen)
    for _ in range(100):  # wait for the --help probes
        await pilot.pause(0.05)
        if screen.probes:
            break
    assert screen.probes, "agents were not probed"
    return screen


def cell(screen, agent, col):
    t = screen.query_one("#matrix", DataTable)
    return str(t.get_cell(agent, col))


async def goto(pilot, screen, agent, col):
    from textual.coordinate import Coordinate

    t = screen.query_one("#matrix", DataTable)
    rows = [str(k.value) for k in t.rows]
    t.cursor_coordinate = Coordinate(rows.index(agent), ["agent", "on", "planner", "worker", "reviewer", "conflict",
                                                           "status"].index(col))
    await pilot.pause()


async def test_assign_reorder_disable_and_save(tmp_path, scenario):
    f = write_policy(tmp_path, scenario, extra_agents={"ds": {"adapter": "litellm", "model": "deepseek/x"}})
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f, start="team")
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_team(pilot)
        # automatic to begin with: the planner column marks who automatic selection would use
        assert cell(screen, "alpha", "planner") == "auto" and cell(screen, "beta", "planner") == "·"
        assert cell(screen, "alpha", "worker") == "auto"
        # workers: beta first, then alpha; then swap the order with [
        await goto(pilot, screen, "beta", "worker")
        await pilot.press("space")
        await goto(pilot, screen, "alpha", "worker")
        await pilot.press("space")
        assert screen.roles["worker"] == ["beta", "alpha"] and cell(screen, "alpha", "worker") == "#2"
        await pilot.press("left_square_bracket")
        assert screen.roles["worker"] == ["alpha", "beta"]
        assert "same as workers: alpha -> beta" in str(screen.query_one("#team-summary", Static).content)
        # the planner: gamma (no 'architecture' capability needed once you choose it explicitly)
        await goto(pilot, screen, "gamma", "planner")
        await pilot.press("space")
        # a text-only agent cannot be a worker: refused with a reason, nothing changes
        await goto(pilot, screen, "ds", "worker")
        await pilot.press("space")
        assert "cannot change files" in str(screen.query_one("#team-status", Static).content)
        assert "ds" not in screen.roles["worker"]
        # disable beta with e
        await goto(pilot, screen, "beta", "on")
        await pilot.press("e")
        assert cell(screen, "beta", "on") == "no" and screen.dirty
        await pilot.press("s")
        await pilot.pause()
        assert not screen.dirty
        assert app.policy.roles.worker == ["alpha", "beta"]      # new runs from the TUI use the saved team
    saved = load_policy(f)
    assert saved.roles.worker == ["alpha", "beta"] and saved.roles.planner == ["gamma"]
    assert not saved.agents["beta"].enabled and saved.agents["alpha"].enabled
    assert f.read_text().startswith("# my policy - keep this comment")
    assert (tmp_path / "policy.yaml.bak").exists()


async def test_unsaved_changes_need_confirmation_and_role_back_to_automatic(tmp_path, scenario):
    f = write_policy(tmp_path, scenario)
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
        await pilot.press("t")                                  # Team from the runs list
        screen = await open_team(pilot)
        await goto(pilot, screen, "gamma", "reviewer")
        assert cell(screen, "gamma", "reviewer") == "·"         # agent review is off until a reviewer is chosen
        await pilot.press("space")
        assert screen.roles["reviewer"] == ["gamma"]
        await pilot.press("c")                                  # back to automatic
        assert screen.roles["reviewer"] == [] and screen.dirty
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")                                  # keep editing
        await pilot.pause()
        assert app.screen is screen
        await pilot.press("escape")
        await pilot.press("y")                                  # discard
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
    assert load_policy(f).roles.reviewer == [] and not (tmp_path / "policy.yaml.bak").exists()


async def test_add_agent_from_a_preset(tmp_path, scenario):
    f = write_policy(tmp_path, scenario)
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f, start="team")
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_team(pilot)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, AddAgentScreen)
        options = [str(app.screen.query_one("#presets").get_option_at_index(i).prompt)
                   for i in range(app.screen.query_one("#presets").option_count)]
        assert any(o.startswith("opencode") for o in options)
        idx = next(i for i, o in enumerate(options) if o.startswith("opencode"))
        for _ in range(idx):
            await pilot.press("down")
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if "opencode" in [str(k.value) for k in screen.query_one("#matrix", DataTable).rows]:
                break
    assert "opencode" in load_policy(f).agents
