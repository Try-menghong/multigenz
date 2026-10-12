"""The interactive home screen (what ``multigenz`` opens on a terminal): the pet, the team and its models, a prompt.

Type a goal and press Enter to start a run in the current repository (agents work on run-owned branches; the working
tree is never touched). Type ``/`` for commands: /model, /team, /providers, /planning, /repo, /runs, /doctor, ...
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from functools import partial
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Input, RichLog, Static

from multigenz import __version__
from multigenz.config import ROLE_NAMES, Policy, load_policy
from multigenz.tui.mascot import mascot

COMMANDS: dict[str, str] = {
    "/model": "change the model an agent uses: /model [agent] [model|default]",
    "/team": "choose who plans, codes, reviews and resolves conflicts",
    "/agents": "list the agents, their models and whether they work here",
    "/providers": "API providers: store a key, fetch the models, add an agent",
    "/planning": "show or set planning: /planning always|auto|never",
    "/repo": "show or change the repository goals run in: /repo PATH",
    "/runs": "run history and live run dashboards",
    "/doctor": "check git, the sandbox and the agents",
    "/init": "create a starter policy with the agents installed here",
    "/help": "show this list",
    "/clear": "clear the messages",
    "/quit": "leave MultiGenz (runs keep going in the background)",
}
ALIASES = {"/exit": "/quit", "/q": "/quit", "/?": "/help", "/models": "/model", "/roles": "/team"}
PLANNING_MODES = {
    "always": "a planner agent splits every goal into tasks first",
    "auto": "one worker takes the goal and asks for subagents only when needed",
    "never": "one worker does the whole goal (cheapest)",
}


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
            value += "  (used only with planning: always)"
        rows.append((titles[role], value))
    on = [agent_label(pol, n) for n, c in pol.agents.items() if c.enabled]
    off = [n for n, c in pol.agents.items() if not c.enabled]
    rows.append(("agents", " · ".join(on) if on else "none enabled (/team, or /init)"))
    if off:
        rows.append(("disabled", ", ".join(off)))
    return rows


def hints_for(value: str, pol: Policy | None) -> list[tuple[str, str]]:
    """What to show under the prompt while a command is being typed."""
    if not value.startswith("/"):
        return []
    head, sep, rest = value.partition(" ")
    if not sep:
        return [(c, d) for c, d in COMMANDS.items() if c.startswith(head.lower())]
    cmd = ALIASES.get(head.lower(), head.lower())
    if cmd == "/model" and pol is not None and " " not in rest.strip():
        return [(n, agent_label(pol, n).split(" ", 1)[1]) for n in pol.agents if n.startswith(rest.strip())]
    if cmd == "/planning":
        return [(m, d) for m, d in PLANNING_MODES.items() if m.startswith(rest.strip())]
    return []


def complete(value: str, pol: Policy | None) -> str:
    """Tab completion: a command name, then (for /model and /planning) an agent or a mode."""
    options = [h for h, _ in hints_for(value, pol)]
    if not options:
        return value
    head, sep, rest = value.partition(" ")
    typed = rest.strip() if sep else head
    prefix = os.path.commonprefix(options) if len(options) > 1 else options[0] + " "
    if len(prefix.rstrip()) < len(typed):
        return value
    return (f"{head} {prefix}" if sep else prefix)


class HomeScreen(Screen):
    BINDINGS = [
        Binding("ctrl+t", "command('/team')", "Team"),
        Binding("ctrl+r", "command('/runs')", "Runs"),
        Binding("ctrl+l", "command('/clear')", "Clear"),
        Binding("tab", "complete", "Complete", show=False, priority=True),
        Binding("up", "history(-1)", "Previous", show=False),
        Binding("down", "history(1)", "Next", show=False),
        Binding("escape", "clear_input", "Clear input", show=False),
    ]
    BLINK_EVERY = 4.0

    def __init__(self) -> None:
        super().__init__()
        self.history: list[str] = []
        self.hist_pos = 0

    # -- layout ---------------------------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="welcome"):
            with Horizontal(id="welcome-top"):
                yield Static(mascot(), id="mascot")
                yield Static("", id="home-info")
            yield Static("", id="home-team")
            yield Static("", id="home-recent")
        yield RichLog(id="home-log", wrap=True, markup=False, highlight=False)
        yield Static("", id="home-hints")
        yield Input(placeholder="Type a goal and press Enter, e.g. Add a /health endpoint with tests   ·   "
                                "/ for commands", id="home-input",
                    suggester=SuggestFromList([*COMMANDS, *ALIASES], case_sensitive=False))
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#welcome", Vertical).border_title = "MultiGenz"
        self.query_one("#home-hints", Static).display = False
        self.refresh_info()
        tip = Text()
        tip.append("Type what you want built and press Enter. ", "bold")
        tip.append("The team plans if needed, codes, tests and merges it on a run-owned branch; your working tree is "
                   "never touched.\n")
        tip.append("/ shows commands · /model changes a model · /team picks who plans, codes and reviews · "
                   "↑↓ history · ctrl+q quits", "dim")
        self.log_widget.write(tip)
        self.set_interval(self.BLINK_EVERY, self.blink)
        self.query_one("#home-input", Input).focus()

    def on_screen_resume(self) -> None:
        self.refresh_info()  # the team, a model or the planning mode may have changed on another screen
        self.query_one("#home-input", Input).focus()

    @property
    def log_widget(self) -> RichLog:
        return self.query_one("#home-log", RichLog)

    def say(self, text: str, style: str = "") -> None:
        self.log_widget.write(Text(text, style=style))

    def blink(self) -> None:
        pet = self.query_one("#mascot", Static)
        pet.update(mascot("blink"))
        self.set_timer(0.18, lambda: pet.update(mascot()))

    # -- state ----------------------------------------------------------------------------------------------------
    @property
    def policy_path(self) -> Path:
        return Path(self.app.policy_path)  # type: ignore[attr-defined]

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
        box.display = bool(recent.plain)

    def recent_runs(self, limit: int = 3) -> Text:
        from multigenz.core.runs import open_store

        out = Text()
        try:
            store = open_store(self.app.home, readonly=True)  # type: ignore[attr-defined]
            try:
                runs = store.list_runs(limit)
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - no state yet is fine on a first start
            return out
        if runs:
            out.append("recent runs (/runs)\n", "dim")
        for r in runs:
            color = {"succeeded": "green", "failed": "red", "running": "yellow"}.get(r["status"], "")
            out.append(f"  {r['run_id']}  ")
            out.append(f"{r['status']:<10}", color)
            out.append(f"  {r['objective'][:70]}\n")
        return out

    # -- prompt ---------------------------------------------------------------------------------------------------
    @on(Input.Changed, "#home-input")
    def _typing(self, event: Input.Changed) -> None:
        hints = hints_for(event.value, self.app.policy)  # type: ignore[attr-defined]
        box = self.query_one("#home-hints", Static)
        box.display = bool(hints)
        if hints:
            width = max(len(h) for h, _ in hints) + 2
            text = Text()
            for i, (h, d) in enumerate(hints[:12]):
                text.append(("\n" if i else "") + f"  {h:<{width}}", "bold cyan")
                text.append(d, "dim")
            box.update(text)

    @on(Input.Submitted, "#home-input")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        self.hist_pos = len(self.history)
        if text.startswith("/"):
            self.run_command(text)
        else:
            self.start_goal(text)

    def action_complete(self) -> None:
        inp = self.query_one("#home-input", Input)
        if self.focused is inp and inp.value.startswith("/"):
            inp.value = complete(inp.value, self.app.policy)  # type: ignore[attr-defined]
            inp.cursor_position = len(inp.value)
        else:
            self.focus_next()

    def action_history(self, delta: int) -> None:
        inp = self.query_one("#home-input", Input)
        if not self.history or self.focused is not inp:
            return
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + delta))
        inp.value = self.history[self.hist_pos] if self.hist_pos < len(self.history) else ""
        inp.cursor_position = len(inp.value)

    def action_clear_input(self) -> None:
        self.query_one("#home-input", Input).value = ""

    def action_command(self, line: str) -> None:
        self.run_command(line)

    # -- goals ----------------------------------------------------------------------------------------------------
    def start_goal(self, goal: str) -> None:
        self.say(f"> {goal}", "bold")
        if self.app.policy is None:  # type: ignore[attr-defined]
            self.say(f"Cannot start: the policy is invalid ({self.app.policy_error}).", "red")  # type: ignore[attr-defined]
            return
        repo, _ = git_info(self.app.repo)  # type: ignore[attr-defined]
        if repo is None:
            self.say(f"Cannot start: {self.app.repo} is not a git repository. Use /repo PATH.", "red")  # type: ignore[attr-defined]
            return
        self.say(f"Starting a run in {repo} ...", "dim")
        self.run_worker(partial(self._submit, goal, repo), group="submit")

    async def _submit(self, goal: str, repo: Path) -> None:
        from multigenz.execution.worktree import integration_branch
        from multigenz.policy_edit import short_error
        from multigenz.tui.app import RunScreen

        try:
            rid = await asyncio.to_thread(self.app.submit_run, goal, repo, None)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - every rejection (policy, isolation, repo) is shown
            self.say(f"Rejected: {short_error(exc)}", "red")
            return
        self.say(f"● run {rid} started on branch {integration_branch(rid)}. Opening its dashboard "
                 "(Esc comes back here; the run keeps going).", "green")
        self.refresh_info()
        self.app.push_screen(RunScreen(rid))

    # -- commands -------------------------------------------------------------------------------------------------
    def run_command(self, line: str) -> None:
        parts = line.split()
        name = ALIASES.get(parts[0].lower(), parts[0].lower())
        if name not in COMMANDS:
            self.say(f"Unknown command {parts[0]}. /help lists the commands.", "yellow")
            return
        if name != "/clear":
            self.say(f"> {line}", "bold")
        getattr(self, "cmd_" + name[1:])(parts[1:])

    def _need_file(self) -> bool:
        if self.policy_path.is_file():
            return True
        self.say(f"No policy file at {self.policy_path} yet: /init creates one.", "yellow")
        return False

    def cmd_help(self, args: list[str]) -> None:
        text = Text()
        for i, (c, d) in enumerate(COMMANDS.items()):
            text.append(("\n" if i else "") + f"  {c:<11}", "bold cyan")
            text.append(d)
        text.append("\n  anything else is a goal: press Enter to start a run", "dim")
        self.log_widget.write(text)

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
        self.log_widget.write(text)
