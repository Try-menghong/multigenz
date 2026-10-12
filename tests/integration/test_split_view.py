"""Split view: while several agents work on one run at the same time, the run dashboard shows a live pane for each,
side by side, so nothing has to be watched from another terminal.

A goal is typed at the home prompt; a planner agent splits it into three independent tasks; three different agents
(one task each, ``max_concurrent: 1``) work at the same time, printing as they go. The run is executed by the
detached worker exactly as for a user.
"""

import yaml

from multigenz.config import load_policy
from multigenz.core.runs import open_store
from multigenz.tui.app import MultiGenzApp, RunScreen
from multigenz.tui.panes import AgentGrid, AgentPane
from tests.support.helpers import scripted_policy, write_scenario

PARTS = ("api", "web", "docs")
PLAN = {"schema_version": 1, "summary": "three independent parts", "tasks": [
    {"task_id": t, "title": t, "objective": f"build {t}", "allowed_paths": [f"{t}/"],
     "required_capabilities": ["coding"]} for t in PARTS]}
AGENTS = {"lead": ["architecture"], "alpha": ["coding", "testing"], "beta": ["coding", "testing"],
          "gamma": ["coding", "testing"]}
SECRET = "sk-split-view-secret-0123456789"


def scenarios(scenario):
    write_scenario(scenario, "plan", {"modes": {"plan": {"default": [
        {"print": ["lead: reading the repository"], "sleep": 1.0},
        {"print": ["lead: three independent parts"], "emit_json": PLAN}]}}})
    for t in PARTS:
        write_scenario(scenario, t, {"attempts": {"default": [
            {"print": [f"{t}: step 1 reading"], "sleep": 2.5},
            {"print": [f"{t}: step 2 writing {t}/main.py", f"token={SECRET}"], "stderr": [f"{t}: progress note"],
             "write": {f"{t}/main.py": f"NAME = {t!r}\n"}, "sleep": 2.5},
            {"print": [f"{t}: step 3 done"]}]}})


async def wait_for(pilot, cond, tries=400, step=0.1):
    for _ in range(tries):
        await pilot.pause(step)
        if cond():
            return True
    return False


async def test_three_agents_working_at_once_get_three_live_panes(tmp_path, repo, scenario, monkeypatch):
    monkeypatch.setenv("SPLIT_VIEW_API_KEY", SECRET)   # a host secret: redacted wherever it shows up
    scenarios(scenario)
    pol = scripted_policy(scenario, isolation="none", agents=AGENTS, orchestrator={"planning": "always"},
                          agent_extra={a: {"max_concurrent": 1} for a in ("alpha", "beta", "gamma")})
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump(pol.model_dump(mode="json"), sort_keys=False))
    # the state directory's path contains a secret value: paths in redacted events would be mangled, panes are not
    home_dir = tmp_path / f"state-{SECRET}" / "home"
    app = MultiGenzApp(home_dir, policy=load_policy(f), policy_path=f, repo=repo)
    async with app.run_test(size=(180, 50)) as pilot:
        for ch in "Build an api, a web app and docs":
            await pilot.press("space" if ch == " " else ch)
        await pilot.press("enter")
        home = app.screen
        assert await wait_for(pilot, lambda: bool(home.watching))      # progress inline on the home screen ...
        await pilot.press("ctrl+o")                                      # ... and the split view one key away
        assert await wait_for(pilot, lambda: isinstance(app.screen, RunScreen))
        screen = app.screen
        grid = screen.query_one("#agents", AgentGrid)

        def active_panes():
            return [p for p in grid.panes() if p.worker["active"]]

        # the planner is shown while it works
        assert await wait_for(pilot, lambda: any(p.worker["kind"] == "plan" for p in grid.panes()))
        plan_pane = next(p for p in grid.panes() if p.worker["kind"] == "plan")
        assert plan_pane.worker["agent"] == "lead"
        assert await wait_for(pilot, lambda: "lead: reading the repository" in plan_pane.text())

        # then three coding agents at the same time, each in its own pane, each showing only its own work
        assert await wait_for(pilot, lambda: len(active_panes()) == 3
                              and all("step 2" in p.text() for p in active_panes())), \
            [(p.worker["task_id"], p.worker["status"], p.text()) for p in grid.panes()]
        panes = {p.worker["task_id"]: p for p in active_panes()}
        assert set(panes) == set(PARTS)
        assert len({p.worker["agent"] for p in panes.values()}) == 3          # three different agents
        for t, p in panes.items():
            text = p.text()
            assert f"{t}: step 1 reading" in text and f"{t}: progress note" in text
            assert not any(f"{other}:" in text for other in PARTS if other != t)
            assert SECRET not in text and "token=" in text                     # redacted live, too
            assert "working" in str(p.query_one(".pane-title").content)
        assert "agents working: 3" in str(screen.query_one("#run-header").content)
        assert grid.styles.grid_size_columns == 2 and grid.styles.grid_size_rows == 2   # 3 coders + the planner

        # switching the split view off and on again
        await pilot.press("v")
        assert not grid.display
        await pilot.press("v")
        assert await wait_for(pilot, lambda: grid.display and len(grid.panes()) == 4)

        # when they finish, the panes stay with their final state
        def status():
            st = open_store(home_dir, readonly=True)
            try:
                return st.get_run(screen.run_id)["status"]
            finally:
                st.close()

        assert await wait_for(pilot, lambda: status() in ("succeeded", "failed"), tries=900), status()
        assert status() == "succeeded"
        assert await wait_for(pilot, lambda: all(not p.worker["active"] for p in grid.panes())
                              and all("step 3 done" in p.text() for p in grid.panes() if p.worker["kind"] == "code"))
        assert all("done" in str(p.query_one(".pane-title").content) for p in grid.panes())
        assert {type(p) for p in grid.panes()} == {AgentPane}
