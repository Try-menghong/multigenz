# Instructions for Codex and coding agents

Implement MultiGenz incrementally according to docs/ARCHITECTURE.md and docs/IMPLEMENTATION_PLAN.md. These files describe **planned behavior**, not existing features.

## Priority
1. Package skeleton with Python 3.12+, Typer CLI, Pydantic v2, pytest, pyproject.toml and executable `multigenz`.
2. Typed contracts, provider-neutral adapters, subprocess manager with cwd, process-group termination, timeouts, and event streaming.
3. SQLite WAL state machine, validated task DAG, bounded scheduler and restart reconciliation.
4. Safe worktree lifecycle and integration in run-owned branches, strict diff checks and independent test validation.
5. Bounded planner/repair loop, sandbox policy enforcement, then Textual TUI and LiteLLM.

## Engineering constraints
- Write tests with every milestone. Run them and report results; never invent test success.
- Never use `git reset --hard`, force-push, delete user branches, or modify the user's original working tree.
- No `|| true` to mask errors. Capture process exit status and verify changed artifacts.
- Never expose host API keys to untrusted agent processes. Keep policy enforcement outside agent sandboxes.
- No arbitrary shell execution from planner-provided commands. Use administrator-defined test profiles/allowlists.
- Fail closed when required isolation is unavailable; do not silently fall back to unsandboxed execution.
- Avoid hard-coding assumed Antigravity, Claude or Codex CLI flags. Detect capabilities and verify supported non-interactive modes before enabling an adapter.
- Keep CLI commands documented as proposed until implemented.
- Autonomous integration is allowed only on `multigenz/run-<id>/integration`; pushing and deployments are disabled by default.
- Record structured, redacted events and ensure restart recovery never duplicates unsafe external side effects.

## Definition of done
All 16 acceptance tests and three end-to-end scenarios in docs/IMPLEMENTATION_PLAN.md pass in a clean Linux environment. Until then, clearly describe incomplete features.
