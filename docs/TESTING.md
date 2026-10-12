# Testing

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest                       # everything (~3 min)
pytest -m acceptance         # AT-01 .. AT-16
pytest -m e2e                # scenarios A, B (repaired / unfixable), C
pytest tests/unit tests/contract tests/integration tests/security
scripts/verify_clean_install.sh   # builds the wheel, installs it into a FRESH venv, runs the whole suite against it
pytest -m cli_agents         # real opencode / Cline (/ Codex) binaries driven end to end against a local mock model
MULTIGENZ_TEST_INSTALLER=1 pytest tests/integration/test_installer.py   # real install into a throwaway HOME (downloads deps)
```
Tests marked `needs_userns` are skipped (not faked) when unprivileged user namespaces are unavailable. `cli_agents` tests
look for the binaries on `$MULTIGENZ_TEST_CLI_PATH` (default `$PATH` plus `/opt/agentclis/bin`) and skip missing ones;
the Codex test additionally needs `MULTIGENZ_TEST_CODEX_OUTER_SANDBOX=1` because it runs Codex with its own sandbox off.
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
- **Real agent CLIs, scripted model.** `tests/e2e/test_cli_agents.py` runs the actual opencode and Cline binaries inside
  the sandbox, configured through their own login/config files (Cline's written by `cline auth` itself), against
  `tests/support/mock_llm.py`: an OpenAI-compatible server (chat completions and the Responses API) that answers the
  planner prompt with a JSON plan and a coding prompt with one tool call that creates a file, using whatever shell or
  write tool the CLI offered. This proves the CLI integration (stdin prompts, unattended flags, sandbox mounts, logins,
  plan → code → validate → integrate, mixed vendors in one run); it says nothing about model quality.
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
| full suite, dev tree, Python 3.13, with `MULTIGENZ_TEST_INSTALLER=1` and shellcheck | this container | 283 passed, 1 skipped (Codex opt-in) |
| `scripts/verify_clean_install.sh` (wheel in a fresh Python 3.12.3 venv) | this container | 282 passed, 2 skipped (Codex opt-in; install-from-checkout needs `src/`) |

Both include the real opencode 1.19.0 and Cline 3.0.70 binaries (`tests/e2e/test_cli_agents.py`, against the local mock
model), the installer (`curl | bash` from the served page, install/upgrade/uninstall into throwaway HOMEs) and the
download page in Chromium. The installer was also run by hand from GitHub (`MULTIGENZ_REF=<branch>`) with uv and with
the system Python. Not covered: Codex inside MultiGenz (needs your opt-in), agy (not installable here), real providers
behind opencode/Cline.

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
