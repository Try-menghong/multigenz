# MultiGenz

Local-first autonomous multi-agent software-development orchestrator for Claude Code, OpenAI Codex, Google Antigravity and LiteLLM providers.

**Status:** Architecture and implementation plan only. No production-ready orchestrator is implemented yet.

## Goal
Accept a development objective, produce a validated task DAG, dispatch independent tasks to isolated agents, verify artifacts, perform bounded repairs, and integrate tested changes on a run-owned branch.

## Design principles
- Python 3.12+, Typer CLI, Textual TUI, Pydantic v2, asyncio, SQLite WAL, Git worktrees.
- The orchestrator—not any model—owns task state, policy, scheduling, Git integration, and final success decisions.
- Each agent runs in a restricted sandbox; Git worktrees alone are **not** security isolation.
- Agent output and repository text are untrusted. Test commands and permissions are selected by host policy, not agent-generated instructions.
- Autonomous work may update namespaced integration branches; never push, deploy, modify infrastructure, or update protected branches without separate authorization.
- No success claim without independently verified tests and artifacts.

## Start here
1. [Codex implementation instructions](AGENTS.md)
2. [Detailed architecture](docs/ARCHITECTURE.md)
3. [Implementation milestones and acceptance tests](docs/IMPLEMENTATION_PLAN.md)
4. [Example configuration](config/policy.example.yaml)

## Proposed commands (not implemented yet)
```bash
multigenz doctor
multigenz agents list
multigenz plan "Build a FastAPI service"
multigenz run "Build a FastAPI service" --repo ./my-project --mode autonomous
multigenz runs list
multigenz runs show <run-id>
multigenz runs cancel <run-id>
multigenz runs resume <run-id>
multigenz tui
```
