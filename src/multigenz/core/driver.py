"""High-level run drivers shared by the CLI and the TUI."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from multigenz.config import Policy
from multigenz.contracts.task import PlanDocument
from multigenz.core.bootstrap import Services, build_services
from multigenz.core.locks import RunLock
from multigenz.core.orchestrator import Orchestrator, RunOutcome
from multigenz.core.runs import RunPaths, create_run, open_store
from multigenz.errors import MultiGenzError
from multigenz.storage.sqlite import Store


def load_run_policy(store: Store, run_id: str) -> Policy:
    row = store.get_run(run_id)
    if row is None:
        raise MultiGenzError(f"unknown run {run_id}")
    return Policy.model_validate_json(row["policy_snapshot"])


async def drive(store: Store, svc: Services, run_id: str, home: Path, *, handle_signals: bool = False,
                on_message: Callable[[str], None] | None = None, **orch_kw) -> RunOutcome:
    orch = Orchestrator(store, svc, run_id, home, **orch_kw)
    loop = asyncio.get_running_loop()
    installed = []
    if handle_signals:
        state = {"n": 0}

        def on_sig() -> None:
            state["n"] += 1
            if state["n"] == 1:
                if on_message:
                    on_message("interrupt requested: stopping agents, preserving work (resume with `runs resume`)")
                orch.request_interrupt()
            else:
                os._exit(130)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, on_sig)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        return await orch.execute()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def start_run(policy: Policy, *, objective: str, repo: Path, plan: PlanDocument | None, home: Path,
                    mode: str = "autonomous", services: Services | None = None, execute: bool = True,
                    handle_signals: bool = False, on_message: Callable[[str], None] | None = None,
                    **orch_kw) -> tuple[str, RunOutcome | None]:
    svc = services or await build_services(policy)
    store = open_store(home, svc.redactor)
    try:
        run_id = await create_run(store, svc, objective=objective, repo=repo, plan=plan, home=home, mode=mode)
        if not execute:
            return run_id, None
        outcome = await drive(store, svc, run_id, home, handle_signals=handle_signals, on_message=on_message,
                              **orch_kw)
        return run_id, outcome
    finally:
        store.close()


async def resume_run(home: Path, run_id: str, *, services: Services | None = None, handle_signals: bool = False,
                     on_message: Callable[[str], None] | None = None, **orch_kw) -> RunOutcome:
    store = open_store(home)
    try:
        policy = load_run_policy(store, run_id)
        svc = services or await build_services(policy)
        store.redactor = svc.redactor
        return await drive(store, svc, run_id, home, handle_signals=handle_signals, on_message=on_message, **orch_kw)
    finally:
        store.close()


async def cancel_run(home: Path, run_id: str, *, services: Services | None = None) -> str:
    """Request cancellation. If no process owns the run, perform the cancellation here."""
    store = open_store(home)
    try:
        row = store.get_run(run_id)
        if row is None:
            raise MultiGenzError(f"unknown run {run_id}")
        if row["status"] in ("succeeded", "failed", "cancelled", "plan_rejected"):
            return f"run already {row['status']}"
        store.update_run(run_id, cancel_requested=1)
        store.emit(run_id, "cancel.requested", payload={"by": "cli"})
        if RunLock.is_held(RunPaths(home, run_id).lock):
            return "cancellation requested; the owning orchestrator will stop agents and preserve their work"
        if row["status"] == "running":
            policy = load_run_policy(store, run_id)
            svc = services or await build_services(policy)
            store.redactor = svc.redactor
            out = await drive(store, svc, run_id, home)
            return f"run {out.status}"
        from multigenz.core.state_machine import StateMachine
        StateMachine(store).run(run_id, "cancelled", reason="cancelled while not running")
        return "run cancelled"
    finally:
        store.close()


def spawn_detached(home: Path, run_id: str, policy_path: Path | None = None) -> int:
    """Start a background worker that resumes (= executes) the run; returns its pid."""
    paths = RunPaths(home, run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    log = open(paths.run_dir / "worker.log", "ab")
    env = dict(os.environ)
    env["MULTIGENZ_HOME"] = str(home)
    proc = subprocess.Popen([sys.executable, "-m", "multigenz", "runs", "resume", run_id], stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    return proc.pid
