"""MultiGenz command line interface (Typer)."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Annotated, Optional

import typer

from multigenz import __version__
from multigenz.config import Policy, default_state_home, load_policy
from multigenz.errors import MultiGenzError

app = typer.Typer(name="multigenz", help="Local-first autonomous multi-agent software-development orchestrator.",
                  no_args_is_help=True, add_completion=False)
agents_app = typer.Typer(help="Inspect and exercise agent adapters.", no_args_is_help=True)
app.add_typer(agents_app, name="agents")
runs_app = typer.Typer(help="Inspect and control runs.", no_args_is_help=True)
app.add_typer(runs_app, name="runs")

PolicyOpt = Annotated[Optional[Path], typer.Option("--policy", "-p", help="Policy YAML (default: $MULTIGENZ_POLICY, "
                                                   "~/.config/multigenz/policy.yaml, then built-in defaults).")]


def _load(policy: Path | None) -> Policy:
    try:
        return load_policy(policy)
    except MultiGenzError as exc:
        typer.secho(f"policy error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _fail(msg: str, code: int = 1) -> typer.Exit:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def doctor(policy: PolicyOpt = None, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check git, sqlite, isolation backends and agent capabilities. Exit 1 if a required check fails."""
    from multigenz.doctor import run_doctor

    pol: Policy | None = None
    err: str | None = None
    try:
        pol = load_policy(policy)
    except MultiGenzError as exc:
        err = str(exc)
    checks = asyncio.run(run_doctor(pol, policy_error=err))
    if json_out:
        typer.echo(json.dumps([c.__dict__ | {"status": c.status} for c in checks], indent=2))
    else:
        for c in checks:
            color = {"ok": typer.colors.GREEN, "FAIL": typer.colors.RED, "warn": typer.colors.YELLOW}[c.status]
            typer.echo(typer.style(f"[{c.status:>4}] ", fg=color) + f"{c.name}: {c.detail}")
    if any(c.status == "FAIL" for c in checks):
        raise typer.Exit(1)


@agents_app.command("list")
def agents_list(policy: PolicyOpt = None, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List configured agents with detected capabilities (probes the binaries' --help)."""
    from multigenz.adapters.registry import build_adapters

    pol = _load(policy)

    async def go():
        rows = []
        for name, ad in build_adapters(pol).items():
            p = await ad.probe()
            rows.append({"name": name, "adapter": pol.agents[name].adapter, "enabled": p.enabled,
                         "reason": p.reason, "invocation": p.invocation, "modes": p.modes,
                         "capabilities": p.capabilities or sorted(pol.agents[name].capabilities),
                         "version": p.version})
        return rows

    rows = asyncio.run(go())
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        state = "enabled " if r["enabled"] else "disabled"
        typer.echo(f"{r['name']:<12} {state} {r['adapter']:<8} caps={','.join(r['capabilities'])}")
        typer.echo(f"{'':<12} {r['invocation'] or r['reason']}")


@agents_app.command("run")
def agents_run(
    agent: Annotated[str, typer.Argument(help="Agent name from the policy.")],
    objective: Annotated[str, typer.Argument(help="What the agent should do.")],
    repo: Annotated[Path, typer.Option("--repo", help="Git repository (never modified; a worktree is used).")] = Path("."),
    allow: Annotated[list[str], typer.Option("--allow", help="Allowed path pattern (repeatable).")] = None,  # type: ignore[assignment]
    timeout: Annotated[Optional[int], typer.Option(help="Seconds (default: policy task timeout).")] = None,
    policy: PolicyOpt = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON-lines events.")] = False,
) -> None:
    """Run ONE agent in a controlled, sandboxed worktree and validate the resulting commit (M1)."""
    from multigenz.single import run_single_agent

    pol = _load(policy)
    if not allow:
        raise _fail("at least one --allow path pattern is required")
    try:
        out = asyncio.run(run_single_agent(pol, agent=agent, objective=objective, repo=repo, allow=allow,
                                           timeout=timeout, home=default_state_home(),
                                           emit=(lambda s: typer.echo(s)) if json_out else None))
    except MultiGenzError as exc:
        raise _fail(f"error: {exc}") from exc
    if json_out:
        typer.echo(json.dumps(out))
    else:
        typer.echo(f"attempt {out['attempt_id']} agent_exit={out['exit_code']} error_code={out['error_code']}")
        typer.echo(f"branch {out['branch']} commit {out['commit_sha']}")
        for c in out["checks"]:
            typer.echo(f"  [{'ok' if c['passed'] else 'FAIL'}] {c['name']} {c['detail']}")
        typer.echo("VALIDATED" if out["validated"] else "NOT VALIDATED")
    if not out["validated"]:
        raise typer.Exit(1)


# ----------------------------------------------------------------------------------------------
def _read_plan(plan_file: Path | None):
    from multigenz.contracts.task import PlanDocument

    if plan_file is None:
        return None
    try:
        return PlanDocument.model_validate_json(plan_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - pydantic/JSON/OS errors all mean "plan unusable"
        raise _fail(f"invalid plan file {plan_file}: {exc}", 2) from exc


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help="Development objective.")],
    repo: Annotated[Path, typer.Option("--repo", help="Git repository (never modified directly).")] = Path("."),
    mode: Annotated[str, typer.Option(help="autonomous (integrates on a run-owned branch only).")] = "autonomous",
    plan_file: Annotated[Optional[Path], typer.Option("--plan-file", help="Use this validated plan JSON instead of "
                                                      "invoking a planner agent.")] = None,
    detach: Annotated[bool, typer.Option(help="Create the run and execute it in a background worker.")] = False,
    policy: PolicyOpt = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Plan (or load a plan), validate it, execute tasks in isolated worktrees and integrate verified work."""
    from multigenz.core.driver import spawn_detached, start_run

    pol = _load(policy)
    plan = _read_plan(plan_file)
    home = default_state_home()
    try:
        run_id, outcome = asyncio.run(start_run(
            pol, objective=goal, repo=repo, plan=plan, home=home, mode=mode, execute=not detach,
            handle_signals=True, on_message=lambda m: typer.secho(m, fg=typer.colors.YELLOW, err=True)))
    except MultiGenzError as exc:
        raise _fail(f"error: {exc}") from exc
    if detach:
        pid = spawn_detached(home, run_id)
        typer.echo(run_id if json_out else f"run {run_id} started in background (worker pid {pid})")
        return
    _print_outcome(home, run_id, outcome, json_out)


@app.command()
def plan(
    goal: Annotated[str, typer.Argument(help="Development objective.")],
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    out: Annotated[Optional[Path], typer.Option("--out", help="Write the validated plan JSON here.")] = None,
    policy: PolicyOpt = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ask a planner agent for a task DAG and validate it. Nothing is executed or persisted."""
    from multigenz.core.driver import plan_only

    pol = _load(policy)
    try:
        doc = asyncio.run(plan_only(pol, goal=goal, repo=repo, home=default_state_home()))
    except MultiGenzError as exc:
        issues = getattr(exc, "issues", None)
        typer.secho("plan rejected:" if issues else f"error: {exc}", fg=typer.colors.RED, err=True)
        for i in issues or []:
            typer.secho(f"  - {i}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    if out:
        out.write_text(doc.model_dump_json(indent=2))
    if json_out:
        typer.echo(doc.model_dump_json(indent=2))
        return
    typer.echo(f"plan accepted: {len(doc.tasks)} tasks. {doc.summary}")
    for t in doc.tasks:
        typer.echo(f"  - {t.task_id:<16} deps={t.dependencies or '-'} paths={t.allowed_paths} caps={sorted(t.required_capabilities)}")


def _print_outcome(home: Path, run_id: str, outcome, json_out: bool) -> None:
    from multigenz.core.report import build_report
    from multigenz.core.runs import open_store

    store = open_store(home, readonly=True)
    try:
        rep = build_report(store, run_id)
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(rep, indent=2, default=str))
    else:
        _print_report(rep)
    if outcome is None or outcome.status != "succeeded":
        raise typer.Exit(1 if outcome is None or outcome.status != "interrupted" else 130)


def _print_report(rep: dict) -> None:
    color = {"succeeded": typer.colors.GREEN, "failed": typer.colors.RED}.get(rep["status"], typer.colors.YELLOW)
    typer.echo(typer.style(f"run {rep['run_id']}: {rep['status'].upper()}", fg=color, bold=True))
    typer.echo(f"  objective: {rep['objective']}")
    typer.echo(f"  integration branch: {rep['integration_branch']}  final commit: {rep['final_integration_commit'] or '-'}")
    for issue in (rep["planning"]["failure"] or {}).get("issues", []):
        typer.secho(f"  plan rejected: {issue}", fg=typer.colors.RED)
    for t in rep["tasks"]:
        v = t.get("last_validation") or {}
        typer.echo(f"  - {t['task_id']:<18} {t['status']:<10} attempts={t['attempts']} provider={t['provider'] or '-'}"
                   f"{'  ' + t['failure_reason'] if t['failure_reason'] else ''}"
                   f"{'  tests/validation: ' + ('pass' if v.get('passed') else 'FAIL') if v else ''}")
    c = rep["cost"]
    typer.echo(f"  cost: measured ${c['measured_usd']} (budget ${c['budget_usd']}); {c['note']}")
    typer.echo(f"  {rep['push_policy']}")


def _resolve(store, prefix: str) -> str:
    rid = store.resolve_run_id(prefix)
    if rid is None:
        raise _fail(f"no unique run matching {prefix!r}")
    return rid


@runs_app.command("list")
def runs_list(limit: int = 30, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Run history."""
    from multigenz.core.locks import RunLock
    from multigenz.core.runs import RunPaths, open_store

    home = default_state_home()
    store = open_store(home)
    rows = store.list_runs(limit)
    for r in rows:
        r["owned"] = RunLock.is_held(RunPaths(home, r["run_id"]).lock)
    store.close()
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        flag = " (orphaned: resume with `runs resume`)" if r["status"] == "running" and not r["owned"] else ""
        typer.echo(f"{r['run_id']}  {r['status']:<10} {r['created_at']}  {r['objective'][:60]}{flag}")


@runs_app.command("show")
def runs_show(run_id: str, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Task states, validation results, artifacts, final SHA and (explicitly unknown) costs."""
    from multigenz.core.report import build_report
    from multigenz.core.runs import open_store

    store = open_store(default_state_home())
    rid = _resolve(store, run_id)
    rep = build_report(store, rid)
    store.close()
    typer.echo(json.dumps(rep, indent=2, default=str)) if json_out else _print_report(rep)


@runs_app.command("events")
def runs_events(run_id: str, after: int = 0, limit: int = 200, kind: Annotated[Optional[str], typer.Option()] = None) -> None:
    """Structured, redacted event log (JSON lines)."""
    from multigenz.core.runs import open_store

    store = open_store(default_state_home())
    rid = _resolve(store, run_id)
    for e in store.events(rid, after_seq=after, limit=limit, kinds=(kind,) if kind else None):
        typer.echo(json.dumps({k: e[k] for k in ("seq", "ts", "kind", "task_id", "attempt_id", "payload")}))
    store.close()


@runs_app.command("audit")
def runs_audit(run_id: str) -> None:
    """Verify the hash-chained event log and replay it to reproduce the stored states (AT-16)."""
    from multigenz.core.replay import replay_run
    from multigenz.core.runs import open_store

    store = open_store(default_state_home())
    rid = _resolve(store, run_id)
    res = replay_run(store, rid)
    store.close()
    typer.echo(f"{res.events} events; replayed run status={res.run_status}; tasks={res.task_status}")
    if res.errors:
        for e in res.errors:
            typer.secho(f"  AUDIT ERROR: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("audit OK: sequence contiguous, hash chain intact, every transition legal, replay == stored state",
                fg=typer.colors.GREEN)


@runs_app.command("cancel")
def runs_cancel(run_id: str) -> None:
    """Cancel a run. Committed work and diagnostic logs are preserved on the run's branches."""
    from multigenz.core.driver import cancel_run
    from multigenz.core.runs import open_store

    home = default_state_home()
    store = open_store(home)
    rid = _resolve(store, run_id)
    store.close()
    try:
        typer.echo(asyncio.run(cancel_run(home, rid)))
    except MultiGenzError as exc:
        raise _fail(f"error: {exc}") from exc


@runs_app.command("resume")
def runs_resume(run_id: str, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Reconcile leases/processes/worktrees/commits, then continue the run without duplicating work."""
    from multigenz.core.driver import resume_run
    from multigenz.core.runs import open_store

    home = default_state_home()
    store = open_store(home)
    rid = _resolve(store, run_id)
    store.close()
    try:
        outcome = asyncio.run(resume_run(home, rid, handle_signals=True,
                                         on_message=lambda m: typer.secho(m, fg=typer.colors.YELLOW, err=True)))
    except MultiGenzError as exc:
        raise _fail(f"error: {exc}") from exc
    _print_outcome(home, rid, outcome, json_out)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
