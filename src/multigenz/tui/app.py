"""Textual TUI: interactive home (pet, team, prompt, slash commands), run history, run dashboard with live events,
redacted log viewer, doctor, team, models and API providers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

from multigenz.core.redaction import Redactor
from multigenz.core.runs import open_store
from multigenz.tui import data

REFRESH_SECONDS = 1.0
PANE_REFRESH_SECONDS = 0.5  # the run dashboard tails the agents' logs this often


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.question, id="question")
            with Horizontal():
                yield Button("Yes (y)", id="yes", variant="error")
                yield Button("No (n)", id="no", variant="primary")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)


class LogScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

    def __init__(self, run_id: str, task_id: str):
        super().__init__()
        self.run_id, self.task_id = run_id, task_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Logs (redacted) - run {self.run_id} task {self.task_id}", id="log-title")
        yield RichLog(id="log", wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        store = open_store(self.app.home, readonly=True)  # type: ignore[attr-defined]
        try:
            blocks = data.attempt_logs(store, self.run_id, self.task_id, self.app.redactor)  # type: ignore[attr-defined]
        finally:
            store.close()
        if not blocks:
            log.write("(no logs yet)")
        for header, text in blocks:
            log.write(f"=== {header}")
            for line in text.splitlines():
                log.write(line)


class RunScreen(Screen):
    """A run's dashboard: header, task table, a live pane per working agent (split view), and the event log."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("v", "split", "Split view"),
        Binding("l", "logs", "Logs"),
        Binding("c", "cancel", "Cancel run"),
        Binding("a", "audit", "Audit"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id
        self._seq = 0
        self.split = True

    def compose(self) -> ComposeResult:
        from multigenz.tui.panes import AgentGrid

        pol = getattr(self.app, "policy", None)
        models = {n: a.model for n, a in pol.agents.items()} if pol is not None else {}
        yield Header()
        yield Static("", id="run-header")
        yield DataTable(id="tasks", cursor_type="row")
        yield AgentGrid(self.app.redactor, models, id="agents")  # type: ignore[attr-defined]
        yield Static("", id="agents-note")
        yield RichLog(id="events", wrap=True, highlight=False, markup=False, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#tasks", DataTable)
        t.add_columns("task", "status", "provider", "attempts", "validation", "note")
        self.refresh_data()
        self.set_interval(PANE_REFRESH_SECONDS, self.refresh_data)

    def action_split(self) -> None:
        self.split = not self.split
        self.refresh_data()
        self.app.notify("Split view " + ("on: a live pane per working agent" if self.split else "off"))

    def action_refresh(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        from multigenz.tui.panes import AgentGrid, workers

        store = open_store(self.app.home, readonly=True)  # type: ignore[attr-defined]
        try:
            hdr = data.run_header(store, self.run_id)
            rows = data.tasks(store, self.run_id)
            evs = data.events(store, self.run_id, after_seq=self._seq)
            ws = workers(store, self.run_id, self.app.home)  # type: ignore[attr-defined]
        finally:
            store.close()
        grid = self.query_one("#agents", AgentGrid)
        if self.split:
            grid.sync(ws)
        else:
            grid.display = False
        working = sum(1 for w in ws if w["active"])
        note = self.query_one("#agents-note", Static)
        note.display = self.split and grid.hidden_active > 0
        note.update(f"+{grid.hidden_active} more agents working (see the task table)")
        self.set_class(bool(self.split and grid.display), "-split")
        r = hdr["run"]
        counts = " ".join(f"{k}={v}" for k, v in sorted(hdr["counts"].items()))
        cancel = "  CANCEL REQUESTED" if r.get("cancel_requested") else ""
        self.query_one("#run-header", Static).update(
            f"run {r.get('run_id')}  status={str(r.get('status')).upper()}{cancel}   "
            f"agents working: {working}\n"
            f"goal: {r.get('objective')}\nintegration: {r.get('integration_branch')}  "
            f"final: {r.get('final_commit') or '-'}\ntasks: {counts or '-'}   (pushing/deploying: disabled)")
        table = self.query_one("#tasks", DataTable)
        keep = table.cursor_row
        table.clear()
        for t in rows:
            table.add_row(t["task_id"], t["status"], t["provider"] or "-", str(t["total_attempts"]),
                          t["validation"] or "-", t["failure_reason"] or "", key=t["task_id"])
        if rows:
            table.move_cursor(row=min(keep, len(rows) - 1))
        log = self.query_one("#events", RichLog)
        for e in evs:
            log.write(data.format_event(e))
            self._seq = max(self._seq, e["seq"])

    def _selected_task(self) -> str | None:
        table = self.query_one("#tasks", DataTable)
        if table.row_count == 0:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def action_logs(self) -> None:
        tid = self._selected_task()
        if tid:
            self.app.push_screen(LogScreen(self.run_id, tid))

    def action_cancel(self) -> None:
        def done(yes: bool | None) -> None:
            if yes:
                store = open_store(self.app.home)  # type: ignore[attr-defined]
                try:
                    store.update_run(self.run_id, cancel_requested=1)
                    store.emit(self.run_id, "cancel.requested", payload={"by": "tui"})
                finally:
                    store.close()
                self.app.notify("Cancellation requested; work is preserved on the run's branches.")
                self.refresh_data()

        self.app.push_screen(ConfirmScreen(f"Cancel run {self.run_id}? Agents are stopped; committed work is kept."), done)

    def action_audit(self) -> None:
        from multigenz.core.replay import replay_run

        store = open_store(self.app.home, readonly=True)  # type: ignore[attr-defined]
        try:
            res = replay_run(store, self.run_id)
        finally:
            store.close()
        if res.ok:
            self.app.notify(f"Audit OK: {res.events} events, replay matches stored state.")
        else:
            self.app.notify("AUDIT FAILED: " + "; ".join(res.errors[:3]), severity="error")


class NewRunScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-run"):
            yield Label("New run (autonomous; integrates on a run-owned branch only)")
            yield Input(placeholder="Goal, e.g. Create a REST API with authentication and tests", id="goal")
            yield Input(placeholder="Repository path", id="repo", value=str(Path.cwd()))
            yield Input(placeholder="Plan file (optional; otherwise a planner agent is used)", id="plan")
            with Horizontal():
                yield Button("Start", id="start", variant="success")
                yield Button("Cancel", id="cancel")
            yield Static("", id="error")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#start")
    async def _start(self) -> None:
        goal = self.query_one("#goal", Input).value.strip()
        repo = self.query_one("#repo", Input).value.strip()
        plan = self.query_one("#plan", Input).value.strip() or None
        err = self.query_one("#error", Static)
        if not goal:
            err.update("A goal is required.")
            return
        if not (Path(repo) / ".git").exists() and not Path(repo).is_dir():
            err.update("Repository path does not exist.")
            return
        try:
            err.update("Creating run...")
            rid = await asyncio.to_thread(self.app.submit_run, goal, Path(repo),  # type: ignore[attr-defined]
                                          Path(plan) if plan else None)
        except Exception as exc:  # noqa: BLE001 - show any rejection (policy, plan, isolation) to the user
            err.update(f"Rejected: {exc}")
            return
        self.dismiss(rid)


class DoctorScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="checks")
        yield Footer()

    async def on_mount(self) -> None:
        t = self.query_one("#checks", DataTable)
        t.add_columns("status", "check", "detail")
        from multigenz.doctor import run_doctor

        checks = await run_doctor(self.app.policy, policy_error=self.app.policy_error)  # type: ignore[attr-defined]
        for c in checks:
            t.add_row(c.status, c.name, c.detail[:100])


class RunsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("o", "open", "Open (or Enter)"),
        Binding("n", "new", "New run"),
        Binding("t", "team", "Team (roles)"),
        Binding("d", "doctor", "Doctor"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="runs", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#runs", DataTable)
        t.add_columns("run", "status", "created", "goal")
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS * 2, self.refresh_data)
        t.focus()

    def action_refresh(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        store = open_store(self.app.home, readonly=True)  # type: ignore[attr-defined]
        try:
            rows = data.runs(store, self.app.home)  # type: ignore[attr-defined]
        finally:
            store.close()
        t = self.query_one("#runs", DataTable)
        keep = t.cursor_row
        t.clear()
        for r in rows:
            status = r["status"] + (" (orphaned)" if r["orphaned"] else "")
            t.add_row(r["run_id"], status, r["created_at"][:19], r["objective"][:70], key=r["run_id"])
        if rows:
            t.move_cursor(row=min(keep, len(rows) - 1))

    @on(DataTable.RowSelected, "#runs")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(RunScreen(str(event.row_key.value)))

    def action_open(self) -> None:
        t = self.query_one("#runs", DataTable)
        if t.row_count:
            rid = str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
            self.app.push_screen(RunScreen(rid))

    def action_new(self) -> None:
        def done(rid: str | None) -> None:
            if rid:
                self.app.notify(f"Run {rid} started in the background.")
                self.refresh_data()

        self.app.push_screen(NewRunScreen(), done)

    def action_doctor(self) -> None:
        self.app.push_screen(DoctorScreen())

    def action_back(self) -> None:
        if len(self.app.screen_stack) > 2:  # opened from the home screen (the bottom one is Textual's default)
            self.app.pop_screen()

    def action_team(self) -> None:
        from multigenz.tui.team import TeamScreen

        self.app.push_screen(TeamScreen(self.app.policy_path))  # type: ignore[attr-defined]


class MultiGenzApp(App):
    TITLE = "MultiGenz"
    CSS = """
    #confirm, #new-run, #add-agent, #add-provider, #pick-agent {
        width: 80%; height: auto; padding: 1 2; border: thick $accent; background: $surface;
    }
    ConfirmScreen, NewRunScreen, AddAgentScreen, AddProviderScreen, PickAgentScreen { align: center middle; }
    #welcome {
        height: auto; border: round $accent; border-title-color: $accent; border-title-style: bold;
        padding: 0 1; margin: 0 1;
    }
    #welcome-top { height: auto; }
    #mascot { width: 21; height: 5; margin: 1 2 0 0; }
    #home-info { width: 1fr; height: auto; margin-top: 1; }
    #home-team { height: auto; margin-top: 1; }
    #home-recent { height: auto; }
    #home-log { height: 1fr; margin: 0 1; background: $background; }
    #home-live { height: auto; max-height: 16; margin: 0 2; }
    #home-menu { height: auto; max-height: 12; margin: 0 2; border: none; border-left: tall $accent; padding: 0;
                 background: $background; }
    #home-input { margin: 0 1; border: round $accent; padding: 0 1; }
    #home-input:focus { border: round $accent-lighten-1; }
    #statusline { height: 1; margin: 0 1; }
    #pa-agents { height: auto; max-height: 16; }
    #ms-title, #ms-note { height: auto; padding: 0 1; color: $text-muted; }
    #ms-models { height: 1fr; border: solid $primary; }
    #ms-buttons { height: auto; }
    #ms-status { height: auto; padding: 0 1; color: $warning; }
    #prov-table { height: auto; max-height: 60%; }
    #prov-intro, #mp-title { height: auto; padding: 0 1; color: $text-muted; }
    #mp-models { height: 1fr; border: solid $primary; }
    #mp-bottom { height: auto; }
    #mp-bottom Input { width: 1fr; }
    #mp-status, #prov-status { height: auto; padding: 0 1; color: $warning; }
    #presets { height: auto; max-height: 12; }
    #team-intro { height: auto; padding: 0 1; color: $text-muted; }
    #matrix { height: auto; max-height: 50%; }
    #team-summary { height: auto; padding: 1 1 0 1; border-top: solid $primary; }
    #team-status { height: auto; padding: 0 1; color: $warning; }
    #run-header { height: 6; padding: 0 1; border-bottom: solid $primary; }
    #tasks { height: 40%; }
    #events { height: 1fr; border-top: solid $primary; }
    RunScreen.-split #tasks { height: auto; max-height: 25%; }
    RunScreen.-split #events { height: 7; }
    #agents { height: 1fr; grid-gutter: 0 1; padding: 0 1; }
    .agent-pane { height: 1fr; border: round $primary-darken-1; }
    .agent-pane.-active { border: round $warning; }
    .pane-title { height: 1; padding: 0 1; }
    .pane-log { height: 1fr; scrollbar-size-vertical: 1; }
    #agents-note { height: auto; padding: 0 2; color: $warning; }
    """

    def __init__(self, home: Path, policy=None, policy_error: str | None = None, policy_path: Path | None = None,
                 start: str = "home", repo: Path | None = None):
        super().__init__()
        from multigenz.config import config_policy_path, default_policy_path

        self.home = home
        self.policy = policy
        self.policy_error = policy_error
        self.policy_path = Path(policy_path or default_policy_path() or config_policy_path()).expanduser()
        self.start = start
        self.repo = Path(repo or Path.cwd())  # where goals typed on the home screen run
        self.redactor = Redactor.from_environ(os.environ)

    # -- settings that survive restarts (theme) ------------------------------------------------------------------
    @staticmethod
    def settings_path() -> Path:
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "multigenz" / "tui.json"

    def load_settings(self) -> dict:
        import json

        try:
            data = json.loads(self.settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_settings(self, **values) -> None:
        import json

        data = {**self.load_settings(), **values}
        path = self.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def on_mount(self) -> None:
        theme = self.load_settings().get("theme")
        if isinstance(theme, str) and theme in self.available_themes:
            self.theme = theme
        if self.start == "runs":
            self.push_screen(RunsScreen())
        else:
            from multigenz.tui.home import HomeScreen

            self.push_screen(HomeScreen())
        if self.start == "team":
            from multigenz.tui.team import TeamScreen

            self.push_screen(TeamScreen(self.policy_path))

    def submit_run(self, goal: str, repo: Path, plan_file: Path | None, plan=None) -> str:
        """Create the run (validating policy/plan/isolation up front) and start a detached worker. ``plan`` is an
        already validated PlanDocument (e.g. one the user confirmed on the home screen)."""
        from multigenz.contracts.task import PlanDocument
        from multigenz.core.driver import spawn_detached, start_run

        if self.policy is None:
            raise RuntimeError(f"no valid policy: {self.policy_error}")
        if plan is None and plan_file:
            plan = PlanDocument.model_validate_json(plan_file.read_text())

        async def create() -> str:
            rid, _ = await start_run(self.policy, objective=goal, repo=repo, plan=plan, home=self.home, execute=False)
            return rid

        rid = asyncio.run(create())  # called from a worker thread (see NewRunScreen._start)
        spawn_detached(self.home, rid)
        return rid
