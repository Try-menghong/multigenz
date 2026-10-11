"""Structured event records persisted in the audit log."""

from __future__ import annotations

from pydantic import BaseModel, Field

# Event kinds. Free-form kinds are allowed but these are the ones the system relies on.
RUN_CREATED = "run.created"
RUN_TRANSITION = "run.transition"
TASK_TRANSITION = "task.transition"
ATTEMPT_STARTED = "attempt.started"
ATTEMPT_FINISHED = "attempt.finished"
AGENT_OUTPUT = "agent.output"
VALIDATION_RESULT = "validation.result"
INTEGRATION = "integration"
RECONCILE = "reconcile"
PROVIDER = "provider"


class EventRecord(BaseModel):
    run_id: str
    seq: int
    ts: str
    kind: str
    task_id: str | None = None
    attempt_id: str | None = None
    payload: dict = Field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""
