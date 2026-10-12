# Operations guide

## Requirements
Linux, Python ≥ 3.12, git ≥ 2.38 (`merge-tree --write-tree`), SQLite with WAL, and one isolation backend:
a rootless `podman`/`docker`, or unprivileged user namespaces plus util-linux `unshare` (the built-in `userns` backend).
`multigenz doctor` checks all of this.

## Policy
The policy YAML is administrator-owned, parsed strictly (unknown keys are errors) and fails closed. Lookup order:
`--policy`, `$MULTIGENZ_POLICY`, `~/.config/multigenz/policy.yaml`, built-in defaults. A run stores a snapshot of the
policy (JSON + sha256) at creation; `runs resume` uses the *snapshot*, so editing the file cannot loosen a running run.

Key rules enforced at load time: `git.auto_push` must be `false`; `git.protect_default_branch` must be `true`;
`execution.isolation: none` additionally requires `allow_unsandboxed: true` (and unattended agent invocations refuse to run
unsandboxed regardless).

### Agents
`adapter: cli` agents run in the sandbox. **No CLI flag is assumed.** An invocation is enabled only when every string in its
`requires_help_tokens` appears in the binary's own help output (`help_args`, default `--help`). Built-in candidates:

| Adapter (by agent name or command basename) | Built-in invocations | Verified with |
|---|---|---|
| `claude` | `--print --permission-mode acceptEdits` / `plan` | real model runs |
| `opencode` | `run --auto` (code) / `run --agent plan` (read-only roles) | real binary + local scripted model, in the sandbox |
| `cline` | `--yolo` (code) / `--plan` (read-only roles) | real binary + local scripted model, in the sandbox |
| `codex` | none: written only by `multigenz agents add codex --trust-outer-sandbox` | help text only |
| `antigravity` / `agy` | none: `multigenz agents add agy` writes experimental candidates | help text only |

All of them read the prompt on stdin. The easy way to configure them is `multigenz init` (all supported CLIs found on
PATH) or `multigenz agents add <preset>`; both validate the result with the real policy loader and keep a `.bak` copy.
`multigenz agents presets` shows each preset's test status and install hint.

**Codex.** `codex exec` works unattended, but Codex's own sandbox needs bubblewrap to create a nested user namespace,
which fails inside MultiGenz's sandbox (`bwrap: setting up uid map: Operation not permitted`; the legacy Landlock mode
also requires bubblewrap in Codex 0.162). Every shell command Codex runs then fails, so inside MultiGenz Codex can only
work with its inner sandbox off (`--sandbox danger-full-access`), confined by the MultiGenz sandbox alone. That is your
decision: `multigenz agents add codex --trust-outer-sandbox`. This path has not been executed in development.

**Logins.** Presets hand an agent its own login by name: environment variables (`credentials.env`) and files mounted
**read-only** at the same path under the sandbox home (`credentials.files`, e.g. `~/.codex/auth.json` →
`/home/agent/.codex/auth.json`). Missing ones are skipped. A CLI that wants to rewrite its login file inside the sandbox
(Cline prints `failed to persist selection`) keeps working; if a login token expires, refresh it by running the CLI once on
the host.

**Installs outside /usr.** For npm-installed CLIs the sandbox automatically exposes the `node_modules` root the binary
lives in and the directory of its `#!/usr/bin/env node` interpreter (never a whole prefix such as `~/.local`). Anything
else goes in `execution.readonly_paths`.

Any other CLI with a non-interactive mode can be added by hand:
```yaml
agents:
  my-agent:
    adapter: cli
    command: my-agent
    capabilities: [coding, testing]
    invocations:
      - name: exec
        argv: ["{command}", "<flags>"]                   # placeholders: {command} {prompt_file} {task_file} {task_id} {workspace}
        stdin: prompt                                    # or `none` and pass {prompt_file}
        help_args: ["--help"]                            # e.g. ["exec", "--help"] for a subcommand
        requires_help_tokens: ["<each flag used>"]
        modes: [code, conflict]                          # roles: code, conflict, plan, review, analysis
```
The planner needs an agent with the `architecture` capability and a read-only (`plan`) invocation.
`credentials.env` / `credentials.files` grant a *named* host variable/file to *that agent only*; nothing else from the host
environment is ever passed. `adapter: litellm` agents run in-process for planning/review text only and cannot produce commits.

### Validation profiles
Fixed argv lists run in the sandbox against an *exported copy* of the exact tree under test (no network, no `.git`).
`validation.default_profile` verifies each task result; `integration_profile` verifies the **merged tree** before it may
enter the integration branch; the integration head is tested once more at the end. A planner may name a profile via
`validation_profile` only if it exists in the policy. With `require_tests: true` and no profile, nothing can be accepted.
A tree with no tests at all fails a pytest profile (exit 5) — give the repository a baseline test.

## Run lifecycle and states
`created → planning → running → succeeded | failed | cancelled` (or `plan_rejected`). Tasks:
`pending → ready → running → validating → succeeded`, with `failed | blocked | cancelled` terminal; there is no
`running → succeeded` edge. A task is `succeeded` only after: clean agent exit, passing validation event for that attempt,
and a merged integration record. Retries: 2 charged attempts by default (`max_task_attempts`), provider failures
(missing binary / auth / sandbox-required) reassign to another compatible agent without charging the task, rate limits use
capped exponential backoff with jitter, interruptions are bounded separately, merge conflicts get one isolated resolution
attempt (`max_conflict_repairs`).

## State layout (`$MULTIGENZ_HOME`, default `~/.local/state/multigenz`, mode 0700)
```
multigenz.db                  SQLite WAL: runs, tasks, attempts, events (append-only, hash-chained), artifacts, integrations
runs/<run>/worktrees/         integration + one worktree per attempt (preserved; never removed)
runs/<run>/attempts/<id>/     logs/ (redacted stdout/stderr), inbox/ (prompt), sandbox/ (transient launcher files)
runs/<run>/tests/             test-profile logs;  report.json / report.md
```
Git branches live in the target repository under `multigenz/run-<id>/`: `integration`, `task-<task>` (first attempt),
`task-<task>.aN` (later attempts), `task-<task>.cN` (conflict resolutions). Each validated task lands on `integration` as
**one commit holding exactly the tested tree**, with `Multigenz-Task/-Attempt/-Candidate` trailers; intermediate attempt
snapshots stay on their own branches. Review or merge `integration` yourself — MultiGenz never pushes.

## Recovery
`multigenz runs resume <id>` acquires the run's file lock, terminates leftover agent process groups (verified by pid +
kernel start time), snapshots any interrupted work in Git, settles half-finished merges by asking Git, resumes tasks that were
mid-validation at validation, and only then schedules new attempts. `multigenz runs audit <id>` replays the event log and
checks sequence, hash chain, transition legality and equality with the stored state.

## Installer and download page
`install.sh` (also served as `/install` by the page in `site/`) installs into a private virtualenv in `~/.multigenz` and
links `~/.local/bin/multigenz`; see `bash install.sh --help` for the overrides (`MULTIGENZ_REF`, `MULTIGENZ_SOURCE`, ...).
It never uses sudo and only appends one `PATH` line to `~/.bashrc`/`~/.zshrc` (skip with `MULTIGENZ_NO_MODIFY_PATH=1`).
To publish the page: enable GitHub Pages with "GitHub Actions" as the source and get `site/`, `install.sh` and
`.github/workflows/pages.yml` onto `main`; the workflow checks that `site/install` equals `install.sh` and deploys.
GitHub Pages for a private repository needs a paid plan, and the installer itself can only fetch a private repository
over SSH, so a public download page in practice means a public repository (or hosting `site/` elsewhere).

## Running with real Claude Code
1. Install and verify the CLI: `claude --version`. Authentication: use an API key (`ANTHROPIC_API_KEY`); it is injected only
   into the sandbox of the agent that lists it under `credentials.env`. (Interactive OAuth login files can be granted with
   `credentials.files`, but they are mounted read-only, so token refresh inside the sandbox will not work, and a refresh
   attempted from the sandbox could invalidate your host login — prefer the token below.)
   **Subscription (Pro/Max), no API key:** run `claude setup-token` on the host, export the token it prints as
   `CLAUDE_CODE_OAUTH_TOKEN` and list that name under `credentials.env`. Subscription usage limits are shared by all
   parallel agents, and whether automated use fits your plan's terms is for you to check. Untested against the live service.
2. Start from `config/policy.claude.example.yaml`. `network: host` is required for the agent to reach the API (name
   resolution is provided read-only from the host); it means the agent has unrestricted network access.
3. Point `validation.profiles` at your repository's real test command. The repository needs at least one test already
   (a pytest/unittest run that collects nothing fails). Use a throwaway repository first.
4. `multigenz doctor --policy P` → `multigenz agents list --policy P` (shows which Claude invocations were verified) →
   `multigenz plan "<goal>" --repo R --policy P` → `multigenz run "<goal>" --repo R --policy P`; watch with `multigenz tui`.
Claude runs unattended with `--permission-mode acceptEdits`: it can edit files in its worktree but not run shell commands,
so it cannot run tests itself — the orchestrator runs the profile and feeds failures back. This path has not been exercised
against the live service in this repository's tests.

## Known planner behaviour
Planners (including real Claude) sometimes add a "review / run the tests" task that changes no files. Such a task can never
be verified and fails with `agent produced no changes`, making the whole run FAILED even though the real work was integrated
and verified. The planner prompt now forbids these tasks; if one still appears, the integration branch holds the good work
(`runs show` lists it) and you can merge that branch yourself.
