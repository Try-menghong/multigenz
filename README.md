# MultiGenz

Local-first autonomous multi-agent software-development orchestrator. It drives the coding-agent CLIs you already use
(**Claude Code, opencode, Cline, OpenAI Codex**, experimentally Google Antigravity) plus LiteLLM providers for planning
and review text, each in its own sandbox and Git worktree, and integrates only independently tested work.

**Status:** implemented through milestones M1–M5 and verified by an automated suite (see [docs/TESTING.md](docs/TESTING.md)).
Read [What is and is not verified](#what-is-and-is-not-verified) before trusting it with real work.

## What it does
Accepts a development objective, obtains a validated task DAG from a planner agent (or a plan file), dispatches
independent tasks to isolated agents in separate Git worktrees, **independently** verifies every result (path ownership,
diff hygiene, credential scan, deterministic review, administrator-defined tests), integrates only verified work on a
run-owned branch, repairs within bounds, and reports honestly. It never pushes, deploys or touches your branches.

## Design principles
- Python 3.12+, Typer CLI, Textual TUI, Pydantic v2, asyncio, SQLite WAL, Git worktrees.
- The orchestrator — not any model — owns task state, policy, scheduling, Git integration and every success decision.
- Each agent runs in a restricted sandbox (fail closed; never silently unsandboxed). Worktrees alone are *not* isolation.
- Agent output, repository text and planner output are untrusted. Test commands come from host policy only.
- Autonomous work may update only `multigenz/run-<id>/…` branches. No push, deployment or protected-branch write exists.
- No success claim without independently verified tests and artifacts; costs that cannot be measured are reported as unknown.

## Install and first run
```bash
curl -fsSL https://try-menghong.github.io/multigenz/install | bash   # once the site is published (see below)
# or, from a clone of this repository:
MULTIGENZ_SOURCE=$PWD bash install.sh

multigenz agents presets      # supported agent CLIs, how well each is tested, which are installed
multigenz init                # writes ~/.config/multigenz/policy.yaml with the CLIs found on PATH
multigenz doctor              # sandbox, git, sqlite, agents, test profile
multigenz run "Add input validation with tests" --repo ./app
```
The installer needs Linux, git >= 2.38 and Python >= 3.12 (or `uv`, which can provide Python). It installs into
`~/.multigenz`, links `~/.local/bin/multigenz`, never uses sudo, and `bash install.sh --uninstall` removes it again.
It installs from `main` by default; set `MULTIGENZ_REF=<branch>` to install another branch. While the repository is
private, the installer uses your SSH key (`git@github.com:...`) when https needs credentials.

`site/` is the download page; `.github/workflows/pages.yml` publishes it with `install.sh` served at `/install` once
GitHub Pages is enabled (Settings → Pages → Source: GitHub Actions) and the work is on `main`.

### Agents
| Preset | Runs | Tested | Notes |
|---|---|---|---|
| `claude` | `claude --print --permission-mode acceptEdits` | real model runs (Claude Pro login, userns sandbox) | may run pytest itself; Pro/Max: export `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` |
| `opencode` | `opencode run --auto` (prompt on stdin) | end to end in the sandbox against a local scripted model | uses opencode's own login files, mounted read-only |
| `cline` | `cline --yolo` / `cline --plan` | end to end in the sandbox against a local scripted model | uses `~/.cline/data/settings/providers.json` |
| `codex` | `codex exec --sandbox danger-full-access` | flags verified against `codex exec --help` only | **opt-in** (`--trust-outer-sandbox`): Codex's own sandbox cannot start inside MultiGenz's |
| `agy` | `agy --print --mode accept-edits` | experimental: flags from `agy --help`, never executed here | check with `multigenz agents run` first |

Add one with `multigenz agents add <preset>`. Any other CLI with a non-interactive mode can be configured by hand
(`agents.<name>.invocations`, see [docs/OPERATIONS.md](docs/OPERATIONS.md)); flags are always verified against the
binary's own `--help` before use.

## Commands (all implemented and covered by tests)
```text
multigenz init [--agent P] [--overwrite]   starter policy with the agent CLIs found on PATH
multigenz doctor                         environment, isolation backends, adapter capability detection
multigenz agents presets | add P         supported agent CLIs; add one to the policy (validated, backup kept)
multigenz agents list | run              inspect adapters; run ONE sandboxed agent -> validated commit
multigenz plan "<goal>" [--out plan.json]   planner agent proposes a DAG; validated; nothing executed
multigenz run "<goal>" --repo R [--plan-file plan.json] [--detach]
multigenz runs list | show | events | audit | logs | cancel | resume
multigenz tui                            dashboards: history, live task states, redacted logs, cancel, doctor
```
`Ctrl-C`/SIGTERM on `run` is a *resumable* interrupt (agents stopped, work preserved); `runs cancel` is terminal.
A run whose orchestrator was killed shows as *orphaned*; `runs resume` reconciles leases, OS processes, worktrees and
commits before continuing and never re-executes finished work.

## What is and is not verified
| Area | Evidence | Caveat |
|---|---|---|
| Planner → DAG → sandboxed tasks → validation → integration → report | 16 acceptance tests, E2E A/B/C (real FastAPI code, real pytest in the sandbox and again on the host) | those agents are **scripted stand-ins** |
| Real coding-agent CLIs inside the sandbox | opencode 1.19 and Cline 3.0 plan, code, validate and integrate end to end (`tests/e2e/test_cli_agents.py`), including one run where Cline plans and opencode codes | the model behind them is a **local scripted server**, not a real provider |
| Claude Code with a real model | several real runs by a user with a Claude Pro login (planning, repair after a failed attempt, integration) | not part of the automated suite (needs a login) |
| Codex | `codex exec` driven by the mock outside MultiGenz; inside, its inner bwrap sandbox fails, so it needs `--trust-outer-sandbox` | the opt-in path has **not been executed** in development |
| `userns` sandbox (read-only FS, hidden host env/home, caps dropped, pid ns; npm/node CLIs mounted automatically) | unit + AT-05/13/14 | Linux-only; no cgroup memory/CPU ceilings; no seccomp (see docs/SECURITY.md) |
| `rootless-container` sandbox | argv construction unit-tested | **never run against a live runtime** |
| Installer and download page | full install/upgrade/uninstall into a throwaway HOME, `curl … \| bash` from the served page, page checked in Chromium (desktop/phone, light/dark) | the public site is not deployed yet |
| LiteLLM adapter | injected fake completion | no live provider call |
| TUI | Textual pilot tests | not manually exercised in a real terminal |

Not implemented: pushing, deployment, an approval workflow (the `approvals` table exists but nothing grants anything),
garbage collection / retention of worktrees (nothing is ever deleted), distributed workers.

## Documents
1. [AGENTS.md](AGENTS.md) – implementation rules
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) – architecture and contracts
3. [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) – milestones, acceptance tests
4. [docs/OPERATIONS.md](docs/OPERATIONS.md) – policy, profiles, state layout, recovery
5. [docs/SECURITY.md](docs/SECURITY.md) – threat model, controls, residual risks
6. [docs/TESTING.md](docs/TESTING.md) – how to run the suites, results, simulated parts
7. [config/policy.example.yaml](config/policy.example.yaml), [config/policy.userns.example.yaml](config/policy.userns.example.yaml),
   [config/policy.claude.example.yaml](config/policy.claude.example.yaml)
8. [install.sh](install.sh), [site/](site/) – installer and download page
