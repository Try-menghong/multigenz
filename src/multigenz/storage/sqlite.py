"""SQLite (WAL) state store with transactional, hash-chained, redacted event records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from multigenz.core.redaction import Redactor
from multigenz.storage.migrations import migrate


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def event_hash(prev: str, run_id: str, seq: int, ts: str, kind: str, task_id: str | None,
               attempt_id: str | None, payload: str) -> str:
    raw = "\x1f".join([prev, run_id, str(seq), ts, kind, task_id or "", attempt_id or "", payload])
    return hashlib.sha256(raw.encode()).hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Store:
    def __init__(self, path: Path, redactor: Redactor | None = None, *, readonly: bool = False):
        self.path = Path(path)
        self.redactor = redactor or Redactor()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), isolation_level=None, timeout=30.0)
        self.con.row_factory = sqlite3.Row
        self._depth = 0
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.execute("PRAGMA busy_timeout=30000")
        if not readonly:
            migrate(self.con)

    def close(self) -> None:
        self.con.close()

    # -- transactions --------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant IMMEDIATE transaction; only the outermost level commits/rolls back."""
        if self._depth == 0:
            self.con.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self.con
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.con.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.con.execute("COMMIT")

    def q(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def q1(self, sql: str, params: tuple | dict = ()) -> dict | None:
        r = self.con.execute(sql, params).fetchone()
        return dict(r) if r else None

    # -- runs ------------------------------------------------------------------------------
    def create_run(self, **f: Any) -> None:
        ts = now_iso()
        f.setdefault("created_at", ts)
        f.setdefault("updated_at", ts)
        cols = ", ".join(f)
        marks = ", ".join(f":{k}" for k in f)
        with self.tx():
            self.con.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", f)

    def get_run(self, run_id: str) -> dict | None:
        return self.q1("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def list_runs(self, limit: int = 100) -> list[dict]:
        return self.q("SELECT run_id, objective, status, repo_path, created_at, finished_at, integration_branch, "
                      "final_commit, cancel_requested FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                      (limit,))

    def update_run(self, run_id: str, **f: Any) -> None:
        f["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=:{k}" for k in f)
        with self.tx():
            self.con.execute(f"UPDATE runs SET {sets} WHERE run_id=:_run", {**f, "_run": run_id})

    def resolve_run_id(self, prefix: str) -> str | None:
        rows = self.q("SELECT run_id FROM runs WHERE run_id LIKE ? ESCAPE '\\' LIMIT 2",
                      (prefix.replace("\\", "\\\\").replace("%", "\\%") + "%",))
        return rows[0]["run_id"] if len(rows) == 1 else None

    # -- tasks -------------------------------------------------------------------------------
    def insert_task(self, run_id: str, task_id: str, spec_json: str, status: str) -> None:
        with self.tx():
            self.con.execute("INSERT INTO tasks (run_id, task_id, spec, status, updated_at) VALUES (?,?,?,?,?)",
                             (run_id, task_id, spec_json, status, now_iso()))

    def get_task(self, run_id: str, task_id: str) -> dict | None:
        return self.q1("SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id))

    def list_tasks(self, run_id: str) -> list[dict]:
        return self.q("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (run_id,))

    def update_task(self, run_id: str, task_id: str, **f: Any) -> None:
        f["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=:{k}" for k in f)
        with self.tx():
            self.con.execute(f"UPDATE tasks SET {sets} WHERE run_id=:_run AND task_id=:_task",
                             {**f, "_run": run_id, "_task": task_id})

    # -- attempts -----------------------------------------------------------------------------
    def insert_attempt(self, **f: Any) -> None:
        cols = ", ".join(f)
        marks = ", ".join(f":{k}" for k in f)
        with self.tx():
            self.con.execute(f"INSERT INTO attempts ({cols}) VALUES ({marks})", f)

    def get_attempt(self, attempt_id: str) -> dict | None:
        return self.q1("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))

    def update_attempt(self, attempt_id: str, **f: Any) -> None:
        sets = ", ".join(f"{k}=:{k}" for k in f)
        with self.tx():
            self.con.execute(f"UPDATE attempts SET {sets} WHERE attempt_id=:_id", {**f, "_id": attempt_id})

    def list_attempts(self, run_id: str, task_id: str | None = None) -> list[dict]:
        if task_id:
            return self.q("SELECT * FROM attempts WHERE run_id=? AND task_id=? ORDER BY rowid", (run_id, task_id))
        return self.q("SELECT * FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,))

    def active_attempts(self, run_id: str) -> list[dict]:
        return self.q("SELECT * FROM attempts WHERE run_id=? AND status IN ('leased','running') ORDER BY rowid",
                      (run_id,))

    # -- events ---------------------------------------------------------------------------------
    def emit(self, run_id: str, kind: str, *, task_id: str | None = None, attempt_id: str | None = None,
             payload: dict | None = None) -> int:
        """Append a redacted event; sequence number and hash chain are assigned transactionally."""
        clean = self.redactor.redact_obj(payload or {})
        body = canonical(clean)
        ts = now_iso()
        with self.tx():
            row = self.con.execute("SELECT event_seq, last_event_hash FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown run {run_id}")
            seq = row["event_seq"] + 1
            h = event_hash(row["last_event_hash"], run_id, seq, ts, kind, task_id, attempt_id, body)
            self.con.execute("INSERT INTO events (run_id, seq, ts, kind, task_id, attempt_id, payload, prev_hash, hash) "
                             "VALUES (?,?,?,?,?,?,?,?,?)",
                             (run_id, seq, ts, kind, task_id, attempt_id, body, row["last_event_hash"], h))
            self.con.execute("UPDATE runs SET event_seq=?, last_event_hash=?, updated_at=? WHERE run_id=?",
                             (seq, h, ts, run_id))
        return seq

    def events(self, run_id: str, *, after_seq: int = 0, limit: int | None = None,
               kinds: tuple[str, ...] | None = None, task_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE run_id=? AND seq>?"
        params: list[Any] = [run_id, after_seq]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += list(kinds)
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        sql += " ORDER BY seq"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        out = self.q(sql, tuple(params))
        for e in out:
            e["payload"] = json.loads(e["payload"])
        return out

    # -- misc tables --------------------------------------------------------------------------------
    def add_artifact(self, run_id: str, kind: str, path: str, *, task_id: str | None = None,
                     attempt_id: str | None = None, sha256: str | None = None, size: int | None = None) -> None:
        with self.tx():
            self.con.execute("INSERT INTO artifacts (run_id, task_id, attempt_id, kind, path, sha256, size, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?)", (run_id, task_id, attempt_id, kind, path, sha256, size, now_iso()))

    def list_artifacts(self, run_id: str) -> list[dict]:
        return self.q("SELECT * FROM artifacts WHERE run_id=? ORDER BY artifact_id", (run_id,))

    def add_integration(self, run_id: str, task_id: str, attempt_id: str | None, status: str, candidate: str,
                        base_head: str | None, detail: str = "") -> int:
        ts = now_iso()
        with self.tx():
            cur = self.con.execute(
                "INSERT INTO integrations (run_id, task_id, attempt_id, status, candidate_commit, base_head, detail, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, attempt_id, status, candidate, base_head, detail, ts, ts))
            return int(cur.lastrowid)

    def update_integration(self, integration_id: int, **f: Any) -> None:
        f["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=:{k}" for k in f)
        with self.tx():
            self.con.execute(f"UPDATE integrations SET {sets} WHERE integration_id=:_id", {**f, "_id": integration_id})

    def list_integrations(self, run_id: str) -> list[dict]:
        return self.q("SELECT * FROM integrations WHERE run_id=? ORDER BY integration_id", (run_id,))

    def set_provider_state(self, run_id: str, provider: str, status: str, reason: str = "") -> None:
        with self.tx():
            self.con.execute(
                "INSERT INTO provider_state (run_id, provider, status, reason, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(run_id, provider) DO UPDATE SET status=excluded.status, reason=excluded.reason, "
                "updated_at=excluded.updated_at", (run_id, provider, status, reason, now_iso()))

    def provider_states(self, run_id: str) -> dict[str, dict]:
        return {r["provider"]: r for r in self.q("SELECT * FROM provider_state WHERE run_id=?", (run_id,))}


def wait_until(pred, timeout: float, interval: float = 0.05) -> bool:  # small helper for tests/CLI
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()
