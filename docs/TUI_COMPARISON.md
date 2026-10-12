# MultiGenz TUI compared with Claude Code's

Claude Code is a single coding agent in a terminal; MultiGenz runs a *team* of coding agents (Claude Code among them)
on one goal. This page lists what Claude Code's terminal UI offers, what MultiGenz offers for the same need, and what
was deliberately left out. Everything marked *added* is covered by tests (see [TESTING.md](TESTING.md)).

## Prompt and editing
| Claude Code | MultiGenz | |
|---|---|---|
| Welcome box with a mascot, the working directory and tips | the pet (three heads on one body), repository and branch, policy, standing notes, team with models; shrinks once you start | had |
| Multi-line prompt: `\` + Enter, Shift+Enter / Option+Enter, pasted line breaks kept | `\` + Enter, Ctrl+J, Shift+Enter / Alt+Enter where the terminal reports them; paste keeps lines; the box grows to 12 lines | **added** |
| `/` opens a command menu; ↑/↓ choose, Enter runs, Tab completes | the same; arguments are completed too (agents for `/model`, modes for `/planning`, run ids, themes, `/memory` actions) | **added** (was hints + Tab) |
| `@` mentions a file, with a fuzzy menu | `@` lists the files of the repository's HEAD — exactly what the agents' worktrees contain — name matches first | **added** |
| `#` saves a memory (CLAUDE.md) | `#` saves a *standing note* for this repository, appended to every goal started here; kept in `~/.config/multigenz/notes/`, never written into the repository; `/memory` lists, removes, clears | **added** |
| `?` shows the keyboard shortcuts | the same | **added** |
| ↑/↓ prompt history, kept across sessions | the same; stored 0600 in the state directory with secrets from the environment redacted | **added** (was per session) |
| Esc clears / interrupts | Esc closes the menu, then clears the prompt, then offers to cancel the run in progress | **added** |
| Ctrl+L clears, Ctrl+C / Ctrl+D quit | Ctrl+L clears, Ctrl+Q quits (`/quit`, `/exit`) | had |

## Working on a task
| Claude Code | MultiGenz | |
|---|---|---|
| Shift+Tab cycles modes, including *plan mode* (look first, change nothing until you accept) | Shift+Tab switches between **run** and **plan first**: the planner agent plans read-only, the plan is shown (tasks, files each may change, order), nothing runs until you confirm; `/plan GOAL` does it once | **added** |
| A spinner ("✻ Thinking… 12s") and a todo checklist while it works | a spinner with the elapsed time, tasks done, agents working, and a checklist of the run's tasks with the agent on each, inline under the messages | **added** (was a separate screen) |
| Notification when it is done | a summary in the messages (what was integrated, the branch and commit, failures with reasons, the report) and the terminal bell | **added** |
| Ctrl+O shows more detail | Ctrl+O opens the run's dashboard: a live pane per agent working at the same time (split view) | **added** key (split view existed) |
| Sub-agents work in the background | several agents work in parallel, each in its own sandbox and worktree; their output side by side in the split view | had |
| Status line (model, directory, …) | status line: mode, repository and branch, planning mode, number of agents, runs in progress | **added** |
| `/model` | `/model`, Team screen `m`: each agent's model, choices asked from the tools themselves | had |
| `/agents` (sub-agents) | `/agents` (installed agent CLIs and whether they work here), `/team` (who plans, codes, reviews) | had |

## Sessions and housekeeping
| Claude Code | MultiGenz | |
|---|---|---|
| `/resume`, `--continue` | `/resume [RUN]`: continues an interrupted run (leases, processes and worktrees reconciled first; finished work is not redone) and follows it inline | **added** (was CLI only) |
| `/cost`, `/usage` | `/cost`: what recent runs cost; API agents are metered, CLI subscriptions are reported as not measurable | **added** |
| `/status` | `/status`: versions, repository, policy, state directory, sandbox and network, planning, agents, pushing (disabled), unfinished runs | **added** |
| `/export` | `/export [PATH]`: the session's messages to a file (0600, secrets redacted) and the clipboard | **added** |
| `/theme` | `/theme [NAME]`: any Textual theme; remembered in `~/.config/multigenz/tui.json` | **added** |
| `/doctor`, `/init`, `/help`, `/clear`, `/config` | `/doctor`, `/init`, `/help`, `/clear`; configuration through `/team`, `/model`, `/planning`, `/providers` | had |
| `/cancel` (Esc) | `/cancel [RUN]` or Esc: agents are stopped, work done so far is kept on the run's branches | **added** |

## Deliberately not added
| Claude Code | Why not in MultiGenz |
|---|---|
| `!` bash mode (run a shell command from the prompt) | MultiGenz never executes shell commands typed into its prompt or proposed by agents; tests run only from administrator-defined profiles (AGENTS.md). Use your terminal. |
| Permission prompts per edit / *accept edits* mode | Agents never touch your working tree: every change is made in a sandboxed worktree, validated by your tests and merged only on `multigenz/run-<id>/…`; you review and merge the branch yourself. |
| `/compact`, `/context`, rewind (Esc Esc) | There is no single long conversation: each task is a fresh agent run with its own prompt; a run's history is its event log (`runs events`, `runs audit`). |
| MCP servers, hooks, output styles, `/permissions` | These configure one agent. Each agent CLI keeps its own (opencode and Hermes plugins are mounted into the sandbox; see OPERATIONS.md). |
| Image paste, vim mode, IDE integration | Not needed for writing a goal; may come later. |
| `/login` | Each agent CLI uses its own login; API keys go to `~/.config/multigenz/secrets.env` (0600) through `/providers`. |
