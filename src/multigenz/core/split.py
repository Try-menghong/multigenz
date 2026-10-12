"""Subagents on demand (``orchestrator.planning: auto``).

The run starts with ONE task covering the whole goal. Its agent either does the work (the cheapest outcome: one agent
run in total) or, when the goal is too large, changes nothing and replies with ``{"subtasks": [...]}``. MultiGenz
validates that request exactly like a planner's plan (schema, DAG, path ownership, policy, capabilities, limits) and runs
each subtask with its own agent. The request is untrusted model output: it can only ask; it grants nothing.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from multigenz.config import Policy
from multigenz.contracts.task import PlanDocument, TaskSpec
from multigenz.core.dag import validate_plan

SPLIT_KEY = "subtasks"
GOAL_TASK_ID = "goal"


def goal_plan(objective: str, policy: Policy) -> PlanDocument:
    """The single whole-repository task a run starts with when planning is ``auto`` or ``never``."""
    title = " ".join(objective.split())[:80] or "goal"
    return PlanDocument(summary="one agent takes the whole goal" + (
        "; it may hand parts to subagents" if policy.orchestrator.planning == "auto" else ""), tasks=[TaskSpec(
            task_id=GOAL_TASK_ID, title=title, objective=objective, allowed_paths=["**"],
            required_capabilities={"coding"}, max_attempts=min(5, policy.orchestrator.max_task_attempts),
            timeout_seconds=policy.orchestrator.task_timeout_seconds,
            may_split=policy.orchestrator.planning == "auto")])


def split_prompt(max_subtasks: int, capabilities: list[str]) -> str:
    example = {SPLIT_KEY: [
        {"task_id": "api", "title": "short title", "objective": "what to build and why",
         "allowed_paths": ["app/api.py", "tests/test_api.py"], "dependencies": [], "required_capabilities": ["coding"]},
        {"task_id": "docs", "title": "short title", "objective": "...", "allowed_paths": ["docs/api.md"],
         "dependencies": ["api"], "required_capabilities": ["coding"]}]}
    return f"""## Doing it yourself, or asking for subagents
You are the only agent on this goal so far. Doing the work yourself is the cheapest path: do that whenever the goal
fits one focused change.

Only if the goal is clearly too big for one agent - several large parts that are independent (they could be built in
parallel) or need different capabilities - do NOT change any files. Instead reply with ONE JSON object and stop:
{json.dumps(example, indent=2)}
Rules: 2 to {max_subtasks} subtasks; task_id matches ^[a-z0-9][a-z0-9_-]{{0,47}}$; allowed_paths are repo-relative files
or directories ending in '/'; subtasks that do not depend on each other must not share allowed_paths; each subtask
changes at least one file and puts its own tests in its allowed_paths; required_capabilities may only use
{sorted(capabilities)}. Every subtask costs a full agent run, so split only when it clearly pays off. If you change
files, any subtask request is ignored and your changes are validated as usual.
"""


def find_split_request(text: str) -> list | None:
    """The ``subtasks`` list of the first JSON object in ``text`` that has one (fenced, bare or inside prose)."""
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get(SPLIT_KEY), list):
            return obj[SPLIT_KEY]
        i = text.find("{", i + 1)
    return None


def build_subplan(raw: list, *, policy: Policy, engine=None, existing_ids: set[str],
                  capabilities: list[set[str]] | None) -> tuple[PlanDocument | None, list[str]]:
    """Validate an agent's subtask request like a planner's plan. Returns (plan, []) or (None, issues)."""
    limit = policy.orchestrator.max_subtasks
    if len(raw) < 2:
        return None, ["a split needs at least 2 subtasks; if the goal fits one task, do it yourself"]
    if len(raw) > limit:
        return None, [f"{len(raw)} subtasks requested; the limit is {limit}"]
    try:
        plan = PlanDocument.model_validate({"tasks": raw})
    except ValidationError as exc:
        return None, [f"subtask {e['loc'][1] if len(e['loc']) > 1 else ''} {'.'.join(map(str, e['loc'][2:]))}: "
                      f"{e['msg']}".replace("  ", " ") for e in exc.errors()][:10]
    clash = sorted({t.task_id for t in plan.tasks} & existing_ids)
    if clash:
        return None, [f"subtask ids already used in this run: {clash}"]
    issues = validate_plan(plan, engine, available_capabilities=capabilities)
    if issues:
        return None, issues
    for t in plan.tasks:  # administrator limits; a subtask can never split again
        t.may_split = False
        if "max_attempts" not in t.model_fields_set:
            t.max_attempts = min(5, policy.orchestrator.max_task_attempts)
        if "timeout_seconds" not in t.model_fields_set:
            t.timeout_seconds = policy.orchestrator.task_timeout_seconds
    return plan, []
