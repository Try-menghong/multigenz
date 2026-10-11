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
`requires_help_tokens` appears in the binary's own help output. Claude ships two candidate invocations (checked against
`claude --help` at probe time); Codex and Antigravity have none, so they stay disabled until you add `invocations`:
```yaml
agents:
  codex:
    adapter: cli
    command: codex
    capabilities: [coding, testing]
    invocations:
      - name: exec
        argv: ["{command}", "<flags>", "{prompt_file}"]   # placeholders: {command} {prompt_file} {task_file} {task_id} {workspace}
        stdin: none                                      # or `prompt` to pipe the prompt on stdin
        requires_help_tokens: ["<each flag used>"]
        modes: [code, conflict]                          # which roles this invocation may serve
```
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
