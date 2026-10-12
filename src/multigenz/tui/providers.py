"""API providers in the TUI: add a provider (key stored 0600, never in the policy), fetch its model ids, pick one and
turn it into an agent - a coding agent (opencode harness) or a text-only planner/reviewer (direct API call)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, OptionList, RadioButton, RadioSet, Select, Static
from textual.widgets.option_list import Option

from multigenz.config import load_policy
from multigenz.providers import PRESETS, ProviderError, fetch_models, key_for, set_secret


class AddProviderScreen(ModalScreen[str | None]):
    """Preset, name, endpoint, key variable and the key itself (hidden)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, policy_path: Path, taken: set[str]):
        super().__init__()
        self.policy_path, self.taken = policy_path, taken

    def compose(self) -> ComposeResult:
        with Vertical(id="add-provider"):
            yield Label("Add an API provider")
            yield Select([(p.label, p.key) for p in PRESETS.values()], value="openrouter", allow_blank=False,
                         id="preset")
            yield Input(placeholder="name in the policy", id="pname")
            yield Input(placeholder="endpoint, e.g. https://openrouter.ai/api/v1", id="purl")
            yield Input(placeholder="environment variable for the key, e.g. OPENROUTER_API_KEY (empty: no key)",
                        id="penv")
            yield Input(placeholder="API key (hidden; stored in ~/.config/multigenz/secrets.env, mode 0600)",
                        password=True, id="pkey")
            with Horizontal():
                yield Button("Save and fetch models", id="save", variant="success")
                yield Button("Cancel", id="cancel")
            yield Static("", id="perror")

    def on_mount(self) -> None:
        self._fill("openrouter")
        self.query_one("#pkey", Input).focus()

    def _fill(self, key: str) -> None:
        p = PRESETS[key]
        name = key if key not in self.taken else f"{key}-2"
        self.query_one("#pname", Input).value = name
        self.query_one("#purl", Input).value = p.base_url
        self.query_one("#penv", Input).value = p.api_key_env or ("" if key in ("ollama", "lmstudio") else
                                                                  f"{name.upper().replace('-', '_')}_API_KEY")

    @on(Select.Changed, "#preset")
    def _preset(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._fill(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        from multigenz.policy_edit import add_provider, short_error

        preset = PRESETS[str(self.query_one("#preset", Select).value)]
        name = self.query_one("#pname", Input).value.strip()
        url = self.query_one("#purl", Input).value.strip()
        env = self.query_one("#penv", Input).value.strip() or None
        key = self.query_one("#pkey", Input).value.strip()
        err = self.query_one("#perror", Static)
        if not name or not url:
            err.update("A name and an endpoint are required.")
            return
        if key and not env:
            err.update("Give the key an environment variable name (e.g. MYPROVIDER_API_KEY).")
            return
        try:
            add_provider(self.policy_path, name, kind=preset.kind, base_url=url, api_key_env=env)
            if key and env:
                set_secret(env, key)
        except Exception as exc:  # noqa: BLE001 - show any rejection
            err.update(f"Not saved: {short_error(exc)}")
            return
        self.dismiss(name)


class ModelPickerScreen(Screen):
    """Fetch a provider's model ids, filter, pick one, choose how it is used, add the agent."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("ctrl+r", "refetch", "Fetch again")]

    def __init__(self, policy_path: Path, provider: str):
        super().__init__()
        self.policy_path, self.provider = policy_path, provider
        self.models: list[str] = []
        self.chosen: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Models of {self.provider}", id="mp-title")
        yield Input(placeholder="type to filter model ids", id="mp-filter")
        yield OptionList(id="mp-models")
        with Horizontal(id="mp-bottom"):
            with RadioSet(id="mp-use"):
                yield RadioButton("coding agent (opencode)", value=True, id="use-coder")
                yield RadioButton("text only: planner / reviewer", id="use-text")
            yield Input(placeholder="agent name", id="mp-agent")
            yield Button("Add agent", id="mp-add", variant="success")
        yield Static("", id="mp-status")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refetch()

    def action_refetch(self) -> None:
        self.run_worker(self._fetch, exclusive=True, group="fetch")

    async def _fetch(self) -> None:
        status = self.query_one("#mp-status", Static)
        pol = load_policy(self.policy_path)
        prov = pol.providers[self.provider]
        status.update(f"fetching {prov.base_url}/models ...")
        if prov.api_key_env and not key_for(prov):
            status.update(f"No API key: set {prov.api_key_env}, or add the provider again with the key.")
            return
        try:
            self.models = await asyncio.to_thread(fetch_models, prov, key_for(prov))
        except ProviderError as exc:
            status.update(f"Could not fetch models: {exc}")
            return
        self._show()
        status.update(f"{len(self.models)} models. Pick one (Enter), choose how to use it, then Add agent.")
        self.query_one("#mp-filter", Input).focus()

    def _show(self) -> None:
        text = self.query_one("#mp-filter", Input).value.strip().lower()
        ol = self.query_one("#mp-models", OptionList)
        ol.clear_options()
        ol.add_options([Option(m, id=m) for m in self.models if text in m.lower()][:500])

    @on(Input.Changed, "#mp-filter")
    def _filter(self) -> None:
        self._show()

    @on(Input.Submitted, "#mp-filter")
    def _to_list(self) -> None:
        self.query_one("#mp-models", OptionList).focus()

    @on(OptionList.OptionSelected, "#mp-models")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        from multigenz.policy_edit import agent_name_for

        self.chosen = str(event.option.id)
        taken = set(load_policy(self.policy_path).agents)
        self.query_one("#mp-agent", Input).value = agent_name_for(self.provider, self.chosen, taken)
        self.query_one("#mp-status", Static).update(f"selected {self.chosen}")
        self.query_one("#mp-add", Button).focus()

    @on(Button.Pressed, "#mp-add")
    def _add(self) -> None:
        from multigenz.policy_edit import add_provider_agent, short_error

        status = self.query_one("#mp-status", Static)
        if not self.chosen:
            status.update("Pick a model first (Enter on a model id).")
            return
        use = "text" if self.query_one("#use-text", RadioButton).value else "coder"
        name = self.query_one("#mp-agent", Input).value.strip()
        try:
            add_provider_agent(self.policy_path, name, provider=self.provider, model=self.chosen, use=use)
        except Exception as exc:  # noqa: BLE001
            status.update(f"Not added: {short_error(exc)}")
            return
        self.app.notify(f"Added {name} ({self.provider} {self.chosen}). Give it a role on the Team screen.")
        self.app.pop_screen()


class ProvidersScreen(Screen):
    BINDINGS = [
        Binding("n", "new", "Add provider"),
        Binding("f", "models", "Models (or Enter)"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, policy_path: Path):
        super().__init__()
        self.policy_path = Path(policy_path)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("API providers. n: add a provider   Enter/f: fetch its models and add an agent from one   "
                     "Esc: back to the team", id="prov-intro")
        yield DataTable(id="prov-table", cursor_type="row")
        yield Static("", id="prov-status")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#prov-table", DataTable)
        t.add_columns("provider", "kind", "endpoint", "key", "agents")
        self.refresh_table()
        t.focus()

    def on_screen_resume(self) -> None:
        self.refresh_table()

    def refresh_table(self) -> None:
        t = self.query_one("#prov-table", DataTable)
        t.clear()
        try:
            pol = load_policy(self.policy_path)
        except Exception as exc:  # noqa: BLE001
            self.query_one("#prov-status", Static).update(f"Policy problem: {exc}")
            return
        for name, prov in pol.providers.items():
            key = "not needed" if not prov.api_key_env else (
                f"{prov.api_key_env}: {'set' if key_for(prov) else 'MISSING'}")
            users = ", ".join(f"{a} ({c.model})" for a, c in pol.agents.items() if c.provider == name) or "-"
            t.add_row(name, prov.kind, prov.base_url, key, users, key=name)
        if not pol.providers:
            self.query_one("#prov-status", Static).update("No providers yet: press n to add one.")

    def _selected(self) -> str | None:
        t = self.query_one("#prov-table", DataTable)
        if t.row_count == 0:
            return None
        return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    def action_new(self) -> None:
        taken = set(load_policy(self.policy_path).providers)

        def done(name: str | None) -> None:
            self.refresh_table()
            if name:
                self.app.push_screen(ModelPickerScreen(self.policy_path, name))

        self.app.push_screen(AddProviderScreen(self.policy_path, taken), done)

    def action_models(self) -> None:
        name = self._selected()
        if name:
            self.app.push_screen(ModelPickerScreen(self.policy_path, name))

    @on(DataTable.RowSelected, "#prov-table")
    def _row(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(ModelPickerScreen(self.policy_path, str(event.row_key.value)))
