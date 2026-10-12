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
roles_app = typer.Typer(help="Choose which agent plans, which ones code, review and resolve conflicts.",
                        invoke_without_command=True)
app.add_typer(roles_app, name="roles")
providers_app = typer.Typer(help="API providers (OpenRouter, OpenAI, DeepSeek, Ollama, ...): add one, list its models, "
                                 "make an agent from a model.", no_args_is_help=True)
app.add_typer(providers_app, name="providers")

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
    from multigenz.config import default_policy_path

    checks = asyncio.run(run_doctor(pol, policy_error=err, policy_file=policy or default_policy_path()))
    if json_out:
        typer.echo(json.dumps([c.__dict__ | {"status": c.status} for c in checks], indent=2))
    else:
        for c in checks:
            color = {"ok": typer.colors.GREEN, "FAIL": typer.colors.RED, "warn": typer.colors.YELLOW}[c.status]
            typer.echo(typer.style(f"[{c.status:>4}] ", fg=color) + f"{c.name}: {c.detail}")
    if any(c.status == "FAIL" for c in checks):
        raise typer.Exit(1)


@app.command()
def init(
    policy: PolicyOpt = None,
    agent: Annotated[Optional[list[str]], typer.Option(
        "--agent", "-a", help="Preset to include (repeatable). Default: every supported CLI found on PATH.")] = None,
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace an existing policy (a .bak copy is kept).")] = False,
) -> None:
    """Create a starter policy (userns sandbox, a pytest profile, and the coding-agent CLIs found on PATH)."""
    from multigenz.config import config_policy_path
    from multigenz.presets import PRESETS, detect_agents, init_policy

    path = policy or config_policy_path()
    keys = agent if agent else detect_agents()
    try:
        added = init_policy(path, agents=keys, overwrite=overwrite)
    except (FileExistsError, ValueError, MultiGenzError) as exc:
        raise _fail(str(exc), 2) from exc
    typer.echo(f"wrote {path}")
    typer.echo(f"agents: {', '.join(added) if added else '(none found on PATH; add one with `multigenz agents add`)'}")
    typer.echo("next: edit the `tests` profile for your project, then run `multigenz doctor`")


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


@agents_app.command("presets")
def agents_presets() -> None:
    """Ready-made agent entries (`multigenz agents add <preset>`), how well each is tested, and whether it is installed."""
    import shutil as _sh

    from multigenz.presets import PRESETS

    for p in PRESETS.values():
        found = _sh.which(p.command)
        typer.echo(f"{p.key:<9} {p.status:<14} {('found ' + found) if found else 'not on PATH'}")
        typer.echo(f"{'':<9} {p.summary}")
        if not found and p.install_hint:
            typer.echo(f"{'':<9} install: {p.install_hint}")


@agents_app.command("add")
def agents_add(
    preset: Annotated[str, typer.Argument(help="Preset: claude, opencode, cline, codex, agy.")],
    policy: PolicyOpt = None,
    name: Annotated[Optional[str], typer.Option("--name", help="Policy key (default: the preset's).")] = None,
    command: Annotated[Optional[str], typer.Option("--command", help="Binary path or name (default: the preset's).")] = None,
) -> None:
    """Add a preset agent to the policy file (backup kept; validated before it is kept)."""
    from multigenz.config import config_policy_path
    from multigenz.presets import PRESETS, add_preset

    path = policy or config_policy_path()
    try:
        backup = add_preset(path, preset, name=name, command=command)
    except (ValueError, FileNotFoundError, MultiGenzError) as exc:
        raise _fail(str(exc), 2) from exc
    p = PRESETS[preset]
    typer.echo(f"added agents.{name or p.agent} ({p.status}) to {path}; backup at {backup}")
    if p.status != "run-tested":
        typer.secho(f"note: {p.key} is {p.status}; check it with `multigenz agents run {name or p.agent} ...` "
                    "on a scratch repository first", fg=typer.colors.YELLOW)
    typer.echo("next: multigenz agents list")


def _policy_file(policy: Path | None) -> Path:
    from multigenz.config import config_policy_path, default_policy_path

    path = policy or default_policy_path() or config_policy_path()
    if not Path(path).expanduser().is_file():
        raise _fail(f"{path}: no policy file (create one with `multigenz init`)", 2)
    return Path(path).expanduser()


def _save(path: Path, **edits) -> Policy:
    from multigenz.policy_edit import save_policy_edits, short_error

    try:
        pol, backup = save_policy_edits(path, **edits)
    except (MultiGenzError, FileNotFoundError, KeyError) as exc:
        raise _fail(f"not saved: {short_error(exc)}", 2) from exc
    typer.echo(f"saved {path} (previous version: {backup})")
    return pol


@agents_app.command("enable")
def agents_enable(name: str, policy: PolicyOpt = None) -> None:
    """Enable an agent in the policy file."""
    _save(_policy_file(policy), enabled={name: True})


@agents_app.command("disable")
def agents_disable(name: str, policy: PolicyOpt = None) -> None:
    """Disable an agent in the policy file (it is never used, whatever its roles)."""
    _save(_policy_file(policy), enabled={name: False})


ROLE_HELP = {
    "planner": "plans: turns the goal into a task graph (the orchestrating agent)",
    "worker": "codes: runs the tasks",
    "reviewer": "reviews: veto-only second opinion on each validated change",
    "conflict": "resolves merge conflicts",
}
ROLE_AUTO = {
    "planner": "automatic: enabled agents with the 'architecture' capability and a plan invocation",
    "worker": "automatic: any enabled agent with the task's capabilities",
    "reviewer": "automatic: off unless validation.review_with_agent (then 'review'-capable agents)",
    "conflict": "automatic: same as the workers",
}


PLANNING_HELP = {
    "always": "a planner agent splits every goal into tasks first (one extra agent run per goal)",
    "auto": "one worker takes the goal; it asks for subagents only when the goal is too big (no planning run)",
    "never": "one worker does the whole goal; no planning, no subagents (cheapest)",
}


def _show_roles(pol: Policy) -> None:
    from multigenz.config import ROLE_NAMES

    mode = pol.orchestrator.planning
    typer.echo(f"{'planning':<9} {mode}: {PLANNING_HELP[mode]}")

    for r in ROLE_NAMES:
        chosen = getattr(pol.roles, r)
        if r == "conflict" and not chosen and pol.roles.worker:
            value = "same as the workers: " + " -> ".join(pol.roles.worker)
        else:
            value = " -> ".join(chosen) if chosen else ROLE_AUTO[r]
        disabled = [n for n in chosen if not pol.agents[n].enabled]
        note = f"   (disabled in policy: {', '.join(disabled)})" if disabled else ""
        typer.echo(f"{r:<9} {value}{note}")
        typer.echo(f"{'':<9} {ROLE_HELP[r]}")


@roles_app.callback()
def roles_main(ctx: typer.Context, policy: PolicyOpt = None) -> None:
    """Show who plans, codes, reviews and resolves conflicts (``multigenz roles``)."""
    if ctx.invoked_subcommand is None:
        _show_roles(_load(policy))


@roles_app.command("planning")
def roles_planning(
    mode: Annotated[str, typer.Argument(help="always, auto or never.")],
    policy: PolicyOpt = None,
) -> None:
    """How goals are split: always (planner first), auto (subagents only when needed) or never (one agent)."""
    if mode not in PLANNING_HELP:
        raise _fail("planning must be always, auto or never", 2)
    _show_roles(_save(_policy_file(policy), planning=mode))


@roles_app.command("set")
def roles_set(
    role: Annotated[str, typer.Argument(help="planner, worker, reviewer or conflict.")],
    agents: Annotated[list[str], typer.Argument(help="Agents in order of preference.")],
    policy: PolicyOpt = None,
) -> None:
    """Assign agents to a role, in order of preference (the next one is the fallback)."""
    from multigenz.config import ROLE_NAMES

    if role not in ROLE_NAMES:
        raise _fail(f"unknown role {role!r}; choose from {', '.join(ROLE_NAMES)}", 2)
    path = _policy_file(policy)
    pol = _load(path)
    roles = {r: list(getattr(pol.roles, r)) for r in ROLE_NAMES} | {role: list(agents)}
    _show_roles(_save(path, roles=roles))


@roles_app.command("clear")
def roles_clear(
    role: Annotated[str, typer.Argument(help="planner, worker, reviewer, conflict, or all.")],
    policy: PolicyOpt = None,
) -> None:
    """Return a role (or all) to automatic selection by capability."""
    from multigenz.config import ROLE_NAMES

    if role != "all" and role not in ROLE_NAMES:
        raise _fail(f"unknown role {role!r}; choose from {', '.join(ROLE_NAMES)} or all", 2)
    path = _policy_file(policy)
    pol = _load(path)
    roles = {r: [] if role in (r, "all") else list(getattr(pol.roles, r)) for r in ROLE_NAMES}
    _show_roles(_save(path, roles=roles))


@providers_app.command("presets")
def providers_presets() -> None:
    """Known provider endpoints you can add by name."""
    from multigenz.providers import PRESETS

    for p in PRESETS.values():
        typer.echo(f"{p.key:<11} {p.label:<26} {p.base_url or '(you give the URL)':<56} {p.api_key_env or '(no key)'}")


@providers_app.command("list")
def providers_list(policy: PolicyOpt = None) -> None:
    """Providers in the policy, whether their key is available, and the agents using them."""
    from multigenz.providers import key_for

    pol = _load(policy)
    if not pol.providers:
        typer.echo("no providers yet: multigenz providers add <preset>   (see: multigenz providers presets)")
    for name, prov in pol.providers.items():
        key = "no key needed" if not prov.api_key_env else (
            f"key {prov.api_key_env}: {'set' if key_for(prov) else 'MISSING'}")
        users = [f"{a} ({c.model})" for a, c in pol.agents.items() if c.provider == name]
        typer.echo(f"{name:<12} {prov.kind:<18} {prov.base_url}  [{key}]")
        typer.echo(f"{'':<12} agents: {', '.join(users) or '-'}")


@providers_app.command("add")
def providers_add(
    preset: Annotated[str, typer.Argument(help="openrouter, openai, anthropic, deepseek, groq, ollama, custom, ...")],
    name: Annotated[Optional[str], typer.Option("--name", help="Provider name in the policy (default: the preset).")] = None,
    base_url: Annotated[Optional[str], typer.Option("--base-url", help="Endpoint (required for custom).")] = None,
    key_env: Annotated[Optional[str], typer.Option("--key-env", help="Environment variable holding the key.")] = None,
    key_stdin: Annotated[bool, typer.Option("--key-stdin", help="Read the API key from stdin and store it.")] = False,
    no_key: Annotated[bool, typer.Option("--no-key", help="The endpoint needs no key (local servers).")] = False,
    policy: PolicyOpt = None,
) -> None:
    """Add an API provider. The key is stored in ~/.config/multigenz/secrets.env (0600), never in the policy."""
    import getpass

    from multigenz.policy_edit import add_provider, short_error
    from multigenz.providers import PRESETS, ProviderError, set_secret

    if preset not in PRESETS:
        raise _fail(f"unknown preset {preset!r}; see `multigenz providers presets`", 2)
    pr = PRESETS[preset]
    url = base_url or pr.base_url
    if not url:
        raise _fail("--base-url is required for a custom provider", 2)
    pname = name or preset
    env_name = None if no_key else (key_env or pr.api_key_env or f"{pname.upper().replace('-', '_')}_API_KEY")
    path = _policy_file(policy)
    try:
        add_provider(path, pname, kind=pr.kind, base_url=url, api_key_env=env_name)
    except (MultiGenzError, FileNotFoundError) as exc:
        raise _fail(f"not added: {short_error(exc)}", 2) from exc
    typer.echo(f"added provider {pname} ({url}) to {path}")
    if env_name and (key_stdin or sys.stdin.isatty()):
        key = sys.stdin.readline().strip() if key_stdin else getpass.getpass(f"{env_name} (input hidden, Enter to skip): ")
        if key:
            try:
                where = set_secret(env_name, key)
            except ProviderError as exc:
                raise _fail(str(exc), 2) from exc
            typer.echo(f"stored {env_name} in {where} (mode 0600)")
    typer.echo(f"next: multigenz providers models {pname}")


@providers_app.command("models")
def providers_models(
    name: str,
    search: Annotated[Optional[str], typer.Option("--search", "-s", help="Only ids containing this text.")] = None,
    policy: PolicyOpt = None,
) -> None:
    """Fetch the model ids a provider serves (GET <base_url>/models with its key)."""
    from multigenz.providers import ProviderError, fetch_models, key_for

    pol = _load(policy)
    prov = pol.providers.get(name)
    if prov is None:
        raise _fail(f"no provider {name!r} in the policy (multigenz providers list)", 2)
    if prov.api_key_env and not key_for(prov):
        raise _fail(f"no API key: set {prov.api_key_env} (or re-run `multigenz providers add` to store it)", 2)
    try:
        ids = fetch_models(prov, key_for(prov))
    except ProviderError as exc:
        raise _fail(str(exc), 1) from exc
    shown = [i for i in ids if not search or search.lower() in i.lower()]
    for i in shown:
        typer.echo(i)
    typer.secho(f"{len(shown)} of {len(ids)} models. Use one: multigenz providers use {name} <model-id>",
                fg=typer.colors.BLUE, err=True)


@providers_app.command("use")
def providers_use(
    name: str,
    model: str,
    as_: Annotated[str, typer.Option("--as", help="coder: a coding agent (opencode harness); text: planner/reviewer "
                                                  "only (direct API call).")] = "coder",
    agent: Annotated[Optional[str], typer.Option("--agent", help="Agent name (default: derived).")] = None,
    policy: PolicyOpt = None,
) -> None:
    """Make an agent from a provider's model. Give it a role with `multigenz roles` or the TUI Team screen."""
    from multigenz.policy_edit import add_provider_agent, agent_name_for, short_error

    path = _policy_file(policy)
    pol = _load(path)
    if name not in pol.providers:
        raise _fail(f"no provider {name!r} in the policy (multigenz providers list)", 2)
    agent_name = agent or agent_name_for(name, model, set(pol.agents))
    try:
        add_provider_agent(path, agent_name, provider=name, model=model, use=as_)
    except (MultiGenzError, FileNotFoundError) as exc:
        raise _fail(f"not added: {short_error(exc)}", 2) from exc
    typer.echo(f"added agent {agent_name}: {name} model {model} "
               f"({'coding agent via opencode' if as_ == 'coder' else 'text only: planner/reviewer'})")
    typer.echo("next: multigenz agents list, then give it a role (multigenz roles set worker ...) or use the TUI")


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
    planning: Annotated[Optional[str], typer.Option(
        "--planning", help="always: a planner splits the goal first; auto: one agent takes the goal and asks for "
                           "subagents only when needed; never: one agent does it all. Default: the policy's.")] = None,
    detach: Annotated[bool, typer.Option(help="Create the run and execute it in a background worker.")] = False,
    policy: PolicyOpt = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Plan (or load a plan), validate it, execute tasks in isolated worktrees and integrate verified work."""
    from multigenz.core.driver import spawn_detached, start_run

    pol = _load(policy)
    if planning is not None:
        if planning not in ("always", "auto", "never"):
            raise _fail("--planning must be always, auto or never", 2)
        pol = pol.model_copy(update={"orchestrator": pol.orchestrator.model_copy(update={"planning": planning})})
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
        if t["status"] == "split":
            typer.echo(f"  - {t['task_id']:<18} {'split':<10} provider={t['provider'] or '-'} handed the work to "
                       f"subtasks: {', '.join(t.get('subtasks') or [])}")
            continue
        typer.echo(f"  - {t['task_id']:<18} {t['status']:<10} attempts={t['attempts']} provider={t['provider'] or '-'}"
                   f"{'  ' + t['failure_reason'] if t['failure_reason'] else ''}"
                   f"{'  tests/validation: ' + ('pass' if v.get('passed') else 'FAIL') if v else ''}")
    runs = rep.get("agent_runs") or {}
    if runs:
        typer.echo(f"  agent runs: {runs['planning']} planning + {runs['coding']} coding + {runs['review']} review")
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


@app.command()
def tui(policy: PolicyOpt = None,
        team: Annotated[bool, typer.Option("--team", help="Open the Team screen (who plans, codes, reviews).")] = False
        ) -> None:
    """Interactive dashboard: run history, live task states, redacted logs, cancel, doctor, team (roles)."""
    from multigenz.tui.app import MultiGenzApp

    pol, err = None, None
    try:
        pol = load_policy(policy)
    except MultiGenzError as exc:
        err = str(exc)
    MultiGenzApp(default_state_home(), policy=pol, policy_error=err, policy_path=policy,
                 start="team" if team else "runs").run()


@runs_app.command("logs")
def runs_logs(run_id: str, task: Annotated[str, typer.Argument(help="Task id.")]) -> None:
    """Show the (redacted) stdout/stderr logs of every attempt of a task."""
    from multigenz.core.redaction import Redactor
    from multigenz.core.runs import open_store
    from multigenz.tui.data import attempt_logs

    store = open_store(default_state_home(), readonly=True)
    rid = _resolve(store, run_id)
    blocks = attempt_logs(store, rid, task, Redactor.from_environ(__import__("os").environ))
    store.close()
    if not blocks:
        raise _fail(f"no logs for task {task!r} in run {rid}")
    for header, text in blocks:
        typer.echo(f"=== {header}")
        typer.echo(text)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
