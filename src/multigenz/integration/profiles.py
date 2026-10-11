"""Administrator-defined validation profiles, executed independently of the agent.

Commands are fixed argv lists from the host policy (no shell, nothing from the planner or the
agent). They run in the configured sandbox against an *exported copy* of the exact tree being
validated, with no network, so a test run can neither alter the candidate nor reach the host.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from multigenz.config import Policy
from multigenz.contracts.agent import CancellationToken
from multigenz.contracts.result import CommandReport, TestReport
from multigenz.execution.manager import ExecutionRunner

TAIL = 60


class ProfileRunner:
    def __init__(self, runner: ExecutionRunner, policy: Policy, root: Path):
        self.runner = runner
        self.policy = policy
        self.root = root  # <run_dir>/tests

    async def run(self, profile_name: str, tree: Path, *, label: str,
                  cancel: CancellationToken | None = None) -> TestReport:
        profile = self.policy.validation.profiles[profile_name]
        run_id = f"{label}-{uuid.uuid4().hex[:8]}"
        base = self.root / run_id
        reports: list[CommandReport] = []
        ro = [(p, p) for p in profile.readonly_paths]
        ok = True
        for i, cmd in enumerate(profile.commands, start=1):
            tail: list[str] = []
            result = None
            async for ev in self.runner.run_sandboxed(
                argv=list(cmd.argv), workspace=tree, state_dir=base / f"cmd{i}-sandbox", log_dir=base / "logs",
                timeout=cmd.timeout_seconds, sandbox_id=f"t{uuid.uuid4().hex[:10]}", label=f"{label}:cmd{i}",
                ro_mounts=ro, network="none", cancel=cancel, log_prefix=f"cmd{i}-"):
                if ev.kind in ("stdout", "stderr"):
                    tail.append(ev.line)
                    del tail[:-TAIL]
                elif ev.kind == "exit":
                    result = ev.result
            assert result is not None
            passed = result.exit_code == 0 and not result.timed_out and not result.cancelled and not result.spawn_error
            if result.spawn_error:
                tail.append(f"could not start test command: {result.spawn_error}")
            reports.append(CommandReport(argv=list(cmd.argv), exit_code=result.exit_code, timed_out=result.timed_out,
                                         duration_seconds=round(result.duration, 3), output_tail="\n".join(tail),
                                         log_path=str(base / "logs" / f"cmd{i}-stdout.log")))
            if not passed:
                ok = False
                break  # fail fast: later commands cannot rescue a failed one
        return TestReport(profile=profile_name, passed=ok, commands=reports)

    async def run_on_commit(self, git, profile_name: str, treeish: str, *, label: str,
                            cancel: CancellationToken | None = None) -> TestReport:
        tmp = self.root / f"export-{uuid.uuid4().hex[:8]}"
        try:
            git.export_tree(treeish, tmp)
            return await self.run(profile_name, tmp, label=label, cancel=cancel)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
