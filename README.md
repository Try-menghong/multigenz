# MultiGenz

Local-first autonomous multi-agent software-development orchestrator for Claude Code, OpenAI Codex, Google
Antigravity and LiteLLM providers.

**Status:** implemented through milestones M1–M5 and verified by an automated suite (see [docs/TESTING.md](docs/TESTING.md)).
Read [What is and is not verified](#what-is-and-is-not-verified) before trusting it with real work: the test suite uses
**scripted stand-in agents, not LLMs**, and no live Claude / Codex / Antigravity / DeepSeek call has been made.

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
pipx install .            # or: pip install .   (Python >= 3.12, git >= 2.38, Linux)
multigenz doctor --policy config/policy.userns.example.yaml
multigenz run "Create a REST API with authentication and tests" --repo ./demo-api --mode autonomous \
    --policy my-policy.yaml
```
`doctor` exits non-zero if a required component (git, sqlite WAL, the policy's isolation backend, a usable agent) is
missing. With `config/policy.example.yaml` it fails on a host without a rootless container runtime — by design.

## Commands (all implemented and covered by tests)
```text
multigenz doctor                         environment, isolation backends, adapter capability detection
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
| Planner → DAG → sandboxed tasks → validation → integration → report | 16 acceptance tests, E2E A/B/C (real FastAPI code, real pytest in the sandbox and again on the host) | agents are **scripted stand-ins**, not LLMs |
| `userns` sandbox (read-only FS, hidden host env/home, no net, caps dropped, pid ns) | unit + AT-05/13/14 | Linux-only; no cgroup memory/CPU ceilings; no seccomp (see docs/SECURITY.md) |
| `rootless-container` sandbox | argv construction unit-tested | **never run against a live runtime** (none available here) |
| Claude Code adapter | invocation verified against the installed binary's `--help` text | **no live agent call**; unattended flags rely on the sandbox |
| Codex / Antigravity adapters | capability probing only | ship **disabled** until an administrator configures and verifies an invocation |
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
7. [config/policy.example.yaml](config/policy.example.yaml), [config/policy.userns.example.yaml](config/policy.userns.example.yaml)
