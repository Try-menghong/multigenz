# Implementation milestones, execution rules and acceptance tests

## Technical stack
Python 3.12+, Typer, Textual, Pydantic v2, asyncio subprocesses, LiteLLM, SQLite WAL, Git CLI, rootless container runtime, pytest and pytest-asyncio. Start on one Linux host; avoid Redis/Kubernetes/distributed workers in v1.

## Milestones
**M1 — CLI and adapters:** pipx-installable package; `doctor`; capability/auth checks; one agent in controlled workspace; structured events; timeout and cancellation. Exit: one validated agent commit.

**M2 — Durable scheduler:** typed DAG, SQLite state transitions, transactional event records, bounded concurrency, exclusive attempt leases, restart reconciliation. Exit: parallel and interruption tests pass.

**M3 — Safe integration:** unique Git worktrees, diff and artifact validation, dependency snapshots, isolated merge, conflict handling, test profiles. Exit: failed changes cannot enter integration branch.

**M4 — Autonomous planning:** structured planner, capability routing, bounded retries, reviewer, spend controls where usage is measurable. Exit: successful and broken feature scenarios pass.

**M5 — TUI and release:** Textual dashboards, run history, redacted logs, security regression, packaging and docs. Exit: all mandatory acceptance tests pass.

## State and scheduling rules
- READY requires all prerequisites independently validated and inputs present.
- One active lease per task attempt; enforce global/per-provider concurrency.
- Conflicting write ownership must serialize or have an explicit dependency.
- No task is successful before independent verification.
- Failed prerequisite blocks dependents.
- Persist transitions and event sequence numbers transactionally.
- On restart, reconcile leases and OS processes before re-execution.

## Recommended v1 defaults
Three concurrent CLI agents, two auxiliary API requests, two attempts per task, one merge-conflict repair, 900-second task timeout, configurable $5 run budget (where actual usage can be measured). Subscription CLI spending may be unknown and must be labeled as such. No automatic push.

## Acceptance tests
- AT-01 Planner rejects cyclic dependencies.
- AT-02 Independent tasks execute concurrently.
- AT-03 Dependents wait for validated prerequisites.
- AT-04 Provider failure triggers compatible reassignment.
- AT-05 Timeout terminates subprocess process group.
- AT-06 Cancellation preserves committed and diagnostic artifacts.
- AT-07 Restart reconciles unfinished runs without duplicate unsafe execution.
- AT-08 Failed agent cannot create successful task status.
- AT-09 Worktrees cannot overwrite each other.
- AT-10 Merge conflict preserves original commits.
- AT-11 Failing tests prevent successful integration.
- AT-12 Original branch and dirty files remain unchanged.
- AT-13 Agent cannot read host credentials.
- AT-14 Agent cannot write outside authorized sandbox.
- AT-15 Logs redact known credentials.
- AT-16 Every state transition is auditable and replayable.

## End-to-end scenarios
A. Successful feature: create FastAPI authentication service, tests and docs, merge verified commits, full tests pass.
B. Broken implementation: inject failing test, attempt bounded repair, then accurately report success or failure.
C. Interrupted run: terminate during concurrent tasks, restart and reconcile without losing work or duplicating unsafe side effects.

## Definition of done
```bash
multigenz run "Create a REST API with authentication and tests" --repo ./demo-api --mode autonomous
```
A real run produces verified task states, test report, artifact list, final integration SHA, explicit unknown costs, and no automatic push. All AT-01–AT-16 and A–C must pass repeatedly in a clean Linux environment before claiming v1 works.
