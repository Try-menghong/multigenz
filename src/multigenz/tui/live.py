"""Inline progress of the runs started from the home screen, Claude Code style: a spinner line with the elapsed time
and a checklist of the run's tasks with the agent on each, updated from the run database while you keep typing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text

from multigenz.tui.panes import elapsed

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
VERBS = {"created": "Starting", "planning": "Planning", "running": "Working"}
TERMINAL_RUN = ("succeeded", "failed", "cancelled", "plan_rejected")
TASK_MARK = {"succeeded": ("✓", "green"), "failed": ("✗", "red"), "blocked": ("✗", "red"),
             "cancelled": ("■", "red"), "running": ("●", "yellow"), "validating": ("●", "cyan"),
             "split": ("↳", "magenta"), "ready": ("○", "dim"), "pending": ("○", "dim")}
TASK_WORD = {"running": "working", "validating": "validating", "ready": "waiting", "pending": "waiting for deps",
             "split": "split into subtasks"}


def snapshot(home: Path, run_id: str) -> dict | None:
    """The run row, its tasks and the number of agents working, read once (None if the run is unknown)."""
    from multigenz.core.runs import open_store
    from multigenz.tui import data

    store = open_store(home, readonly=True)
    try:
        run = store.get_run(run_id)
        if run is None:
            return None
        return {"run": run, "tasks": data.tasks(store, run_id), "working": len(store.active_attempts(run_id))}
    finally:
        store.close()


def render(snap: dict, frame: int, now: datetime | None = None) -> Text:
    run, tasks = snap["run"], snap["tasks"]
    status = run["status"]
    done = sum(1 for t in tasks if t["status"] in ("succeeded", "split"))
    out = Text()
    out.append(f"{SPINNER[frame % len(SPINNER)]} ", "bold magenta")
    out.append(f"{VERBS.get(status, status.capitalize())}… ", "bold")
    out.append(f"{elapsed(run['created_at'], None, now or datetime.now(UTC))}", "dim")
    out.append(f" · run {run['run_id']}", "dim")
    if tasks:
        out.append(f" · {done} of {len(tasks)} tasks done", "dim")
    if snap["working"]:
        out.append(f" · {snap['working']} agent{'s' if snap['working'] != 1 else ''} working", "dim")
    out.append(" · esc cancels · ctrl+o dashboard", "dim")
    for t in tasks[:12]:
        mark, color = TASK_MARK.get(t["status"], ("○", "dim"))
        out.append(f"\n  {mark} ", color)
        out.append(f"{t['task_id']:<18}", "bold" if t["status"] in ("running", "validating") else "")
        if t.get("provider"):
            out.append(f" {t['provider']:<12}", "cyan")
        out.append(f" {TASK_WORD.get(t['status'], '')}", "dim")
        if t["status"] in ("failed", "blocked") and t.get("failure_reason"):
            out.append(f" {t['failure_reason'][:80]}", "red")
    if len(tasks) > 12:
        out.append(f"\n  … and {len(tasks) - 12} more (ctrl+o)", "dim")
    return out


def summary(snap: dict, home: Path) -> Text:
    """What is written to the log when a run ends."""
    from multigenz.core.runs import RunPaths

    run, tasks = snap["run"], snap["tasks"]
    status = run["status"]
    ok = status == "succeeded"
    out = Text()
    out.append("✓ " if ok else "✗ ", "bold green" if ok else "bold red")
    out.append(f"Run {run['run_id']} {status}", "bold")
    out.append(f" after {elapsed(run['created_at'], run.get('updated_at'))}", "dim")
    good = [t["task_id"] for t in tasks if t["status"] == "succeeded"]
    if good:
        out.append(f"\n  integrated: {', '.join(good)}")
    for t in tasks:
        if t["status"] in ("failed", "blocked", "cancelled"):
            out.append(f"\n  {t['task_id']}: {t['status']}", "red")
            if t.get("failure_reason"):
                out.append(f" - {t['failure_reason'][:160]}", "red")
    if run.get("final_commit"):
        out.append(f"\n  branch {run['integration_branch']} at {run['final_commit'][:12]}")
        out.append(f"\n  review: git log {run['integration_branch']}  ·  merge it yourself when you are happy", "dim")
    report = RunPaths(home, run["run_id"]).report_md
    if report.exists():
        from multigenz.tui.home import tilde

        out.append(f"\n  report: {tilde(report)}", "dim")
    return out
