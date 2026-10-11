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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
