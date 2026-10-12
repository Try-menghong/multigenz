"""TUI: add an API provider, fetch its model ids, pick one, and get an agent - all from the Team screen (m)."""

import yaml
from textual.widgets import DataTable, Input, OptionList, RadioButton, Select, Static

from multigenz.config import load_policy
from multigenz.tui.app import MultiGenzApp
from multigenz.tui.providers import AddProviderScreen, ModelPickerScreen, ProvidersScreen
from multigenz.tui.team import TeamScreen
from tests.support import mock_llm
from tests.support.helpers import scripted_policy

KEY = "sk-tui-provider-0123456789abcdef"


async def wait_for(pilot, cond, tries=200):
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return True
    return False


async def test_add_provider_fetch_models_pick_one_and_get_an_agent(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MOCKPROV_API_KEY", raising=False)
    monkeypatch.setattr(mock_llm, "REQUIRED_KEY", KEY)
    srv = mock_llm.serve(0, None)
    base_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    pol = scripted_policy(scenario, isolation="none", agents={"alpha": ["coding", "architecture"]})
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump(pol.model_dump(mode="json"), sort_keys=False))
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f, start="team")
    try:
        async with app.run_test(size=(170, 50)) as pilot:
            team = app.screen
            assert isinstance(team, TeamScreen)
            await wait_for(pilot, lambda: bool(team.probes))
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, ProvidersScreen)
            await pilot.press("n")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, AddProviderScreen)
            form.query_one("#preset", Select).value = "custom"
            await pilot.pause()
            form.query_one("#pname", Input).value = "mockprov"
            form.query_one("#purl", Input).value = base_url
            form.query_one("#penv", Input).value = "MOCKPROV_API_KEY"
            form.query_one("#pkey", Input).value = KEY
            assert form.query_one("#pkey", Input).password       # the key is never shown
            await pilot.click("#save")
            # saving opens the model picker, which fetches the ids with the stored key
            assert await wait_for(pilot, lambda: isinstance(app.screen, ModelPickerScreen))
            picker = app.screen
            assert await wait_for(pilot, lambda: len(picker.models) == 3), \
                str(picker.query_one("#mp-status", Static).content)
            picker.query_one("#mp-filter", Input).value = "large"
            await pilot.pause()
            ol = picker.query_one("#mp-models", OptionList)
            assert ol.option_count == 1
            ol.focus()
            ol.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()
            assert picker.chosen == "mock-large"
            assert picker.query_one("#mp-agent", Input).value == "mockprov-mock-large"
            picker.query_one("#use-text", RadioButton).value = True    # a text-only planner/reviewer
            await pilot.pause()
            await pilot.click("#mp-add")
            assert await wait_for(pilot, lambda: isinstance(app.screen, ProvidersScreen))
            rows = app.screen.query_one("#prov-table", DataTable)
            assert "mockprov-mock-large (mock-large)" in str(rows.get_row("mockprov"))
            await pilot.press("escape")
            assert await wait_for(pilot, lambda: isinstance(app.screen, TeamScreen))
            assert await wait_for(pilot, lambda: "mockprov-mock-large" in
                                  [str(k.value) for k in team.query_one("#matrix", DataTable).rows])
    finally:
        srv.shutdown()
    saved = load_policy(f)
    assert saved.providers["mockprov"].base_url == base_url and saved.providers["mockprov"].api_key_env == "MOCKPROV_API_KEY"
    agent = saved.agents["mockprov-mock-large"]
    assert (agent.adapter, agent.provider, agent.model) == ("litellm", "mockprov", "mock-large")
    secrets = tmp_path / "xdg" / "multigenz" / "secrets.env"
    assert KEY in secrets.read_text() and oct(secrets.stat().st_mode & 0o777) == "0o600"
    assert KEY not in f.read_text()


async def test_a_wrong_key_is_reported_not_hidden(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MOCKPROV_API_KEY", "wrong-key-0123456789")
    monkeypatch.setattr(mock_llm, "REQUIRED_KEY", KEY)
    srv = mock_llm.serve(0, None)
    base_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    pol = scripted_policy(scenario, isolation="none", agents={"alpha": ["coding"]})
    data = pol.model_dump(mode="json")
    data["providers"] = {"mockprov": {"kind": "openai-compatible", "base_url": base_url,
                                      "api_key_env": "MOCKPROV_API_KEY"}}
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump(data, sort_keys=False))
    app = MultiGenzApp(tmp_path / "home", policy=load_policy(f), policy_path=f)
    try:
        async with app.run_test(size=(170, 50)) as pilot:
            app.push_screen(ModelPickerScreen(f, "mockprov"))
            await pilot.pause()
            status = app.screen.query_one("#mp-status", Static)
            assert await wait_for(pilot, lambda: "rejected the API key" in str(status.content)), str(status.content)
    finally:
        srv.shutdown()
