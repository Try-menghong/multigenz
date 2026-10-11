"""Read-side data access for the TUI (and `runs logs`). No UI imports; fully testable."""

from __future__ import annotations

import os
from pathlib import Path

from multigenz.core.locks import RunLock
from multigenz.core.redaction import Redactor
from multigenz.core.runs import RunPaths
from multigenz.storage.sqlite import Store

LOG_TAIL = 400


def runs(store: Store, home: Path, limit: int = 100) -> list[dict]:
    out = []
    for r in store.list_runs(limit):
        r["owned"] = RunLock.is_held(RunPaths(home, r["run_id"]).lock)
        r["orphaned"] = r["status"] in ("running", "planning") and not r["owned"]
        out.append(r)
    return out


def run_header(store: Store, run_id: str) -> dict:
    r = store.get_run(run_id) or {}
    tasks = store.list_tasks(run_id)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {"run": r, "counts": counts}


def tasks(store: Store, run_id: str) -> list[dict]:
    out = []
    for t in store.list_tasks(run_id):
        v = [e for e in store.events(run_id, kinds=("validation.result",), task_id=t["task_id"])]
        last = v[-1]["payload"] if v else None
        out.append({**t, "validation": None if last is None else ("pass" if last.get("passed") else "FAIL")})
    return out


def events(store: Store, run_id: str, after_seq: int = 0, limit: int = 200) -> list[dict]:
    return [e for e in store.events(run_id, after_seq=after_seq, limit=limit) if e["kind"] != "agent.output"]


def format_event(e: dict) -> str:
    p = e["payload"]
    if e["kind"] == "task.transition":
        return f"#{e['seq']} {e['task_id']}: {p.get('from')} -> {p.get('to')}  {p.get('reason', '')}"
    if e["kind"] == "run.transition":
        return f"#{e['seq']} RUN {p.get('from')} -> {p.get('to')}  {p.get('reason', '')}"
    if e["kind"] == "validation.result":
        return f"#{e['seq']} {e['task_id']}: validation {'PASS' if p.get('passed') else 'FAIL'}"
    if e["kind"] in ("attempt.started", "attempt.finished"):
        return f"#{e['seq']} {e['task_id']}: {e['kind']} {p.get('provider', '')} {p.get('status', '')} {p.get('error_code') or ''}"
    return f"#{e['seq']} {e['kind']} {e['task_id'] or ''} {str(p)[:120]}"


def attempt_logs(store: Store, run_id: str, task_id: str, redactor: Redactor | None = None) -> list[tuple[str, str]]:
    """(header, redacted text) per attempt log file. Redaction is applied again on read."""
    red = redactor or Redactor.from_environ(os.environ)
    out: list[tuple[str, str]] = []
    for a in store.list_attempts(run_id, task_id):
        d = a.get("log_dir")
        if not d:
            continue
        for name in ("stdout.log", "stderr.log"):
            p = Path(d) / name
            if not p.exists():
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
            stream = red.stream()
            out.append((f"{a['attempt_id']} #{a['number']} {a['kind']} {a['provider']} {name} [{a['status']}]",
                        "\n".join(stream.line(x) for x in lines)))
    return out
