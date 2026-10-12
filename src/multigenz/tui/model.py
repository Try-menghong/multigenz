"""Choose the model an agent uses: the choices are asked from the tool itself where it can list them (Claude Code's
aliases, ``opencode models``, an API provider's ``/models``); any name can be typed for CLIs that cannot list theirs.

Saving first checks that the agent can actually be told the model (the CLI's help must show its model option), then
rewrites only ``agents.<name>.model`` in the policy, validated by the real loader (a ``.bak`` copy is kept).
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from multigenz.config import load_policy
from multigenz.models import ModelChoices


def model_label(cfg) -> str:
    return (f"{cfg.provider}: " if cfg.provider else "") + (cfg.model or "default")


class PickAgentScreen(ModalScreen[str | None]):
    """Which agent's model to change."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, policy_path: Path):
        super().__init__()
        self.policy_path = Path(policy_path)

    def compose(self) -> ComposeResult:
        pol = load_policy(self.policy_path)
        width = max((len(n) for n in pol.agents), default=5) + 2
        with Vertical(id="pick-agent"):
            yield Label("Change the model of which agent? (Enter to choose, Escape to cancel)")
            yield OptionList(*[Option(f"{n:<{width}} {model_label(c)}" + ("" if c.enabled else "   (disabled)"), id=n)
                               for n, c in pol.agents.items()], id="pa-agents")

    def on_mount(self) -> None:
        self.query_one("#pa-agents", OptionList).focus()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#pa-agents")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class ModelScreen(Screen[str | None]):
    """Pick (or type) a model for one agent. Dismisses with a short description of what was saved, or None."""

    BINDINGS = [
        Binding("escape", "cancel", "Back"),
        Binding("ctrl+r", "discover", "Ask the tool again"),
    ]

    def __init__(self, policy_path: Path, agent: str):
        super().__init__()
        self.policy_path, self.agent = Path(policy_path), agent
        self.choices = ModelChoices(note="asking the tool for its models...")
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="ms-title")
        yield Input(placeholder="type a model name, or type to filter the list", id="ms-input")
        yield OptionList(id="ms-models")
        with Horizontal(id="ms-buttons"):
            yield Button("Use this model", id="ms-use", variant="success")
            yield Button("CLI default", id="ms-default")
            yield Button("Cancel", id="ms-cancel")
        yield Static("", id="ms-note")
        yield Static("", id="ms-status")
        yield Footer()

    def on_mount(self) -> None:
        cfg = load_policy(self.policy_path).agents[self.agent]
        where = f"provider {cfg.provider}" if cfg.provider else (cfg.command or cfg.adapter)
        self.query_one("#ms-title", Static).update(
            f"Model for {self.agent} ({where})   now: {model_label(cfg)}\n"
            "Enter on a model in the list uses it; Enter in the box uses what you typed.")
        self.query_one("#ms-input", Input).focus()
        self.action_discover()

    def action_discover(self) -> None:
        self.run_worker(self._discover, exclusive=True, group="discover")

    async def _discover(self) -> None:
        from multigenz.models import model_choices

        self.query_one("#ms-note", Static).update("asking the tool for its models...")
        pol = load_policy(self.policy_path)
        self.choices = await model_choices(self.agent, pol.agents[self.agent], pol)
        self._show()
        note = self.choices.note
        if self.choices.free_text:
            note += ("   You can also type any model name." if self.choices.models else "")
        self.query_one("#ms-note", Static).update(note)

    def _show(self) -> None:
        text = self.query_one("#ms-input", Input).value.strip().lower()
        current = load_policy(self.policy_path).agents[self.agent].model
        ol = self.query_one("#ms-models", OptionList)
        ol.clear_options()
        ol.add_options([Option(("* " if m == current else "  ") + m, id=m)
                        for m in self.choices.models if text in m.lower()][:500])

    @on(Input.Changed, "#ms-input")
    def _filter(self) -> None:
        self._show()

    @on(Input.Submitted, "#ms-input")
    def _typed(self) -> None:
        text = self.query_one("#ms-input", Input).value.strip()
        ol = self.query_one("#ms-models", OptionList)
        if not text:
            ol.focus()
            return
        exact = [m for m in self.choices.models if m == text]
        if exact or self.choices.free_text:
            self.apply(text)
        elif ol.option_count == 1:
            self.apply(str(ol.get_option_at_index(0).id))
        else:
            self.set_status(f"{text!r} is not one of the models served; pick one from the list")
            ol.focus()

    @on(OptionList.OptionSelected, "#ms-models")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.apply(str(event.option.id))

    @on(Button.Pressed, "#ms-use")
    def _use(self) -> None:
        self._typed()

    @on(Button.Pressed, "#ms-default")
    def _default(self) -> None:
        self.action_use_default()

    @on(Button.Pressed, "#ms-cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_use_default(self) -> None:
        self.apply(None)

    def set_status(self, text: str) -> None:
        self.query_one("#ms-status", Static).update(text)

    def apply(self, model: str | None) -> None:
        if self.busy:
            return
        self.busy = True
        self.set_status(f"checking that {self.agent} accepts " + (repr(model) if model else "its default") + " ...")
        self.run_worker(partial(self._apply, model), exclusive=True, group="apply")

    async def _apply(self, model: str | None) -> None:
        from multigenz.models import set_model

        try:
            check, saved = await set_model(self.policy_path, self.agent, model)
            if saved is None:
                self.set_status(f"NOT SAVED: {check.message}")
                return
            if hasattr(self.app, "policy"):
                self.app.policy = saved  # runs started from the TUI use the new model
                self.app.policy_error = None
            self.app.notify(check.message)
            self.dismiss(f"{self.agent}: {model or 'default'}")
        finally:
            self.busy = False
