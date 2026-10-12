"""Generic CLI agent adapter with capability detection.

No CLI flag is assumed. An invocation is enabled only after the binary's own ``--help`` output
(or the administrator's ``help_args``) contains every token in ``requires_help_tokens``.
Unattended invocations additionally refuse to run outside a sandbox.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from collections.abc import AsyncIterator

from multigenz.adapters.base import (
    AdapterCommand,
    CommandRunner,
    classify_failure,
    render_prompt,
    task_json,
    utcnow,
)
from multigenz.config import AgentCfg, InvocationCfg
from multigenz.contracts.agent import (
    AdapterProbe,
    AgentAdapter,
    AgentEvent,
    AgentResult,
    ExecutionContext,
)
from multigenz.contracts.task import TaskSpec
from multigenz.errors import AdapterUnavailable
from multigenz.execution.subprocess import SAFE_PATH


async def _capture(argv: list[str], timeout: float = 15.0) -> tuple[int | None, str]:
    """Run a trusted, admin-configured binary for capability probing (no secrets in env)."""
    env = {"PATH": os.environ.get("PATH", SAFE_PATH), "HOME": "/nonexistent", "LANG": "C.UTF-8",
           "NO_COLOR": "1", "CI": "1", "TERM": "dumb"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=True)
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
        await proc.wait()
        return None, "timeout"
    return proc.returncode, out.decode("utf-8", "replace")


MAX_ARG_PROMPT = 120_000  # Linux refuses a single argv string over 128 KiB (MAX_ARG_STRLEN)


def has_flag(help_text: str, flag: str) -> bool:
    """``flag`` appears in the help as a whole option (``--model``, not ``--model-provider``)."""
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


class CliAdapter(AgentAdapter):
    provider_class = "cli"
    BUILTIN_INVOCATIONS: list[InvocationCfg] = []
    NO_INVOCATION_HINT: str | None = None
    # How this CLI is told which model to use (``agents.<name>.model``). Used only after the flag is found in the
    # help text of every verified invocation; admin-defined invocations may place ``{model}`` themselves instead.
    MODEL_FLAG: str | None = None

    def __init__(self, name: str, cfg: AgentCfg, *, runner: CommandRunner | None = None):
        self.name = name
        self.cfg = cfg
        self.capabilities = frozenset(cfg.capabilities)
        self.runner = runner
        self._probe: AdapterProbe | None = None
        self._verified: list[InvocationCfg] = []
        self._binary: str | None = None
        self._pass_model = False
        self.model_problem: str | None = None  # set by probe() when a model is configured but cannot be passed
        self._results: dict[str, AgentResult] = {}
        self._outputs: dict[str, list[str]] = {}
        self._cancels: dict[str, asyncio.Event] = {}

    def bind_runner(self, runner: CommandRunner) -> None:
        self.runner = runner

    def candidates(self) -> list[InvocationCfg]:
        return list(self.cfg.invocations) if self.cfg.invocations else list(self.BUILTIN_INVOCATIONS)

    # -- capability detection ------------------------------------------------------
    async def probe(self) -> AdapterProbe:
        if self._probe is not None:
            return self._probe
        assert self.cfg.command
        binary = self.cfg.command if os.path.isabs(self.cfg.command) else shutil.which(self.cfg.command)
        if not self.cfg.enabled:
            self._probe = AdapterProbe(name=self.name, enabled=False, reason="disabled in policy")
            return self._probe
        if not binary or not os.access(binary, os.X_OK):
            self._probe = AdapterProbe(name=self.name, enabled=False,
                                       reason=f"binary {self.cfg.command!r} not found or not executable")
            return self._probe
        self._binary = binary
        _, ver = await _capture([binary, "--version"])
        version = ver.strip().splitlines()[0][:120] if ver.strip() else None
        cands = self.candidates()
        if not cands:
            self._probe = AdapterProbe(
                name=self.name, enabled=False, binary=binary, version=version,
                reason=self.NO_INVOCATION_HINT or ("no non-interactive invocation configured; define "
                                                   "agents.<name>.invocations in the policy (flags are never assumed)"))
            return self._probe
        help_cache: dict[tuple[str, ...], str] = {}
        verified: list[InvocationCfg] = []
        notes: list[str] = []
        for inv in cands:
            key = tuple(inv.help_args)
            if key not in help_cache:
                rc, text = await _capture([binary, *inv.help_args])
                help_cache[key] = text if rc is not None else ""
            text = help_cache[key]
            missing = [t for t in inv.requires_help_tokens if t not in text]
            if not inv.requires_help_tokens:
                notes.append(f"invocation {inv.name!r} has no help tokens to verify; skipped")
            elif missing:
                notes.append(f"invocation {inv.name!r} unverified: help lacks {missing}")
            else:
                verified.append(inv)
        self._verified = verified
        modes = sorted({m for inv in verified for m in inv.modes})
        model_problem = None
        if verified and self.cfg.model and self.cfg.provider is None:
            placed = all("{model}" in " ".join(inv.argv) for inv in verified)
            flag_ok = bool(self.MODEL_FLAG) and all(has_flag(help_cache[tuple(inv.help_args)], self.MODEL_FLAG)
                                                    for inv in verified)
            if placed or flag_ok:
                self._pass_model = True
            else:
                model_problem = (f"model {self.cfg.model!r} requested, but this CLI's help shows no way to choose a "
                                 f"model ({self.MODEL_FLAG or 'no model flag known'}); remove the model or add "
                                 "{model} to an invocation")
        self.model_problem = model_problem
        if model_problem:
            self._probe = AdapterProbe(name=self.name, enabled=False, binary=binary, version=version,
                                       reason=model_problem, warnings=notes)
        elif not verified:
            self._probe = AdapterProbe(name=self.name, enabled=False, binary=binary, version=version,
                                       reason="no invocation verified against the binary's help output",
                                       warnings=notes)
        else:
            self._probe = AdapterProbe(name=self.name, enabled=True, binary=binary, version=version,
                                       invocation=", ".join(i.name for i in verified),
                                       capabilities=sorted(self.capabilities), modes=modes, warnings=notes)
        return self._probe

    async def healthcheck(self) -> bool:
        return (await self.probe()).enabled

    def invocation_for(self, mode: str) -> InvocationCfg:
        for inv in self._verified:
            if mode in inv.modes:
                return inv
        raise AdapterUnavailable(f"{self.name}: no verified invocation for mode {mode!r}")

    # -- execution -----------------------------------------------------------------
    @property
    def passes_model(self) -> bool:
        """After probe(): the configured model is given to the CLI (flag verified, or ``{model}`` in the argv)."""
        return self._pass_model

    def _render_argv(self, inv: InvocationCfg, task: TaskSpec, ctx: ExecutionContext, prompt: str = "") -> list[str]:
        assert self.runner is not None and self._binary is not None
        subs = {
            "command": self._binary,
            "prompt": prompt,  # for CLIs that only take the prompt as an argument (e.g. hermes -z)
            "prompt_file": f"{self.runner.inbox_path(ctx)}/prompt.txt",
            "task_file": f"{self.runner.inbox_path(ctx)}/task.json",
            "task_id": task.task_id,
            "workspace": self.runner.workspace_path(ctx),
        }
        if self._pass_model and self.cfg.model:
            subs["model"] = self.cfg.model
        out = []
        for a in inv.argv:
            for k, v in subs.items():
                a = a.replace("{" + k + "}", v)
            out.append(a)
        if self._pass_model and self.cfg.model and not any("{model}" in a for a in inv.argv) and self.MODEL_FLAG:
            out += [self.MODEL_FLAG, self.cfg.model]
        return out

    async def execute(self, task: TaskSpec, context: ExecutionContext) -> AsyncIterator[AgentEvent]:
        if self.runner is None:
            raise AdapterUnavailable(f"{self.name}: no execution runner bound")
        probe = await self.probe()
        seq = 0
        attempt = context.attempt_id

        def ev(kind: str, message: str) -> AgentEvent:
            nonlocal seq
            seq += 1
            return AgentEvent(run_id=context.run_id, task_id=task.task_id, attempt_id=attempt, sequence=seq,
                              kind=kind, message=message, timestamp=utcnow())

        if not probe.enabled:
            self._results[attempt] = AgentResult(task_id=task.task_id, attempt_id=attempt, exit_code=None,
                                                 commit_sha=None, artifacts=[], success=False,
                                                 error_code="binary_missing" if "not found" in probe.reason
                                                 else "adapter_disabled")
            yield ev("error", probe.reason)
            return
        try:
            inv = self.invocation_for(context.mode)
        except AdapterUnavailable as exc:
            self._results[attempt] = AgentResult(task_id=task.task_id, attempt_id=attempt, exit_code=None,
                                                 commit_sha=None, artifacts=[], success=False,
                                                 error_code="adapter_disabled")
            yield ev("error", str(exc))
            return
        if inv.unattended and not self.runner.sandboxed:
            self._results[attempt] = AgentResult(task_id=task.task_id, attempt_id=attempt, exit_code=None,
                                                 commit_sha=None, artifacts=[], success=False,
                                                 error_code="sandbox_required")
            yield ev("error", "unattended invocation refused: no sandbox")
            return
        assert context.inbox is not None
        prompt = render_prompt(task, context)
        context.inbox.mkdir(parents=True, exist_ok=True)
        (context.inbox / "prompt.txt").write_text(prompt, encoding="utf-8")
        (context.inbox / "task.json").write_text(task_json(task, context), encoding="utf-8")
        if any("{prompt}" in a for a in inv.argv) and len(prompt.encode()) > MAX_ARG_PROMPT:
            self._results[attempt] = AgentResult(task_id=task.task_id, attempt_id=attempt, exit_code=None,
                                                 commit_sha=None, artifacts=[], success=False,
                                                 error_code="spawn_failed")
            yield ev("error", f"prompt is {len(prompt.encode())} bytes; {inv.name} passes it as one argument "
                              f"(limit {MAX_ARG_PROMPT})")
            return
        argv = self._render_argv(inv, task, context, prompt)
        cmd = AdapterCommand(argv=argv, agent=self.name, stdin_data=prompt.encode() if inv.stdin == "prompt" else None,
                             unattended=inv.unattended, label=f"{self.name}:{inv.name}")
        yield ev("started", f"{self.name} invocation={inv.name} mode={context.mode}")
        tail: list[str] = []
        lines: list[str] = []
        self._outputs[attempt] = lines
        exit_code: int | None = None
        code: str | None = None
        async for pe in self.runner.run_command(cmd, context):
            if pe.kind in ("stdout", "stderr"):
                tail.append(pe.line)
                if pe.kind == "stdout":
                    lines.append(pe.line)
                yield ev(pe.kind, pe.line)
            elif pe.kind == "started" and pe.start is not None:
                yield ev("process", f"pid={pe.start.pid}")
            elif pe.kind == "exit" and pe.result is not None:
                r = pe.result
                exit_code = r.exit_code
                code = classify_failure(
                    exit_code=r.exit_code, timed_out=r.timed_out, cancelled=r.cancelled,
                    spawn_error=r.spawn_error, term_signal=r.term_signal, tail=tail,
                    rate_markers=self.cfg.rate_limit_markers, auth_markers=self.cfg.auth_failure_markers)
                yield ev("exit", f"exit_code={r.exit_code} signal={r.term_signal} timed_out={r.timed_out} "
                                 f"cancelled={r.cancelled} duration={r.duration:.2f}s")
        self._results[attempt] = AgentResult(task_id=task.task_id, attempt_id=attempt, exit_code=exit_code,
                                             commit_sha=None, artifacts=[], success=code is None,
                                             error_code=code)

    async def cancel(self, attempt_id: str) -> None:
        # Cancellation flows through the CancellationToken in the ExecutionContext; the process
        # manager terminates the whole process group. Nothing else to do at adapter level.
        return None

    def result(self, attempt_id: str) -> AgentResult | None:
        return self._results.get(attempt_id)

    def output_text(self, attempt_id: str) -> str:
        return "\n".join(self._outputs.get(attempt_id, []))
