"""The interactive home screen (what ``multigenz`` opens on a terminal): the pet, the team and its models, a prompt.

Type a goal and press Enter: a run starts in the current repository (agents work on run-owned branches; the working
tree is never touched) and its progress is shown right here, a spinner and a checklist of its tasks, until it ends.
Shift+Tab switches to *plan first* (see the plan, then confirm). ``/`` opens the command menu, ``@`` mentions a file,
``#`` adds a standing note for this repository, ``?`` lists the shortcuts.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from multigenz import __version__
from multigenz.config import ROLE_NAMES, Policy, load_policy
from multigenz.tui.complete import PLANNING_MODES, Item, common_prefix, completions, repo_files
from multigenz.tui.mascot import mascot
from multigenz.tui.prompt import PromptArea

COMMANDS: dict[str, str] = {
    "/model": "change the model an agent uses: /model [agent] [model|default]",
    "/team": "choose who plans, codes, reviews and resolves conflicts",
    "/plan": "plan a goal and run it only after you confirm: /plan GOAL",
    "/agents": "list the agents, their models and whether they work here",
    "/providers": "API providers: store a key, fetch the models, add an agent",
    "/planning": "show or set planning: /planning always|auto|never",
    "/memory": "standing notes added to every goal here: /memory [add TEXT|remove N|clear]",
    "/status": "version, repository, policy, sandbox, agents and runs",
    "/cost": "what recent runs cost (API agents; CLI subscriptions are not metered)",
    "/runs": "run history",
    "/open": "open a run's dashboard with a live pane per agent: /open [RUN]",
    "/resume": "continue an interrupted run: /resume [RUN]",
    "/cancel": "cancel a run (work done so far is kept): /cancel [RUN]",
    "/repo": "show or change the repository goals run in: /repo PATH",
    "/doctor": "check git, the sandbox and the agents",
    "/init": "create a starter policy with the agents installed here",
    "/theme": "switch the colour theme: /theme [NAME]",
    "/export": "save this session's messages to a file and the clipboard: /export [PATH]",
    "/help": "show this list",
    "/clear": "clear the messages",
    "/quit": "leave MultiGenz (runs keep going in the background)",
}
ALIASES = {"/exit": "/quit", "/q": "/quit", "/?": "/help", "/models": "/model", "/roles": "/team",
           "/dashboard": "/open", "/notes": "/memory"}
MODES = {"run": ("▶ run", "a goal starts a run right away"),
         "plan": ("⏸ plan first", "a goal is planned first; nothing runs until you confirm")}
SHORTCUTS = [
    ("Enter", "send"),
    ("\\ Enter  or  Ctrl+J", "new line (Shift+Enter / Alt+Enter too, where the terminal sends them)"),
    ("Shift+Tab", "switch run / plan first"), ("/", "commands"), ("@", "mention a file of the repository"),
    ("# note", "standing note for this repository"), ("↑ ↓", "history (or move in the menu)"),
    ("Tab", "complete"), ("Esc", "close the menu · clear the prompt · cancel the running run"),
    ("Ctrl+O", "dashboard of the current run (split view)"), ("Ctrl+T", "team"), ("Ctrl+R", "runs"),
    ("Ctrl+L", "clear the messages"), ("Ctrl+Q", "quit"),
]


def git_info(path: Path) -> tuple[Path | None, str | None]:
    """(top-level directory, branch) of the git repository containing ``path``; (None, None) if there is none."""
    def git(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None

    top = git("rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0 or not top.stdout.strip():
        return None, None
    br = git("symbolic-ref", "--short", "-q", "HEAD")
    branch = br.stdout.strip() if br is not None and br.returncode == 0 and br.stdout.strip() else "detached HEAD"
    return Path(top.stdout.strip()), branch


def tilde(path: Path | str) -> str:
    """``/home/me/x`` as ``~/x``."""
    home = os.path.expanduser("~")
    text = str(path)
    return "~" + text[len(home):] if home not in ("", "/") and (text == home or text.startswith(home + "/")) else text


def agent_label(pol: Policy, name: str) -> str:
    cfg = pol.agents[name]
    model = (f"{cfg.provider}: " if cfg.provider else "") + (cfg.model or "default model")
    return f"{name} ({model})"


def team_lines(pol: Policy) -> list[tuple[str, str]]:
    """(label, value) rows: planning mode, who has each role (with models), and every enabled agent."""
    rows = [("planning", f"{pol.orchestrator.planning}: {PLANNING_MODES[pol.orchestrator.planning]}")]
    titles = {"planner": "planner", "worker": "workers", "reviewer": "reviewer", "conflict": "conflicts"}
    for role in ROLE_NAMES:
        chosen = list(getattr(pol.roles, role))
        if chosen:
            value = " → ".join(agent_label(pol, n) for n in chosen)
        elif role == "conflict" and pol.roles.worker:
            value = "same as the workers"
        elif role == "reviewer" and not pol.validation.review_with_agent:
            value = "none (tests decide; add a reviewer with /team)"
        else:
            value = "automatic"
        if role == "planner" and pol.orchestrator.planning != "always":
            value += "  (used only with planning: always, and /plan)"
        rows.append((titles[role], value))
    on = [agent_label(pol, n) for n, c in pol.agents.items() if c.enabled]
    off = [n for n, c in pol.agents.items() if not c.enabled]
    rows.append(("agents", " · ".join(on) if on else "none enabled (/team, or /init)"))
    if off:
        rows.append(("disabled", ", ".join(off)))
    return rows


def plan_text(plan) -> Text:
    """A plan as a numbered list: task, title, the files it may change, what it waits for."""
    out = Text()
    out.append(f"Plan: {plan.summary or 'no summary'}", "bold")
    for i, t in enumerate(plan.tasks, 1):
        out.append(f"\n  {i}. ", "dim")
        out.append(t.task_id, "bold cyan")
        out.append(f"  {t.title}")
        out.append(f"\n     may change: {', '.join(t.allowed_paths)}", "dim")
        if t.dependencies:
            out.append(f"   after: {', '.join(t.dependencies)}", "dim")
        if t.required_capabilities:
            out.append(f"   needs: {', '.join(t.required_capabilities)}", "dim")
    return out


class HomeScreen(Screen):
    BINDINGS = [
        Binding("enter", "enter", "Send", show=False, priority=True),
        Binding("shift+enter,alt+enter,ctrl+j", "newline", "New line", show=False, priority=True),
        Binding("up", "arrow(-1)", "Previous", show=False, priority=True),
        Binding("down", "arrow(1)", "Next", show=False, priority=True),
        Binding("tab", "tab", "Complete", show=False, priority=True),
        Binding("escape", "escape", "Close / clear / cancel", show=False, priority=True),
        Binding("shift+tab", "cycle_mode", "Run / plan first", priority=True),
        Binding("ctrl+o", "dashboard", "Dashboard"),
        Binding("ctrl+t", "command('/team')", "Team"),
        Binding("ctrl+r", "command('/runs')", "Runs"),
        Binding("ctrl+l", "command('/clear')", "Clear"),
    ]
    BLINK_EVERY = 4.0
    TICK = 0.25

    def __init__(self) -> None:
        super().__init__()
        self.mode = "run"
        self.items: list[Item] = []
        self.span = (0, 0)
        self.menu_dismissed_for: str | None = None
        self.watching: list[str] = []        # runs started (or resumed) here whose progress is shown inline
        self.planning_since: datetime | None = None
        self.frame = 0
        self.files: list[str] = []
        self.files_repo: Path | None = None
        self.history = None
        self.repo_info: tuple[Path | None, str | None] = (None, None)

    # -- layout ---------------------------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="welcome"):
            with Horizontal(id="welcome-top"):
                yield Static(mascot(), id="mascot")
                yield Static("", id="home-info")
            yield Static("", id="home-team")
            yield Static("", id="home-recent")
        yield RichLog(id="home-log", wrap=True, markup=False, highlight=False)
        yield Static("", id="home-live")
        yield OptionList(id="home-menu")
        yield PromptArea(id="home-input")
        yield Static("", id="statusline")
        yield Footer()

    def on_mount(self) -> None:
        from multigenz.tui.history import PromptHistory

        self.history = PromptHistory(Path(self.app.home) / "prompt-history.jsonl",  # type: ignore[attr-defined]
                                     self.app.redactor)  # type: ignore[attr-defined]
        self.query_one("#welcome", Vertical).border_title = "MultiGenz"
        self.query_one("#home-menu", OptionList).display = False
        self.query_one("#home-live", Static).display = False
        self.query_one("#home-menu", OptionList).can_focus = False
        self.refresh_info()
        tip = Text()
        tip.append("Type what you want built and press Enter. ", "bold")
        tip.append("The team plans if needed, codes, tests and merges it on a run-owned branch; your working tree is "
                   "never touched.\n")
        tip.append("/ commands · @ files · # notes · shift+tab plan first · ? shortcuts · ctrl+q quits", "dim")
        self.log_widget.write(tip)
        self.set_interval(self.BLINK_EVERY, self.blink)
        self.set_interval(self.TICK, self.tick)
        self.prompt.focus()

    def on_screen_resume(self) -> None:
        self.refresh_info()  # the team, a model or the planning mode may have changed on another screen
        self.prompt.focus()

    @property
    def log_widget(self) -> RichLog:
        return self.query_one("#home-log", RichLog)

    @property
    def prompt(self) -> PromptArea:
        return self.query_one("#home-input", PromptArea)

    @property
    def menu(self) -> OptionList:
        return self.query_one("#home-menu", OptionList)

    def say(self, text: str | Text, style: str = "") -> None:
        self.log_widget.write(text if isinstance(text, Text) else Text(text, style=style))

    def blink(self) -> None:
        pet = self.query_one("#mascot", Static)
        pet.update(mascot("blink"))
        self.set_timer(0.18, lambda: pet.update(mascot()))

    # -- state ----------------------------------------------------------------------------------------------------
    @property
    def policy_path(self) -> Path:
        return Path(self.app.policy_path)  # type: ignore[attr-defined]

    @property
    def home(self) -> Path:
        return Path(self.app.home)  # type: ignore[attr-defined]

    def reload_policy(self) -> Policy | None:
        """Re-read the policy file if there is one (other screens save to it)."""
        if self.policy_path.is_file():
            try:
                self.app.policy = load_policy(self.policy_path)  # type: ignore[attr-defined]
                self.app.policy_error = None  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - shown in the welcome box
                from multigenz.policy_edit import short_error

                self.app.policy, self.app.policy_error = None, short_error(exc)  # type: ignore[attr-defined]
        return self.app.policy  # type: ignore[attr-defined]

    def refresh_info(self) -> None:
        pol = self.reload_policy()
        repo, branch = git_info(self.app.repo)  # type: ignore[attr-defined]
        self.repo_info = (repo, branch)
        info = Text()
        info.append("Welcome to ", "")
        info.append(f"MultiGenz {__version__}", "bold")
        info.append("  one goal, a team of coding agents\n\n", "dim")
        info.append("repo     ", "dim")
        if repo is not None:
            info.append(tilde(repo), "bold")
            info.append(f"  ({branch})\n")
        else:
            info.append(f"{self.app.repo} is not a git repository: /repo PATH chooses one\n",  # type: ignore[attr-defined]
                        "yellow")
        info.append("policy   ", "dim")
        if self.policy_path.is_file():
            info.append(f"{tilde(self.policy_path)}\n")
        else:
            info.append("built-in defaults (no policy file yet: /init creates one)\n", "yellow")
        if pol is None:
            info.append(f"POLICY PROBLEM: {self.app.policy_error}\n", "bold red")  # type: ignore[attr-defined]
        if repo is not None:
            from multigenz.notes import read_notes

            notes = read_notes(repo)
            if notes:
                info.append("notes    ", "dim")
                info.append(f"{len(notes)} standing note{'s' if len(notes) != 1 else ''} added to every goal "
                            "(/memory)\n")
        info.rstrip()
        self.query_one("#home-info", Static).update(info)
        team = Text()
        if pol is not None:
            for label, value in team_lines(pol):
                team.append(f"{label:<10}", "dim")
                team.append(value + "\n")
        team.rstrip()
        self.query_one("#home-team", Static).update(team)
        recent = self.recent_runs()
        recent.rstrip()
        box = self.query_one("#home-recent", Static)
        box.update(recent)
        box.display = bool(recent.plain) and not self.has_class("-compact")
        if repo != self.files_repo:
            self.files_repo, self.files = repo, repo_files(repo)
        self.update_statusline()

    def _runs(self, limit: int = 20) -> list[dict]:
        from multigenz.core.runs import open_store

        try:
            store = open_store(self.home, readonly=True)
            try:
                return store.list_runs(limit)
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - no state yet is fine on a first start
            return []

    def recent_runs(self, limit: int = 3) -> Text:
        out = Text()
        runs = self._runs(limit)
        if runs:
            out.append("recent runs (/runs)\n", "dim")
        for r in runs:
            color = {"succeeded": "green", "failed": "red", "running": "yellow"}.get(r["status"], "")
            out.append(f"  {r['run_id']}  ")
            out.append(f"{r['status']:<10}", color)
            out.append(f"  {r['objective'].splitlines()[0][:70] if r['objective'] else ''}\n")
        return out

    def update_statusline(self) -> None:
        pol = self.app.policy  # type: ignore[attr-defined]
        label, explain = MODES[self.mode]
        t = Text()
        t.append(f" {label}", "bold magenta" if self.mode == "plan" else "bold green")
        t.append(" (shift+tab)", "dim")
        repo, branch = self.repo_info
        if repo is not None:
            t.append(f"  ·  {tilde(repo)} ({branch})", "dim")
        if pol is not None:
            enabled = sum(1 for a in pol.agents.values() if a.enabled)
            t.append(f"  ·  planning {pol.orchestrator.planning}  ·  {enabled} agent{'s' if enabled != 1 else ''}",
                     "dim")
        if self.watching:
            t.append(f"  ·  ● {len(self.watching)} run{'s' if len(self.watching) != 1 else ''} in progress",
                     "yellow")
        t.append("  ·  ? shortcuts", "dim")
        self.query_one("#statusline", Static).update(t)

    # -- prompt and menu ------------------------------------------------------------------------------------------
    def _agents_for_menu(self) -> dict[str, str]:
        pol = self.app.policy  # type: ignore[attr-defined]
        return {} if pol is None else {n: agent_label(pol, n).split(" ", 1)[1] for n in pol.agents}

    def refresh_menu(self) -> None:
        text, cursor = self.prompt.text, self.prompt.cursor_offset
        needs_runs = text.split(" ", 1)[0].lower() in ("/resume", "/cancel", "/open") and " " in text
        start, end, items = completions(
            text, cursor, commands={**COMMANDS}, agents=self._agents_for_menu(), files=self.files,
            runs=self._runs(15) if needs_runs else None, themes=sorted(self.app.available_themes))
        self.items, self.span = items[:30], (start, end)
        key = f"{start}:{text[start:cursor]}"
        show = bool(self.items) and self.menu_dismissed_for != key and not (
            len(self.items) == 1 and text[start:end].strip() == self.items[0].insert.strip())
        menu = self.menu
        menu.clear_options()
        if show:
            width = max(len(i.label) for i in self.items) + 2
            menu.add_options([Option(Text.assemble((f"{i.label:<{width}}", "bold cyan"), (i.detail, "dim")))
                              for i in self.items])
            menu.highlighted = 0
        menu.display = show

    @property
    def menu_open(self) -> bool:
        return bool(self.menu.display) and self.menu.option_count > 0

    def accept(self, item: Item) -> None:
        start, end = self.span
        self.prompt.replace_span(start, end, item.insert)
        self.menu_dismissed_for = None
        self.refresh_menu()

    @on(TextArea.Changed, "#home-input")
    def _changed(self) -> None:
        if self.prompt.text == "?":
            self.prompt.set_text("")
            self.show_shortcuts()
            return
        self.refresh_menu()

    @on(TextArea.SelectionChanged, "#home-input")
    def _moved(self) -> None:
        if self.menu.display or self.prompt.text.startswith("/") or "@" in self.prompt.text:
            self.refresh_menu()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if not self.is_mounted or self.app.screen is not self:
            return True
        try:
            prompt_focused = self.focused is self.prompt
        except Exception:  # noqa: BLE001 - not composed yet
            return True
        if action == "arrow":
            if not prompt_focused:
                return False
            if self.menu_open:
                return True
            if parameters and parameters[0] < 0:
                return self.prompt.on_first_line and bool(self.history and self.history.entries)
            return self.prompt.on_last_line and bool(self.history and self.history.pos < len(self.history.entries))
        if action == "tab":
            return prompt_focused and (self.menu_open or bool(self.items))
        if action in ("enter", "newline"):
            return prompt_focused
        if action == "escape":
            return self.menu_open or bool(self.prompt.text) or bool(self.watching)
        return True

    def action_arrow(self, delta: int) -> None:
        if self.menu_open:
            menu = self.menu
            menu.highlighted = ((menu.highlighted or 0) + delta) % menu.option_count
            return
        if self.history is None:
            return
        value = self.history.previous(self.prompt.text) if delta < 0 else self.history.next()
        if value is not None:
            self.prompt.set_text(value)

    def action_tab(self) -> None:
        if self.menu_open:
            self.accept(self.items[self.menu.highlighted or 0])
            return
        prefix = common_prefix(self.items)
        start, end = self.span
        if prefix and len(prefix) > end - start:
            self.prompt.replace_span(start, end, prefix)

    def action_newline(self) -> None:
        self.prompt.insert("\n")

    def action_enter(self) -> None:
        if self.menu_open:
            item = self.items[self.menu.highlighted or 0]
            self.accept(item)
            if not item.final:
                return
        elif self.prompt.char_before_cursor() == "\\":   # backslash + Enter: a new line instead of sending
            offset = self.prompt.cursor_offset
            self.prompt.replace_span(offset - 1, offset, "\n")
            return
        self.submit()

    def action_escape(self) -> None:
        if self.menu_open:
            start = self.span[0]
            self.menu_dismissed_for = f"{start}:{self.prompt.text[start:self.prompt.cursor_offset]}"
            self.menu.display = False
            return
        if self.prompt.text:
            self.prompt.set_text("")
            return
        if self.watching:
            self.confirm_cancel(self.watching[-1])

    def action_cycle_mode(self) -> None:
        self.mode = "plan" if self.mode == "run" else "run"
        self.update_statusline()
        self.app.notify(f"{MODES[self.mode][0]}: {MODES[self.mode][1]}", timeout=3)

    def action_command(self, line: str) -> None:
        self.run_command(line)

    def action_dashboard(self) -> None:
        self.cmd_open([])

    def show_shortcuts(self) -> None:
        text = Text("Shortcuts", style="bold")
        width = max(len(k) for k, _ in SHORTCUTS) + 2
        for k, d in SHORTCUTS:
            text.append(f"\n  {k:<{width}}", "bold cyan")
            text.append(d)
        self.say(text)

    def compact(self) -> None:
        """Once the conversation starts, the welcome box keeps only the pet and the essentials (Claude Code's banner
        scrolls away); /status and /runs show the rest."""
        self.query_one("#home-team", Static).display = False
        self.query_one("#home-recent", Static).display = False
        self.add_class("-compact")

    def submit(self) -> None:
        text = self.prompt.text.strip()
        self.prompt.set_text("")
        self.menu.display = False
        self.menu_dismissed_for = None
        if not text:
            return
        self.compact()
        if self.history is not None:
            self.history.add(text)
        if text.startswith("/"):
            self.run_command(text)
        elif text.startswith("#"):
            self.cmd_memory(["add", text.lstrip("#").strip()])
        elif self.mode == "plan":
            self.plan_first(text)
        else:
            self.start_goal(text)

    # -- goals ----------------------------------------------------------------------------------------------------
    def _ready_repo(self, goal: str) -> Path | None:
        self.say(f"> {goal}", "bold")
        if self.app.policy is None:  # type: ignore[attr-defined]
            self.say(f"Cannot start: the policy is invalid ({self.app.policy_error}).", "red")  # type: ignore[attr-defined]
            return None
        repo, _ = git_info(self.app.repo)  # type: ignore[attr-defined]
        if repo is None:
            self.say(f"Cannot start: {self.app.repo} is not a git repository. Use /repo PATH.", "red")  # type: ignore[attr-defined]
        return repo

    def _goal_with_notes(self, goal: str, repo: Path) -> str:
        from multigenz.notes import read_notes, with_notes

        notes = read_notes(repo)
        if notes:
            self.say(f"(with {len(notes)} standing note{'s' if len(notes) != 1 else ''} from /memory)", "dim")
        return with_notes(goal, notes)

    def start_goal(self, goal: str) -> None:
        repo = self._ready_repo(goal)
        if repo is None:
            return
        self.say(f"Starting a run in {tilde(repo)} ...", "dim")
        self.run_worker(partial(self._submit, self._goal_with_notes(goal, repo), repo, None), group="submit")

    async def _submit(self, goal: str, repo: Path, plan) -> None:
        from multigenz.execution.worktree import integration_branch
        from multigenz.policy_edit import short_error

        try:
            rid = await asyncio.to_thread(self.app.submit_run, goal, repo, None, plan)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - every rejection (policy, isolation, repo) is shown
            self.say(f"Rejected: {short_error(exc)}", "red")
            return
        self.say(f"● run {rid} started on branch {integration_branch(rid)}; progress below "
                 "(ctrl+o: a live pane per agent).", "green")
        self.watch(rid)
        self.refresh_info()

    def plan_first(self, goal: str) -> None:
        repo = self._ready_repo(goal)
        if repo is None:
            return
        self.say("Planning first: nothing runs until you confirm.", "dim")
        self.planning_since = datetime.now(UTC)
        self.run_worker(partial(self._plan, self._goal_with_notes(goal, repo), repo), group="plan")

    async def _plan(self, goal: str, repo: Path) -> None:
        from multigenz.core.driver import plan_only
        from multigenz.policy_edit import short_error
        from multigenz.tui.app import ConfirmScreen

        pol = self.app.policy  # type: ignore[attr-defined]
        try:
            plan = await asyncio.to_thread(lambda: asyncio.run(plan_only(pol, goal=goal, repo=repo, home=self.home)))
        except Exception as exc:  # noqa: BLE001 - a rejected plan, no planner, ...
            self.say(f"No plan: {short_error(exc)}", "red")
            return
        finally:
            self.planning_since = None
        self.say(plan_text(plan))

        def decided(yes: bool | None) -> None:
            if yes:
                self.say(f"Running the plan ({len(plan.tasks)} tasks) ...", "dim")
                self.run_worker(partial(self._submit, goal, repo, plan), group="submit")
            else:
                self.say("Plan discarded; nothing ran.", "dim")

        self.app.push_screen(ConfirmScreen(f"Run this plan ({len(plan.tasks)} task"
                                           f"{'s' if len(plan.tasks) != 1 else ''})?"), decided)

    # -- live progress --------------------------------------------------------------------------------------------
    def watch(self, run_id: str) -> None:
        if run_id not in self.watching:
            self.watching.append(run_id)
        self.tick()

    def tick(self) -> None:
        from multigenz.tui.live import SPINNER, TERMINAL_RUN, render, snapshot, summary

        self.frame += 1
        live = self.query_one("#home-live", Static)
        parts: list[Text] = []
        if self.planning_since is not None:
            secs = int((datetime.now(UTC) - self.planning_since).total_seconds())
            parts.append(Text.assemble((f"{SPINNER[self.frame % len(SPINNER)]} ", "bold magenta"),
                                       ("Planning… ", "bold"), (f"{secs}s · the planner reads the repository", "dim")))
        for rid in list(self.watching):
            try:
                snap = snapshot(self.home, rid)
            except Exception:  # noqa: BLE001 - the database may be busy for a moment
                continue
            if snap is None:
                self.watching.remove(rid)
                continue
            if snap["run"]["status"] in TERMINAL_RUN:
                self.watching.remove(rid)
                self.say(summary(snap, self.home))
                self.app.bell()
                self.refresh_info()
                continue
            parts.append(render(snap, self.frame))
        live.display = bool(parts)
        if parts:
            live.update(Text("\n").join(parts))
        if self.frame % 8 == 0:
            self.update_statusline()

    def confirm_cancel(self, run_id: str) -> None:
        from multigenz.tui.app import ConfirmScreen

        def decided(yes: bool | None) -> None:
            if yes:
                self.run_worker(partial(self._cancel, run_id), group="cancel")

        self.app.push_screen(ConfirmScreen(f"Cancel run {run_id}? Agents are stopped; work done so far is kept."),
                             decided)

    async def _cancel(self, run_id: str) -> None:
        from multigenz.core.driver import cancel_run
        from multigenz.policy_edit import short_error

        try:
            msg = await asyncio.to_thread(lambda: asyncio.run(cancel_run(self.home, run_id)))
        except Exception as exc:  # noqa: BLE001
            self.say(f"Not cancelled: {short_error(exc)}", "red")
            return
        self.say(f"run {run_id}: {msg}", "yellow")
        self.watch(run_id)

    # -- commands -------------------------------------------------------------------------------------------------
    def run_command(self, line: str) -> None:
        parts = line.split()
        name = ALIASES.get(parts[0].lower(), parts[0].lower())
        if name not in COMMANDS:
            self.say(f"Unknown command {parts[0]}. /help lists the commands.", "yellow")
            return
        if name != "/clear":
            self.say(f"> {line}", "bold")
        if name == "/plan":
            goal = line.split(None, 1)[1].strip() if len(parts) > 1 else ""
            if not goal:
                self.say("Use /plan GOAL (or Shift+Tab: every goal is planned first).", "yellow")
                return
            self.plan_first(goal)
            return
        getattr(self, "cmd_" + name[1:])(parts[1:])

    def _need_file(self) -> bool:
        if self.policy_path.is_file():
            return True
        self.say(f"No policy file at {self.policy_path} yet: /init creates one.", "yellow")
        return False

    def _pick_run(self, args: list[str], *, active: bool) -> str | None:
        runs = self._runs(50)
        if args:
            hits = [r["run_id"] for r in runs if r["run_id"].startswith(args[0])]
            if len(hits) != 1:
                self.say(f"No single run matches {args[0]!r}." + (" /runs lists them." if not hits else ""), "yellow")
                return None
            return hits[0]
        if self.watching:
            return self.watching[-1]
        pool = [r for r in runs if r["status"] not in ("succeeded", "failed", "cancelled", "plan_rejected")] \
            if active else runs
        if not pool:
            self.say("No run to pick: give a run id (/runs lists them).", "yellow")
            return None
        return pool[0]["run_id"]

    def cmd_help(self, args: list[str]) -> None:
        text = Text()
        for i, (c, d) in enumerate(COMMANDS.items()):
            text.append(("\n" if i else "") + f"  {c:<11}", "bold cyan")
            text.append(d)
        text.append("\n  anything else is a goal: Enter starts a run (Shift+Tab: plan first) · # adds a note · "
                    "? shortcuts", "dim")
        self.say(text)

    def cmd_quit(self, args: list[str]) -> None:
        self.app.exit()

    def cmd_clear(self, args: list[str]) -> None:
        self.log_widget.clear()

    def cmd_team(self, args: list[str]) -> None:
        from multigenz.tui.team import TeamScreen

        if self._need_file():
            self.app.push_screen(TeamScreen(self.policy_path))

    def cmd_providers(self, args: list[str]) -> None:
        from multigenz.tui.providers import ProvidersScreen

        if self._need_file():
            self.app.push_screen(ProvidersScreen(self.policy_path))

    def cmd_runs(self, args: list[str]) -> None:
        from multigenz.tui.app import RunsScreen

        self.app.push_screen(RunsScreen())

    def cmd_open(self, args: list[str]) -> None:
        from multigenz.tui.app import RunScreen

        rid = self._pick_run(args, active=False)
        if rid:
            self.app.push_screen(RunScreen(rid))

    def cmd_resume(self, args: list[str]) -> None:
        from multigenz.core.driver import spawn_detached
        from multigenz.core.runs import RunPaths
        from multigenz.core.locks import RunLock

        rid = self._pick_run(args, active=True)
        if not rid:
            return
        run = next((r for r in self._runs(200) if r["run_id"] == rid), None)
        if run is None or run["status"] in ("succeeded", "failed", "cancelled", "plan_rejected"):
            self.say(f"run {rid} has ended ({run['status'] if run else 'unknown'}); nothing to resume.", "yellow")
            return
        if RunLock.is_held(RunPaths(self.home, rid).lock):
            self.say(f"run {rid} is already running; following it here.", "dim")
        else:
            spawn_detached(self.home, rid)
            self.say(f"Resuming run {rid}: leases, processes and worktrees are reconciled first; finished work is "
                     "not redone.", "green")
        self.watch(rid)

    def cmd_cancel(self, args: list[str]) -> None:
        rid = self._pick_run(args, active=True)
        if rid:
            self.confirm_cancel(rid)

    def cmd_doctor(self, args: list[str]) -> None:
        from multigenz.tui.app import DoctorScreen

        self.app.push_screen(DoctorScreen())

    def cmd_init(self, args: list[str]) -> None:
        from multigenz.presets import init_policy

        if self.policy_path.is_file():
            self.say(f"{self.policy_path} already exists; /team and /model edit it.", "yellow")
            return
        try:
            added = init_policy(self.policy_path)
        except Exception as exc:  # noqa: BLE001
            self.say(f"Not created: {exc}", "red")
            return
        self.say(f"Created {self.policy_path} with: {', '.join(added) or 'no agents (none installed)'}.", "green")
        self.say("Next: edit its `tests` profile for your project, then /doctor.", "dim")
        self.refresh_info()

    def cmd_repo(self, args: list[str]) -> None:
        if not args:
            repo, branch = git_info(self.app.repo)  # type: ignore[attr-defined]
            self.say(f"Goals run in {repo} ({branch})." if repo else f"{self.app.repo} is not a git repository.")  # type: ignore[attr-defined]
            return
        path = Path(os.path.expanduser(" ".join(args))).resolve()
        repo, branch = git_info(path)
        if repo is None:
            self.say(f"{path} is not a git repository; nothing changed.", "red")
            return
        self.app.repo = repo  # type: ignore[attr-defined]
        self.say(f"Goals now run in {repo} ({branch}).", "green")
        self.refresh_info()

    def cmd_planning(self, args: list[str]) -> None:
        from multigenz.policy_edit import save_policy_edits, short_error

        pol = self.app.policy  # type: ignore[attr-defined]
        if not args:
            if pol is not None:
                mode = pol.orchestrator.planning
                self.say(f"planning: {mode} - {PLANNING_MODES[mode]}. Set it: /planning always|auto|never")
            return
        mode = args[0].lower()
        if mode not in PLANNING_MODES:
            self.say("Use /planning always, /planning auto or /planning never.", "yellow")
            return
        if not self._need_file():
            return
        try:
            save_policy_edits(self.policy_path, planning=mode)
        except Exception as exc:  # noqa: BLE001
            self.say(f"Not saved: {short_error(exc)}", "red")
            return
        self.say(f"planning: {mode} - {PLANNING_MODES[mode]}", "green")
        self.refresh_info()

    def cmd_memory(self, args: list[str]) -> None:
        from multigenz.notes import add_note, clear_notes, notes_path, read_notes, remove_note

        repo, _ = git_info(self.app.repo)  # type: ignore[attr-defined]
        if repo is None:
            self.say("Notes belong to a repository: /repo PATH first.", "yellow")
            return
        action = args[0].lower() if args else ""
        try:
            if action == "add":
                add_note(repo, " ".join(args[1:]))
                self.say(f"Noted for {tilde(repo)}: every goal started here now includes it.", "green")
            elif action in ("remove", "rm") and len(args) == 2 and args[1].isdigit():
                remove_note(repo, int(args[1]))
                self.say(f"Removed note {args[1]}.", "green")
            elif action == "clear":
                clear_notes(repo)
                self.say("Removed every note for this repository.", "green")
            elif action:
                self.say("Use /memory, /memory add TEXT, /memory remove N or /memory clear (or start a line "
                         "with #).", "yellow")
                return
        except ValueError as exc:
            self.say(f"Not changed: {exc}", "red")
            return
        notes = read_notes(repo)
        text = Text(f"Standing notes for {tilde(repo)}", style="bold")
        text.append(f"  ({tilde(notes_path(repo))}; never written into the repository)", "dim")
        for i, n in enumerate(notes, 1):
            text.append(f"\n  {i}. ", "dim")
            text.append(n)
        if not notes:
            text.append("\n  none yet: start a line with # to add one, e.g. # use pytest, never unittest", "dim")
        self.say(text)
        self.refresh_info()

    def cmd_status(self, args: list[str]) -> None:
        import platform

        pol = self.app.policy  # type: ignore[attr-defined]
        repo, branch = git_info(self.app.repo)  # type: ignore[attr-defined]
        rows = [("version", f"MultiGenz {__version__} · Python {platform.python_version()} · "
                            f"{platform.system()} {platform.release()}"),
                ("repo", f"{tilde(repo)} ({branch})" if repo else f"{self.app.repo} (not a git repository)"),  # type: ignore[attr-defined]
                ("policy", tilde(self.policy_path) if self.policy_path.is_file() else "built-in defaults"),
                ("state", tilde(self.home)),
                ("mode", f"{MODES[self.mode][0]}: {MODES[self.mode][1]}")]
        if pol is not None:
            ex = pol.execution
            rows += [("sandbox", f"{ex.isolation} · network {ex.network}"),
                     ("planning", f"{pol.orchestrator.planning} · up to {pol.orchestrator.max_parallel_agents} agents "
                                  "at once"),
                     ("agents", ", ".join(agent_label(pol, n) for n, c in pol.agents.items() if c.enabled) or "none"),
                     ("pushing", "disabled (MultiGenz never pushes or deploys)")]
        else:
            rows.append(("policy", f"PROBLEM: {self.app.policy_error}"))  # type: ignore[attr-defined]
        active = [r for r in self._runs(50) if r["status"] in ("created", "planning", "running")]
        rows.append(("runs", f"{len(active)} not finished" + (f": {', '.join(r['run_id'] for r in active[:5])}"
                                                              if active else "")))
        text = Text()
        for i, (k, v) in enumerate(rows):
            text.append(("\n" if i else "") + f"  {k:<10}", "dim")
            text.append(v)
        self.say(text)

    def cmd_cost(self, args: list[str]) -> None:
        runs = self._runs(20)
        if not runs:
            self.say("No runs yet.", "dim")
            return
        text = Text("Recent runs", style="bold")
        total = 0.0
        unknown = 0
        for r in runs:
            spent = r.get("spent_usd") or 0.0
            total += spent
            unknown += bool(r.get("cost_unknown"))
            text.append(f"\n  {r['run_id']}  {r['status']:<13}", "dim")
            text.append(f"${spent:.4f}")
            if r.get("cost_unknown"):
                text.append(" + CLI agent time (not metered)", "dim")
        text.append(f"\n  total ${total:.4f} over {len(runs)} runs", "bold")
        if unknown:
            text.append(f"; {unknown} used CLI agents whose subscription usage MultiGenz cannot measure", "dim")
        self.say(text)

    def cmd_theme(self, args: list[str]) -> None:
        themes = sorted(self.app.available_themes)
        if not args:
            self.say(f"theme: {self.app.theme}. Others: {', '.join(t for t in themes if t != self.app.theme)}")
            return
        if args[0] not in themes:
            self.say(f"No theme {args[0]!r}. Themes: {', '.join(themes)}", "yellow")
            return
        self.app.theme = args[0]
        save = getattr(self.app, "save_settings", None)
        if save:
            save(theme=args[0])
        self.say(f"theme: {args[0]} (kept for next time)", "green")

    def cmd_export(self, args: list[str]) -> None:
        lines = ["".join(seg.text for seg in line).rstrip() for line in self.log_widget.lines]
        text = "\n".join(lines).strip() + "\n"
        text = self.app.redactor.redact(text)  # type: ignore[attr-defined]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path(os.path.expanduser(" ".join(args))) if args else self.home / "exports" / f"session-{stamp}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError as exc:
            self.say(f"Not saved: {exc}", "red")
            return
        self.app.copy_to_clipboard(text)
        self.say(f"Saved {len(lines)} lines to {tilde(path)} (and copied them to the clipboard).", "green")

    def cmd_model(self, args: list[str]) -> None:
        from multigenz.tui.model import ModelScreen, PickAgentScreen

        if not self._need_file():
            return
        pol = self.app.policy  # type: ignore[attr-defined]
        if pol is None:
            self.say(f"The policy is invalid ({self.app.policy_error}); fix it first.", "red")  # type: ignore[attr-defined]
            return
        if args and args[0] not in pol.agents:
            self.say(f"No agent {args[0]!r}. Agents: {', '.join(pol.agents)}", "yellow")
            return

        def done(result: str | None) -> None:
            if result:
                self.say(f"model saved: {result}", "green")
            self.refresh_info()

        if not args:
            def picked(agent: str | None) -> None:
                if agent:
                    self.app.push_screen(ModelScreen(self.policy_path, agent), done)

            self.app.push_screen(PickAgentScreen(self.policy_path), picked)
        elif len(args) == 1:
            self.app.push_screen(ModelScreen(self.policy_path, args[0]), done)
        else:
            model = None if args[1].lower() in ("default", "--clear") else args[1]
            self.say(f"checking that {args[0]} accepts {model or 'its default model'} ...", "dim")
            self.run_worker(partial(self._set_model, args[0], model), group="model")

    async def _set_model(self, agent: str, model: str | None) -> None:
        from multigenz.models import set_model

        check, saved = await set_model(self.policy_path, agent, model)
        if saved is None:
            self.say(f"Not saved: {check.message}", "red")
            return
        self.say(check.message, "green")
        self.refresh_info()

    def cmd_agents(self, args: list[str]) -> None:
        if self.app.policy is None:  # type: ignore[attr-defined]
            self.say("No valid policy.", "red")
            return
        self.say("checking agents (each binary's --help) ...", "dim")
        self.run_worker(self._agents, exclusive=True, group="agents")

    async def _agents(self) -> None:
        from multigenz.adapters.registry import build_adapters

        pol: Policy = self.app.policy  # type: ignore[attr-defined]
        adapters = build_adapters(pol)
        probes = await asyncio.gather(*(a.probe() for a in adapters.values()), return_exceptions=True)
        text = Text()
        width = max((len(n) for n in adapters), default=5) + 2
        for i, (name, p) in enumerate(zip(adapters, probes, strict=True)):
            text.append(("\n" if i else "") + f"  {name:<{width}}", "bold")
            text.append(f"{agent_label(pol, name).split(' ', 1)[1]:<28}")
            if isinstance(p, BaseException):
                text.append(f"error: {p}", "red")
            elif p.enabled:
                text.append("ready  ", "green")
                text.append(f"{', '.join(p.modes)}  {p.version or ''}", "dim")
            else:
                text.append(p.reason or "unavailable", "yellow")
        self.say(text)
