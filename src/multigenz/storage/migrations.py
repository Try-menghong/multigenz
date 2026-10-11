"""Schema migrations (PRAGMA user_version). Forward-only; each step runs in one transaction."""

from __future__ import annotations

import sqlite3

MIGRATIONS: list[str] = [
    # ---- v1 -------------------------------------------------------------------------
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        objective TEXT NOT NULL,
        repo_path TEXT NOT NULL,
        repo_identity TEXT NOT NULL,
        base_commit TEXT NOT NULL,
        base_branch TEXT,
        mode TEXT NOT NULL,
        policy_snapshot TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL,
        budget_usd REAL,
        spent_usd REAL NOT NULL DEFAULT 0,
        cost_unknown INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        integration_branch TEXT NOT NULL,
        integration_head TEXT,
        final_commit TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        event_seq INTEGER NOT NULL DEFAULT 0,
        last_event_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        report TEXT
    );
    CREATE TABLE tasks (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT NOT NULL,
        spec TEXT NOT NULL,
        status TEXT NOT NULL,
        provider TEXT,
        excluded_providers TEXT NOT NULL DEFAULT '[]',
        charged_attempts INTEGER NOT NULL DEFAULT 0,
        total_attempts INTEGER NOT NULL DEFAULT 0,
        interruptions INTEGER NOT NULL DEFAULT 0,
        rate_limit_retries INTEGER NOT NULL DEFAULT 0,
        conflict_repairs INTEGER NOT NULL DEFAULT 0,
        not_before REAL,
        base_commit TEXT,
        head_commit TEXT,
        branch TEXT,
        feedback TEXT,
        repair_from TEXT,
        failure_reason TEXT,
        integrated_commit TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (run_id, task_id)
    );
    CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT NOT NULL,
        number INTEGER NOT NULL,
        kind TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        lease_owner TEXT,
        lease_expires REAL,
        pid INTEGER,
        pgid INTEGER,
        proc_start TEXT,
        sandbox_id TEXT,
        sandbox_identity TEXT,
        worktree TEXT,
        gitdir TEXT,
        branch TEXT,
        base_commit TEXT,
        commit_sha TEXT,
        exit_code INTEGER,
        error_code TEXT,
        cost_usd REAL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        log_dir TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id)
    );
    CREATE UNIQUE INDEX one_active_lease_per_task ON attempts(run_id, task_id)
        WHERE status IN ('leased', 'running');
    CREATE TABLE events (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        seq INTEGER NOT NULL,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        task_id TEXT,
        attempt_id TEXT,
        payload TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TABLE artifacts (
        artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT,
        attempt_id TEXT,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT,
        size INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE integrations (
        integration_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        attempt_id TEXT,
        status TEXT NOT NULL,
        candidate_commit TEXT NOT NULL,
        base_head TEXT,
        result_commit TEXT,
        detail TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY,
        run_id TEXT,
        action TEXT NOT NULL,
        status TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT
    );
    CREATE TABLE provider_state (
        run_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (run_id, provider)
    );
    CREATE INDEX idx_attempts_run ON attempts(run_id, task_id);
    CREATE INDEX idx_events_kind ON events(run_id, kind);
    """,
]


def migrate(con: sqlite3.Connection) -> None:
    current = con.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        con.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split(script):
                con.execute(stmt)
            con.execute(f"PRAGMA user_version = {version}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def _split(script: str) -> list[str]:
    """Split on semicolons that terminate a statement (triggers contain one inside BEGIN..END)."""
    out, buf, in_trigger = [], [], False
    for line in script.splitlines():
        buf.append(line)
        stripped = line.strip().upper()
        if stripped.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if stripped.endswith("END;"):
                out.append("\n".join(buf))
                buf, in_trigger = [], False
        elif line.strip().endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf))
    return [s for s in out if s.strip()]
