# Testing

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest                       # everything (~3 min)
pytest -m acceptance         # AT-01 .. AT-16
pytest -m e2e                # scenarios A, B (repaired / unfixable), C
pytest tests/unit tests/contract tests/integration tests/security
scripts/verify_clean_install.sh   # builds the wheel, installs it into a FRESH venv, runs the whole suite against it
pytest -m cli_agents         # real Codex / opencode / Cline / Hermes binaries end to end against a local mock model
MULTIGENZ_TEST_INSTALLER=1 pytest tests/integration/test_installer.py   # real install into a throwaway HOME (downloads deps)
```
Tests marked `needs_userns` are skipped (not faked) when unprivileged user namespaces are unavailable. `cli_agents` tests
look for the binaries on `$MULTIGENZ_TEST_CLI_PATH` (default `$PATH` plus `/opt/agentclis/bin`) and skip missing ones.
`scripts/smoke_agents.sh` is the manual counterpart with REAL providers: it runs every enabled agent of your policy on a
scratch repository and reports whether each produced a validated commit.
The site test uses Chromium through Node's `playwright` package when available.

## Layout
`tests/unit` (contracts, config, DAG, scheduler, state machine, sandbox, git, adapters), `tests/integration` (M1–M5 per
milestone), `tests/acceptance` (AT-01..16 through the real stack and sandbox), `tests/e2e` (scenarios A–C with a real
FastAPI service), `tests/security` (regressions), `tests/contract`, `tests/support` (scripted agent, helpers).

## What is simulated
- **Agents are scripted stand-ins** (`tests/support/scripted_agent`): a deterministic program that, like a CLI agent, is
  discovered via `--help`, invoked through the adapter inside the sandbox, receives a prompt file, writes files and exits.
  Planner and reviewer are the same program emitting JSON. There is no LLM, so these tests prove orchestration, isolation,
  validation, merging and recovery — not the quality of any model's output.
- The FastAPI authentication service in `tests/e2e/fixtures` is real code, tested by real pytest runs (inside the sandbox and
  again on the host); its "agent authorship" is scripted.
- LiteLLM is exercised with an injected completion function.
- **Real agent CLIs, scripted model.** `tests/e2e/test_cli_agents.py` runs the actual Codex, opencode and Cline binaries inside
  the sandbox, configured through their own login/config files (Cline's written by `cline auth` itself), against
  `tests/support/mock_llm.py`: an OpenAI-compatible server (chat completions and the Responses API) that answers the
  planner prompt with a JSON plan and a coding prompt with one tool call that creates a file, using whatever shell or
  write tool the CLI offered. This proves the CLI integration (stdin prompts, unattended flags, sandbox mounts, logins,
  plan → code → validate → integrate, mixed vendors in one run); it says nothing about model quality.
- **Roles.** `tests/unit/test_roles.py` (routing rules), `tests/integration/test_roles_run.py` (orchestrated runs where
  three identical scripted agents could each do everything, so only the roles explain who planned, coded and reviewed),
  `tests/integration/test_tui_team.py` (the Team screen driven through Textual's pilot: assign, reorder, refuse an
  impossible assignment, disable, add a preset, confirm before discarding, save) and, with the real CLIs,
  `test_roles_pick_planner_worker_and_reviewer_across_vendors` (Cline plans, Codex codes, opencode reviews).
- **Usage.** `tests/integration/test_split.py` (planning: auto with scripted agents: a small goal is one agent run and
  no planner call; a requested split runs its subtasks in dependency order; invalid requests are rejected with reasons and
  corrected; a request with file changes is ignored; `never` honours no request), `tests/unit/test_lean_planning.py`
  (plan-only prompt, read-only planner tools, plan timeout) and, with real opencode, auto planning for a small goal
  (1 coding run, the model never sees a planning prompt) and for a split (3 coding runs, both parts integrated).
- **Hermes and plugins.** Hermes Agent 0.19 runs plan → code → integrate in the sandbox (`test_single_cli_plans_codes_and_integrates[hermes]`);
  an opencode local plugin mounted from the config directory demonstrably runs inside the sandbox.
- **API providers.** `tests/unit/test_providers.py` (0600 secrets file, environment precedence, generated configs reference
  keys by name only, validation), `tests/integration/test_tui_providers.py` (TUI: add provider with a hidden key → models
  fetched → filter → pick → agent added and shown on the Team screen; a wrong key is reported) and, with real opencode
  and LiteLLM, `test_api_provider_models_become_a_coding_agent_and_a_text_planner`: everything set up through
  `multigenz providers`, the mock provider refuses requests without the key, the planner model plans and the coder model
  builds through opencode, every call carries the key and the key appears nowhere under MultiGenz's state directory.
- **Models.** `tests/unit/test_models.py` (the model option is used only when the CLI's help shows it — as a whole option,
  not a look-alike like `--model-provider`; a CLI without one is refused before anything is saved; an admin invocation
  may place `{model}` itself; choices are read from `claude --help` and `opencode models`; every shipped CLI's recorded
  help lists `--model`) and, with the real binaries, `test_the_model_chosen_with_agents_model_is_the_one_the_cli_uses`
  for opencode, Codex, Cline and Hermes: the model is set with `multigenz agents model` (each CLI's own config defaults
  to a different model) and every model request the CLI then sends asks for the chosen one.
- **Home screen and pet.** `tests/unit/test_home.py` (the pet's shape and blink frame, the team summary with models,
  command hints, Tab completion, repository detection) and `tests/integration/test_tui_home.py` (pilot: /help, an
  unknown command, /planning, /repo refusing a non-repository, history, /team, /runs and back; /model directly, through
  the agent and model pickers and from the Team screen's `m`, a CLI without a model option refused; a goal typed at the
  prompt runs through the detached worker to a merged commit on its run branch while the checkout stays untouched).
- **Split view.** `tests/unit/test_panes.py` (following a growing log, starting late near the end, which runs get a
  pane, colours kept while other escape codes and secrets hidden between colour codes are not) and
  `tests/integration/test_split_view.py`: a goal typed at the home prompt, a scripted planner splits it into three
  tasks, three different agents work at once — the dashboard shows the planner's pane and then three live panes at the
  same time, each with only its own agent's lines (stdout and stderr), secrets redacted, and their final state when the
  run succeeds; the state directory's path itself contains a secret value, so panes must not depend on redacted event
  payloads. With the real binaries, `test_split_view_shows_three_real_clis_working_at_once`: opencode plans three tasks
  and opencode, Codex and Cline then work at the same time (the mock model waits 4 s per answer so they overlap); three
  panes are active at once, every pane shows output without raw escape codes, and all three files are integrated.
- `tests/contract/fixtures/help/` holds the real `--help` output of the CLI versions the built-in invocations were written
  for (agy's from a user's machine); the contract tests fail if a shipped invocation no longer verifies against them.

## Acceptance test map
| AT | Test | AT | Test |
|---|---|---|---|
| 01 cyclic plan rejected | `test_at_01_…` | 09 worktrees isolated | `test_at_09_…` |
| 02 concurrent execution | `test_at_02_…` | 10 conflict keeps originals | `test_at_10_…` |
| 03 dependents wait | `test_at_03_…` | 11 failing tests block integration | `test_at_11_…` |
| 04 provider reassignment | `test_at_04_…` | 12 original tree unchanged | `test_at_12_…` |
| 05 timeout kills process group | `test_at_05_…` | 13 no host credentials | `test_at_13_…` |
| 06 cancel preserves artifacts | `test_at_06_…` | 14 no writes outside sandbox | `test_at_14_…` |
| 07 restart reconciliation | `test_at_07_…` | 15 logs redacted | `test_at_15_…` |
| 08 failed agent ≠ success | `test_at_08_…` | 16 auditable/replayable transitions | `test_at_16_…` |

## Recorded results (2026-10-12)
| run | where | result |
|---|---|---|
| full suite with the split view (commit 204e840), dev tree, Python 3.13, `MULTIGENZ_TEST_INSTALLER=1`, shellcheck, unraisable-exception warnings as errors; includes real opencode, Codex and Cline working at once in the TUI | this container | 356 passed |
| full suite after the home screen, pet and model choice (commit b787dc0), dev tree, Python 3.13, `MULTIGENZ_TEST_INSTALLER=1`, shellcheck, unraisable-exception warnings as errors | this container | 349 passed |
| `scripts/verify_clean_install.sh` for the same commit (wheel in a fresh Python 3.12.3 venv) | this container | 348 passed, 1 skipped (install-from-checkout needs `src/`) |
| full suite, dev tree, Python 3.13, with `MULTIGENZ_TEST_INSTALLER=1`, shellcheck and unraisable-exception warnings as errors (roles, Team screen, planning modes, Hermes, plugins, API providers) | this container | 324 passed |
| `scripts/verify_clean_install.sh` (wheel in a fresh Python 3.12.3 venv) | this container | 323 passed, 1 skipped (install-from-checkout needs `src/`) |
| `scripts/smoke_agents.sh` with codex, opencode, cline pointed at the mock model | this container | 3 of 3 validated |

Both suite runs include the real Codex 0.162.1, opencode 1.19.0 and Cline 3.0.70 binaries (`tests/e2e/test_cli_agents.py`,
against the local mock model; Codex with its own bubblewrap sandbox nested inside MultiGenz's), the installer
(`curl | bash` from the served page, install/upgrade/uninstall into throwaway HOMEs) and the download page in Chromium.
The installer was also run by hand from GitHub (`MULTIGENZ_REF=<branch>`) with uv and with the system Python.
Not covered here: real providers behind Codex/opencode/Cline (this environment's network policy blocks them, e.g.
`opencode.ai`), and agy (not installable here). Run `scripts/smoke_agents.sh` on a machine with your logins for that.

## Earlier results (2026-10-11)
`scripts/verify_clean_install.sh` — wheel built, installed into a brand-new Python 3.12.3 virtualenv, the whole suite run
against the *installed* package (the script asserts `multigenz` resolves to site-packages, not `src/`):

| run | result |
|---|---|
| 1 | 244 passed |
| 2 | 245 passed (adds the interrupted-conflict regression test) |
| 3 | 245 passed |

Includes all 16 acceptance tests (`tests/acceptance`) and the E2E scenarios A, B (repaired), B (unfixable → reported as
failure) and C. Every run also executed `multigenz doctor --policy config/policy.userns.example.yaml` (all required checks ok).

Limits of this evidence: the "clean environment" is a fresh virtualenv on the same Linux container that was used for
development (kernel with unprivileged user namespaces, Python 3.12 and 3.13 available, git 2.43), not a separate CI
machine; the container backend and real agent CLIs were not exercised (see README). Two bugs found by these tests and fixed
along the way are worth knowing as evidence the suite bites: `git diff --raw` abbreviations silently disabled the file-size
limit, and a repaired task's failed intermediate snapshot used to enter integration history.
