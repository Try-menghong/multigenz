"""Team screen: choose which agent plans (the orchestrating agent), which ones code, review and resolve conflicts.

A grid of agents x roles. Edits stay in memory until saved; saving rewrites only the policy's ``roles:`` block and the
agents' ``enabled:`` flags, validated by the real policy loader (a ``.bak`` copy is kept).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Label, OptionList, Static
from textual.widgets.option_list import Option

from multigenz.config import ROLE_MODES, ROLE_NAMES, Policy, load_policy

ROLE_TITLES = {"planner": "planner", "worker": "worker", "reviewer": "reviewer", "conflict": "conflict"}
ROLE_EXPLAIN = {
    "planner": "Planner (orchestrating agent): turns the goal into a task graph",
    "worker": "Workers: write the code for each task",
    "reviewer": "Reviewers: veto-only second opinion on each validated change",
    "conflict": "Conflict resolvers: fix merge conflicts between tasks",
}
COLUMNS = ["agent", "on", "model", *ROLE_NAMES, "status"]
PLANNING_ORDER = ["always", "auto", "never"]
PLANNING_EXPLAIN = {
    "always": "the planner splits every goal into tasks first (one extra agent run per goal)",
    "auto": "one worker takes the goal and asks for subagents only when it is too big (no planning run)",
    "never": "one worker does the whole goal; no planning, no subagents (cheapest)",
}


def auto_members(pol: Policy, probes: dict, role: str) -> list[str]:
    """Who automatic selection would use for a role (mirrors the planner/scheduler/reviewer rules)."""
    def ok(n: str, mode: str) -> bool:
        p = probes.get(n)
        return bool(p and p.enabled and mode in p.modes)
    if role == "planner":
        return [n for n in pol.agents if ok(n, "plan") and "architecture" in (probes[n].capabilities or [])]
    if role == "worker":
        return [n for n in pol.agents if ok(n, "code")]
    if role == "conflict":
        return list(pol.roles.worker) if pol.roles.worker else [n for n in pol.agents if ok(n, "conflict")]
    if role == "reviewer":
        if not pol.validation.review_with_agent:
            return []
        return [n for n in pol.agents if ok(n, "review") and "review" in (probes[n].capabilities or [])]
    return []


class AddAgentScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, choices: list[tuple[str, str]]):
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="add-agent"):
            yield Label("Add an agent (Enter to add, Escape to cancel)")
            yield OptionList(*[Option(text, id=key) for key, text in self.choices], id="presets")

    def on_mount(self) -> None:
        self.query_one("#presets", OptionList).focus()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#presets")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class TeamScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle", "Toggle"),
        Binding("left_square_bracket", "move(-1)", "Earlier"),
        Binding("right_square_bracket", "move(1)", "Later"),
        Binding("c", "auto", "Role: automatic"),
        Binding("e", "enable", "Enable/disable"),
        Binding("p", "planning", "Planning mode"),
        Binding("a", "add", "Add agent"),
        Binding("m", "model", "Model"),
        Binding("k", "providers", "API providers"),
        Binding("s", "save", "Save"),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, policy_path: Path):
        super().__init__()
        self.policy_path = Path(policy_path)
        self.pol: Policy | None = None
        self.probes: dict = {}
        self.roles: dict[str, list[str]] = {r: [] for r in ROLE_NAMES}
        self.enabled: dict[str, bool] = {}
        self.planning = "always"
        self.dirty = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="team-intro")
        yield DataTable(id="matrix", cursor_type="cell", zebra_stripes=True)
        yield Static("", id="team-summary")
        yield Static("", id="team-status")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#matrix", DataTable)
        for c in COLUMNS:
            t.add_column(ROLE_TITLES.get(c, c), key=c)
        self.load()
        t.focus()

    # -- data ---------------------------------------------------------------------------------------------------
    def load(self, probe: bool = True) -> None:
        intro = self.query_one("#team-intro", Static)
        if not self.policy_path.is_file():
            self.pol = None
            intro.update(f"No policy file at {self.policy_path}. Quit and run `multigenz init`, or press a to add an "
                         "agent once a policy exists.")
            return
        try:
            self.pol = load_policy(self.policy_path)
        except Exception as exc:  # noqa: BLE001 - show any policy problem instead of crashing the TUI
            self.pol = None
            intro.update(f"Policy {self.policy_path} is invalid: {exc}")
            return
        self.roles = {r: list(getattr(self.pol.roles, r)) for r in ROLE_NAMES}
        self.enabled = {n: a.enabled for n, a in self.pol.agents.items()}
        self.planning = self.pol.orchestrator.planning
        self.dirty = False
        from multigenz.tui.home import tilde

        intro.update(f"{tilde(self.policy_path)}  -  space: add/remove the agent in that role   [ ]: reorder   "
                     "c: role back to automatic   e: enable/disable   m: change model   p: planning mode   "
                     "a: add agent   k: API providers/keys   s: save")
        self.redraw()
        if probe:
            self.run_worker(self.probe_all, exclusive=True, group="probe")

    async def probe_all(self) -> None:
        from multigenz.adapters.registry import build_adapters

        assert self.pol is not None
        self.set_status("checking agents (each binary's --help)...")
        adapters = build_adapters(self.pol)
        names = list(adapters)
        results = await asyncio.gather(*(adapters[n].probe() for n in names), return_exceptions=True)
        self.probes = {n: r for n, r in zip(names, results) if not isinstance(r, BaseException)}
        self.set_status("")
        self.redraw()

    # -- rendering -----------------------------------------------------------------------------------------------
    def _effective(self) -> Policy | None:
        if self.pol is None:
            return None
        data = self.pol.model_dump()
        data["roles"] = self.roles
        for n, on_ in self.enabled.items():
            data["agents"][n]["enabled"] = on_
        try:
            return Policy.model_validate(data)
        except Exception:  # noqa: BLE001 - an in-progress edit may be invalid; show the raw lists instead
            return None

    def cell(self, agent: str, role: str, eff: Policy | None) -> str:
        lst = self.roles[role]
        if agent in lst:
            return f"#{lst.index(agent) + 1}"
        p = self.probes.get(agent)
        if p is not None and ROLE_MODES[role] not in (p.modes or []):
            return "n/a"
        if not lst and eff is not None and self.probes and agent in auto_members(eff, self.probes, role) \
                and self.enabled.get(agent, True):
            return "auto"
        return "·"

    def redraw(self) -> None:
        if self.pol is None:
            return
        t = self.query_one("#matrix", DataTable)
        keep = t.cursor_coordinate
        t.clear()
        eff = self._effective()
        for n, cfg in self.pol.agents.items():
            p = self.probes.get(n)
            if p is None:
                status = "checking..." if not self.probes else "not probed"
            elif p.enabled:
                status = (p.version or "ok")[:40]
            else:
                status = (p.reason or "unavailable")[:60]
            if cfg.provider:
                status = f"{cfg.provider}: {cfg.model}" + ("" if p is None or p.enabled else f" - {status}")
            t.add_row(n, "yes" if self.enabled.get(n, True) else "no", cfg.model or "default",
                      *(self.cell(n, r, eff) for r in ROLE_NAMES), status, key=n)
        if t.row_count:
            t.cursor_coordinate = keep if keep.row < t.row_count else keep._replace(row=t.row_count - 1)
        self.query_one("#team-summary", Static).update(self.summary(eff))

    def summary(self, eff: Policy | None) -> str:
        lines = [f"Planning: {self.planning} - {PLANNING_EXPLAIN[self.planning]}"]
        for r in ROLE_NAMES:
            chosen = self.roles[r]
            if chosen:
                off = [n for n in chosen if not self.enabled.get(n, True)]
                value = " -> ".join(chosen) + (f"   (disabled: {', '.join(off)})" if off else "")
            elif r == "conflict" and self.roles["worker"]:
                value = "same as workers: " + " -> ".join(self.roles["worker"])
            elif r == "reviewer" and not (self.pol and self.pol.validation.review_with_agent):
                value = "none (agent review is off; add a reviewer to turn it on)"
            else:
                auto = auto_members(eff, self.probes, r) if (eff is not None and self.probes) else []
                value = "automatic" + (f": {', '.join(auto)}" if auto else "")
            if r == "planner" and self.planning != "always":
                value += "   (only used with planning: always, and by `multigenz plan`)"
            lines.append(f"{ROLE_EXPLAIN[r]}\n    {value}")
        return "\n".join(lines) + ("\n\nUNSAVED CHANGES (s to save)" if self.dirty else "")

    def set_status(self, text: str) -> None:
        self.query_one("#team-status", Static).update(text)

    # -- cursor ---------------------------------------------------------------------------------------------------
    def _cursor(self) -> tuple[str | None, str | None]:
        t = self.query_one("#matrix", DataTable)
        if self.pol is None or t.row_count == 0:
            return None, None
        coord = t.cursor_coordinate
        agent = str(t.coordinate_to_cell_key(coord).row_key.value)
        return agent, COLUMNS[coord.column]

    def _changed(self, message: str) -> None:
        self.dirty = True
        self.set_status(message)
        self.redraw()

    # -- actions --------------------------------------------------------------------------------------------------
    def action_toggle(self) -> None:
        agent, col = self._cursor()
        if agent is None:
            return
        if col == "model":
            self.action_model()
            return
        if col in ("agent", "on", "status"):
            self.action_enable()
            return
        lst = self.roles[col]
        if agent in lst:
            lst.remove(agent)
            self._changed(f"{agent} removed from {col}s" + ("" if lst else " (role is automatic again)"))
            return
        assert self.pol is not None
        if col in ("worker", "conflict") and self.pol.agents[agent].adapter != "cli":
            self.set_status(f"{agent} is a {self.pol.agents[agent].adapter} agent: it cannot change files, so it "
                            f"cannot be a {col}")
            return
        p = self.probes.get(agent)
        if p is not None and ROLE_MODES[col] not in (p.modes or []):
            self.set_status(f"{agent} has no verified {ROLE_MODES[col]!r} invocation, so it cannot be a {col} "
                            f"({p.reason or 'see multigenz agents list'})")
            return
        lst.append(agent)
        self._changed(f"{agent} is {col} #{len(lst)}")

    def action_move(self, delta: int) -> None:
        agent, col = self._cursor()
        if agent is None or col not in ROLE_NAMES or agent not in self.roles[col]:
            self.set_status("put the cursor on an agent's #n cell in a role column to reorder it")
            return
        lst = self.roles[col]
        i = lst.index(agent)
        j = max(0, min(len(lst) - 1, i + delta))
        if i == j:
            return
        lst[i], lst[j] = lst[j], lst[i]
        self._changed(f"{col}s: {' -> '.join(lst)}")

    def action_auto(self) -> None:
        _, col = self._cursor()
        if col not in ROLE_NAMES:
            self.set_status("put the cursor in a role column (planner, worker, reviewer, conflict)")
            return
        if self.roles[col]:
            self.roles[col] = []
            self._changed(f"{col}: automatic")

    def action_enable(self) -> None:
        agent, _ = self._cursor()
        if agent is None:
            return
        self.enabled[agent] = not self.enabled.get(agent, True)
        self._changed(f"{agent} {'enabled' if self.enabled[agent] else 'disabled'}")

    def action_planning(self) -> None:
        if self.pol is None:
            return
        self.planning = PLANNING_ORDER[(PLANNING_ORDER.index(self.planning) + 1) % len(PLANNING_ORDER)]
        self._changed(f"planning: {self.planning}")

    def action_save(self) -> None:
        from multigenz.policy_edit import save_policy_edits, short_error

        if self.pol is None:
            return
        changed = {n: on_ for n, on_ in self.enabled.items() if self.pol.agents[n].enabled != on_}
        try:
            pol, backup = save_policy_edits(
                self.policy_path, roles=self.roles, enabled=changed,
                planning=self.planning if self.planning != self.pol.orchestrator.planning else None)
        except Exception as exc:  # noqa: BLE001 - report, keep the edits so they can be fixed
            self.set_status(f"NOT SAVED: {short_error(exc)}")
            return
        self.pol = pol
        self.dirty = False
        if hasattr(self.app, "policy"):
            self.app.policy = pol  # new runs started from the TUI use the saved team
            self.app.policy_error = None
        self.app.notify(f"Saved team to {self.policy_path} (previous version: {backup.name})")
        self.set_status("saved")
        self.redraw()

    def action_add(self) -> None:
        import shutil

        from multigenz.presets import PRESETS

        if self.pol is None:
            self.set_status("no valid policy file: run `multigenz init` first")
            return
        if self.dirty:
            self.set_status("save (s) or undo your changes before adding an agent")
            return
        have = {self.pol.agents[n].command for n in self.pol.agents}
        choices = []
        for key, p in PRESETS.items():
            if p.agent in self.pol.agents or p.command in have:
                continue
            where = shutil.which(p.command)
            choices.append((key, f"{key:<9} {p.status:<14} {'found ' + where if where else 'not installed'}"))
        if not choices:
            self.set_status("every supported agent is already in the policy")
            return

        def done(key: str | None) -> None:
            from multigenz.policy_edit import short_error
            from multigenz.presets import add_preset

            if not key:
                return
            try:
                add_preset(self.policy_path, key)
            except Exception as exc:  # noqa: BLE001
                self.set_status(f"not added: {short_error(exc)}")
                return
            self.app.notify(f"Added {key}; give it a role and save.")
            self.load()

        self.app.push_screen(AddAgentScreen(choices), done)

    def action_model(self) -> None:
        from multigenz.tui.model import ModelScreen

        agent, _ = self._cursor()
        if agent is None:
            return
        if self.dirty:
            self.set_status("save (s) or undo your changes before changing a model")
            return
        self.app.push_screen(ModelScreen(self.policy_path, agent))

    def action_providers(self) -> None:
        from multigenz.tui.providers import ProvidersScreen

        if self.pol is None:
            self.set_status("no valid policy file: run `multigenz init` first")
            return
        if self.dirty:
            self.set_status("save (s) or undo your changes before adding API providers")
            return
        self.app.push_screen(ProvidersScreen(self.policy_path))

    def on_screen_resume(self) -> None:
        if not self.dirty and self.pol is not None:
            before = set(self.pol.agents)
            self.load(probe=False)  # agents may have been added from the Providers screen
            if self.pol is not None and set(self.pol.agents) != before:
                self.run_worker(self.probe_all, exclusive=True, group="probe")

    def action_back(self) -> None:
        from multigenz.tui.app import ConfirmScreen

        if not self.dirty:
            self.app.pop_screen()
            return

        def done(yes: bool | None) -> None:
            if yes:
                self.app.pop_screen()

        self.app.push_screen(ConfirmScreen("Discard unsaved team changes?"), done)
