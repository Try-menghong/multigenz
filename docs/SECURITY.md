# Security model

Assets: host credentials and files, the user's repository (working tree, branches), host kernel state, the orchestrator's
policy and database. Untrusted: agent processes, their output, repository content, planner output, validation feedback.

## Controls (each has an automated test)
| Control | Mechanism | Tests |
|---|---|---|
| Policy outside the sandbox | policy file, DB and orchestrator memory are never mounted; models only produce proposals validated by `PolicyEngine` | AT-13, regressions |
| Fail closed | required isolation unavailable ⇒ the run is refused before anything is created; `isolation: none` needs explicit opt-in | M1, regressions |
| Sandbox (`userns`) | new user/mount/pid/ipc/uts/net namespaces; tmpfs root; `/usr` etc. read-only; one writable `/workspace`; private `/tmp`; no `/home`, `/root`, `/var`; `/proc/sys` read-only, kernel interfaces masked; all capability sets zero; `no_new_privs`; rlimits | AT-13, AT-14, unit |
| Sandbox (container) | rootless podman/docker: `--cap-drop ALL`, `no-new-privileges`, `--read-only`, pids/memory limits, workspace-only mount, secrets via env-file not argv | argv unit tests only |
| Credential isolation | the agent environment is built from scratch; only allowlisted variables of *that* agent are injected | AT-13 |
| Redaction | known values (env names that look sensitive, granted credentials; plain/URL/base64) + token patterns + PEM blocks, applied before logs/events/reports are written and again when shown | AT-15, TUI |
| Git safety | hooks/fsmonitor/pagers disabled; wrapper refuses push/reset/clean/rebase/checkout/force/delete flags and `worktree` other than add/list; branch writes only in `multigenz/run-<id>/`; explicit `--git-dir` defeats tampered `.git` pointers; original tree opened read-only (`GIT_OPTIONAL_LOCKS=0`) | AT-09, AT-12, regressions |
| Verification | exit code ≠ success: diff vs. base, path ownership (incl. deletions), forbidden paths, symlink/gitlink/mode checks, size limit, credential scan, deterministic review, admin test profile on the exported tree, merged-tree test before integration | AT-08, AT-11 |
| No planner-run commands | `acceptance_commands` are stored, shown, never executed; profiles come from policy | M3 |
| Audit | append-only events (DB triggers), hash chain, replay/verify | AT-16 |
| State privacy | state directories created 0700 | AT-15 |

## Residual risks (read these)
- **`userns` is not a complete sandbox.** No seccomp filter: the agent can issue any syscall the kernel allows an
  unprivileged namespaced process, including creating nested user namespaces; kernel vulnerabilities are in scope. No
  cgroup memory/CPU ceilings (only wall-clock timeout, `RLIMIT_FSIZE/NOFILE/CPU`, optional `NPROC/AS`, tmpfs size caps).
  Use the container backend for stronger, resource-limited isolation.
- **Network.** `userns` supports `none` (default for tests) or `host` (explicit, unrestricted). A hosted-model CLI needs
  network access, so with `userns` it requires `network: host`. Restricted egress needs the container backend with an
  administrator-defined network; `restricted` is rejected by `userns`.
- **A granted credential is visible to that agent.** It is redacted from everything MultiGenz records, but the agent
  process (and anything it writes into its worktree, which is preserved) can contain it. Worktrees live in a 0700 tree.
- **Agent-written files in preserved worktrees** are kept for diagnosis; they are not scanned or redacted.
- **The container backend has not been exercised live** in this repository's test environment. Real agent CLIs have:
  Claude Code with a real model (by a user), opencode and Cline against a local scripted model.
- **Codex with `--trust-outer-sandbox` runs with its own sandbox off.** Its commands are then confined only by the
  MultiGenz sandbox (read-only system, one writable worktree, host network). It is never enabled without that flag.
- **opencode's `plan` agent can still write files** through its shell tool; read-only roles are safe only because they run
  on a disposable export of the tree, never on a task worktree.
- Spend on CLI/subscription agents is unmeasurable and reported as *unknown*; the budget is enforced for measurable API calls only.
- Deterministic review heuristics (deleted tests, skip markers, removed assertions) are not a substitute for human review of the
  integration branch before you merge it.
