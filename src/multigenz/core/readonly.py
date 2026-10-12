"""Run an agent in a read-only role (planner, reviewer) on a throw-away export of a tree.

Whatever the agent writes into the scratch export is discarded; only its textual answer is used,
and that answer is treated as untrusted input that must pass strict validation.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from multigenz.contracts.agent import CancellationToken
from multigenz.contracts.task import TaskSpec
from multigenz.core.bootstrap import Services
from multigenz.core.budget import Budget
from multigenz.execution.git import Git
from multigenz.execution.manager import make_context, new_attempt_id


class AgentCallFailed(Exception):
    def __init__(self, agent: str, code: str):
        self.agent, self.code = agent, code
        super().__init__(f"{agent}: {code}")


def extract_json_object(text: str) -> dict | None:
    """First JSON object in free-form model output (fenced block, bare object or surrounded by prose)."""
    cands = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)]
    cands.append(text.strip())
    dec = json.JSONDecoder()
    for c in cands:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


CALL_STARTED, CALL_FINISHED = "agent.call.started", "agent.call.finished"


class ReadOnlyRunner:
    def __init__(self, svc: Services, git: Git, work_root: Path, budget: Budget, *, run_id: str,
                 on_call: Callable[[str, str, str, dict], None] | None = None):
        """``on_call(kind, task_id, call_id, payload)`` announces each call (for live views); its logs are in
        ``<work_root>/<call_id>/logs``."""
        self.svc, self.git, self.work_root, self.budget, self.run_id = svc, git, work_root, budget, run_id
        self.on_call = on_call

    async def ask(self, agent: str, mode: str, *, task_id: str, objective: str, extra_prompt: str, treeish: str,
                  feedback: str | None = None, number: int = 1, timeout: int | None = None,
                  cancel: CancellationToken | None = None) -> str:
        adapter = self.svc.adapters[agent]
        pol = self.svc.policy
        if adapter.provider_class == "api":
            self.budget.check(f"{mode} call to {agent}")
        task = TaskSpec(task_id=task_id, title=f"{mode} request", objective=objective,
                        allowed_paths=["readonly-output/"],
                        timeout_seconds=timeout or pol.orchestrator.task_timeout_seconds)
        attempt_id = new_attempt_id()
        base = self.work_root / attempt_id
        ws = base / "tree"
        self.git.export_tree(treeish, ws)
        outcome = "failed"
        if self.on_call:
            self.on_call(CALL_STARTED, task_id, attempt_id, {"agent": agent, "mode": mode, "number": number})
        try:
            ctx = make_context(run_id=self.run_id, task_id=task_id, attempt_id=attempt_id, worktree=ws,
                               backend=self.svc.backend, attempt_dir=base, timeout_seconds=task.timeout_seconds,
                               cancel=cancel or CancellationToken(), credential_refs=pol.agents[agent].credentials.env,
                               mode=mode, feedback=feedback, extra_prompt=extra_prompt, attempt_number=number)
            async for _ in adapter.execute(task, ctx):
                pass
            res = adapter.result(attempt_id)
            self.budget.record(getattr(adapter, "costs", {}).get(attempt_id) if adapter.provider_class == "api" else None)
            if res is None or not res.success:
                outcome = (res.error_code if res else None) or "no_result"
                raise AgentCallFailed(agent, outcome)
            outcome = "finished"
            return adapter.output_text(attempt_id)
        finally:
            shutil.rmtree(ws, ignore_errors=True)
            if self.on_call:
                self.on_call(CALL_FINISHED, task_id, attempt_id, {"agent": agent, "mode": mode, "status": outcome})
