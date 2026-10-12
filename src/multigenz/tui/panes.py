"""Split view of a run: one live pane per agent that is working (or just worked) on it, side by side.

Each pane follows one agent run - a coding/conflict attempt, or a planner/reviewer call - by tailing its log files as
they are written. The logs are redacted when written and again here, so a pane never shows more than ``runs logs``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid, Vertical
from textual.widgets import RichLog, Static

from multigenz.core.redaction import Redactor
from multigenz.storage.sqlite import Store

MAX_PANES = 4
START_TAIL_BYTES = 32 * 1024     # a pane opened late starts with this much of the log
READ_CHUNK = 256 * 1024
ACTIVE = ("leased", "running")
STATUS_STYLE = {"finished": ("✓ done", "green"), "succeeded": ("✓ done", "green"), "running": ("● working", "yellow"),
                "leased": ("● starting", "yellow"), "failed": ("✗ failed", "red"), "timed_out": ("✗ timed out", "red"),
                "cancelled": ("■ cancelled", "red"), "interrupted": ("■ interrupted", "yellow"),
                "provider_failed": ("✗ provider failed", "red")}


class LogTail:
    """Complete lines appended to a file since the last read; the file may not exist yet."""

    def __init__(self, path: Path, *, start_tail: int = START_TAIL_BYTES):
        self.path = path
        self.offset: int | None = None   # decided on the first read: near the end of what is already there
        self.partial = b""
        self.start_tail = start_tail

    def read(self) -> list[str]:
        try:
            with open(self.path, "rb") as fh:
                if self.offset is None:
                    size = os.fstat(fh.fileno()).st_size
                    self.offset = max(0, size - self.start_tail)
                    skip_first = self.offset > 0
                else:
                    skip_first = False
                fh.seek(self.offset)
                data = fh.read(READ_CHUNK)
        except FileNotFoundError:
            return []
        self.offset += len(data)
        data = self.partial + data
        *lines, self.partial = data.split(b"\n")
        if skip_first and lines:
            lines = lines[1:]  # started mid-line
        return [ln.decode("utf-8", "replace") for ln in lines]


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def elapsed(started: str | None, finished: str | None, now: datetime | None = None) -> str:
    a = _parse(started)
    if a is None:
        return ""
    b = _parse(finished) or now or datetime.now(UTC)
    secs = max(0, int((b - a).total_seconds()))
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def workers(store: Store, run_id: str, home: Path) -> list[dict]:
    """Every agent run of a run, oldest first: coding/conflict attempts and planner/reviewer calls.

    Each: id, task_id, kind, agent, status, started_at, finished_at, log_dir, active. A call's log directory is
    derived from the run's layout (never taken from the event, whose payload is redacted).
    """
    from multigenz.core.runs import RunPaths

    readonly = RunPaths(home, run_id).run_dir / "readonly"
    out: list[dict] = []
    for a in store.list_attempts(run_id):
        out.append({"id": a["attempt_id"], "task_id": a["task_id"], "kind": a["kind"], "agent": a["provider"],
                    "status": a["status"], "started_at": a["started_at"], "finished_at": a["finished_at"],
                    "log_dir": a["log_dir"], "active": a["status"] in ACTIVE, "number": a["number"]})
    calls: dict[str, dict] = {}
    for e in store.events(run_id, kinds=("agent.call.started", "agent.call.finished"), limit=100000):
        p = e["payload"]
        if e["kind"] == "agent.call.started":
            calls[e["attempt_id"]] = {"id": e["attempt_id"], "task_id": e["task_id"], "kind": p.get("mode", "call"),
                                      "agent": p.get("agent", "?"), "status": "running", "started_at": e["ts"],
                                      "finished_at": None, "log_dir": str(readonly / e["attempt_id"] / "logs"),
                                      "active": True,
                                      "number": p.get("number", 1)}
        elif e["attempt_id"] in calls:
            c = calls[e["attempt_id"]]
            c.update(status=p.get("status", "finished"), finished_at=e["ts"], active=False)
    out.extend(calls.values())
    if store.get_run(run_id) and store.get_run(run_id)["status"] in ("succeeded", "failed", "cancelled",
                                                                       "plan_rejected"):
        for w in out:  # a call cut off by a crash never announced its end
            if w["active"] and w["id"] in calls:
                w.update(active=False, status="interrupted")
    out.sort(key=lambda w: w["started_at"] or "")
    return out


def choose(all_workers: list[dict], shown: list[str], limit: int = MAX_PANES) -> list[dict]:
    """Which runs get a pane: every active one (oldest first, up to ``limit``), then the most recently started
    finished ones. Panes already on screen keep their place while they are still chosen."""
    active = [w for w in all_workers if w["active"]][:limit]
    done = [w for w in reversed(all_workers) if not w["active"]][: max(0, limit - len(active))]
    picked = {w["id"]: w for w in active + done}
    order = [i for i in shown if i in picked]
    order += [w["id"] for w in all_workers if w["id"] in picked and w["id"] not in order]
    return [picked[i] for i in order]


def render_line(line: str, redactor: Redactor, style: str = "") -> Text:
    """One log line as the terminal would show it: colours kept, other escape codes and carriage-return redraws
    dropped. If removing the escape codes reveals a secret the stored line hid, the plain redacted text is used."""
    line = line.rstrip("\r").rsplit("\r", 1)[-1]
    text = Text.from_ansi(line)
    if redactor.redact(text.plain) != text.plain:
        text = Text(redactor.redact(text.plain))
    if style:
        text.stylize(style)
    return text


class AgentPane(Vertical):
    """One agent run: a title line (task, role, agent and model, state, time) and its live output."""

    def __init__(self, worker: dict, redactor: Redactor, model: str | None = None):
        super().__init__(classes="agent-pane")
        self.worker_id = worker["id"]
        self.worker = worker
        self.model = model
        log_dir = Path(worker["log_dir"]) if worker.get("log_dir") else None
        self.redactor = redactor
        self.tails = [] if log_dir is None else [(LogTail(log_dir / "stdout.log"), redactor.stream(), ""),
                                                 (LogTail(log_dir / "stderr.log"), redactor.stream(), "dim")]
        self.lines_shown = 0

    def compose(self) -> ComposeResult:
        yield Static("", classes="pane-title")
        yield RichLog(classes="pane-log", wrap=True, markup=False, highlight=False, max_lines=500)

    def on_mount(self) -> None:
        self.update(self.worker)

    def title(self) -> Text:
        w = self.worker
        label, color = STATUS_STYLE.get(w["status"], (w["status"], ""))
        t = Text()
        t.append(f"{w['task_id']}", "bold")
        t.append(f"  {w['kind']}", "dim")
        t.append(f"  {w['agent']}", "bold cyan")
        if self.model:
            t.append(f" ({self.model})", "cyan")
        t.append(f"  {label}", color)
        t.append(f"  {elapsed(w['started_at'], w['finished_at'])}", "dim")
        return t

    def update(self, worker: dict) -> None:
        self.worker = worker
        self.query_one(".pane-title", Static).update(self.title())
        self.set_class(worker["active"], "-active")
        log = self.query_one(".pane-log", RichLog)
        for tail, stream, style in self.tails:
            for line in tail.read():
                log.write(render_line(stream.line(line), self.redactor, style))
                self.lines_shown += 1
        if self.lines_shown == 0 and not worker["active"] and not self.tails:
            log.write(Text("(no log)", style="dim"))

    def text(self) -> str:
        """Everything the pane currently shows (for tests and copy)."""
        return "\n".join("".join(seg.text for seg in line) for line in self.query_one(".pane-log", RichLog).lines)


class AgentGrid(Grid):
    """Up to MAX_PANES panes: 1, 2 or 3 side by side, 4 as a 2 x 2 grid."""

    def __init__(self, redactor: Redactor, models: dict[str, str | None] | None = None, **kw):
        super().__init__(**kw)
        self.redactor = redactor
        self.models = models or {}
        self.hidden_active = 0

    def panes(self) -> list[AgentPane]:
        return list(self.query(AgentPane))

    def sync(self, all_workers: list[dict]) -> None:
        current = {p.worker_id: p for p in self.panes()}
        chosen = choose(all_workers, list(current))
        keep = {w["id"] for w in chosen}
        for wid, pane in current.items():
            if wid not in keep:
                pane.remove()
        for w in chosen:
            if w["id"] in current:
                current[w["id"]].update(w)
            else:
                self.mount(AgentPane(w, self.redactor, self.models.get(w["agent"])))
        n = len(chosen)
        self.styles.grid_size_columns = 2 if n == 4 else max(1, min(n, 3))
        self.styles.grid_size_rows = 2 if n == 4 else 1
        self.hidden_active = max(0, sum(1 for w in all_workers if w["active"]) - MAX_PANES)
        self.display = n > 0
