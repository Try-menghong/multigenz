"""Subprocess manager: cwd, explicit env, process-group termination, timeouts, streaming.

* Every child starts in its own session/process group; timeouts and cancellation terminate
  the *whole group* (SIGTERM, grace period, SIGKILL) and stragglers are reaped after exit.
* Output is streamed line by line, redacted before it reaches disk or the caller, and capped.
* The child environment is built explicitly; the host environment is never inherited.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from multigenz.contracts.agent import CancellationToken
from multigenz.core.redaction import Redactor

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MAX_LINE = 16 * 1024


def build_env(extra: Mapping[str, str] | None = None, *, home: str | None = None,
              path: str = SAFE_PATH) -> dict[str, str]:
    """Minimal explicit environment. Host variables are never inherited."""
    env = {"PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    if home:
        env["HOME"] = home
    env.update(extra or {})
    return env


@dataclass(frozen=True)
class CommandSpec:
    argv: Sequence[str]
    cwd: Path
    env: Mapping[str, str]
    timeout_seconds: float
    stdin_data: bytes | None = None
    max_log_bytes: int = 4 * 1024 * 1024
    stdout_log: Path | None = None
    stderr_log: Path | None = None
    kill_grace_seconds: float = 5.0
    label: str = ""
    on_terminate: Callable[[], None] | None = None  # extra teardown (e.g. kill container)


@dataclass
class ProcessStart:
    pid: int
    pgid: int
    start_ticks: str | None


@dataclass
class ProcessResult:
    exit_code: int | None = None
    term_signal: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    spawn_error: str | None = None
    duration: float = 0.0
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    pid: int | None = None
    pgid: int | None = None
    cancel_reason: str | None = None


@dataclass
class ProcessEvent:
    kind: str  # started | stdout | stderr | exit
    line: str = ""
    start: ProcessStart | None = None
    result: ProcessResult | None = None


def proc_start_ticks(pid: int) -> str | None:
    """Kernel start time of a pid (field 22 of /proc/<pid>/stat); guards against pid reuse."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = data[data.rfind(")") + 2 :].split()
    return rest[19] if len(rest) > 19 else None


def pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def terminate_group(pgid: int, grace: float) -> None:
    """SIGTERM the group, wait up to ``grace`` seconds, then SIGKILL survivors."""
    kill_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pgid_alive(pgid):
            return
        await asyncio.sleep(0.05)
    kill_group(pgid, signal.SIGKILL)
    for _ in range(100):
        if not pgid_alive(pgid):
            return
        await asyncio.sleep(0.05)


class _LogSink:
    def __init__(self, path: Path | None, cap: int):
        self.path = path
        self.cap = cap
        self.written = 0
        self.truncated = False
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "ab")  # noqa: SIM115 - closed in close()

    def write(self, text: str) -> None:
        if self._fh is None:
            return
        data = (text + "\n").encode("utf-8", "replace")
        if self.written + len(data) > self.cap:
            if not self.truncated:
                self.truncated = True
                self._fh.write(b"[multigenz: log truncated]\n")
                self._fh.flush()
            return
        self.written += len(data)
        self._fh.write(data)
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class ProcessManager:
    def __init__(self, redactor: Redactor | None = None):
        self.redactor = redactor or Redactor()

    async def stream(
        self,
        spec: CommandSpec,
        *,
        cancel: CancellationToken | None = None,
    ) -> AsyncIterator[ProcessEvent]:
        res = ProcessResult()
        started = time.monotonic()
        out_sink = _LogSink(spec.stdout_log, spec.max_log_bytes)
        err_sink = _LogSink(spec.stderr_log, spec.max_log_bytes)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=str(spec.cwd),
                env=dict(spec.env),
                stdin=asyncio.subprocess.PIPE if spec.stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=1 << 20,
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError, OSError) as exc:
            res.spawn_error = f"{type(exc).__name__}: {exc}"
            res.duration = time.monotonic() - started
            out_sink.close()
            err_sink.close()
            yield ProcessEvent("exit", result=res)
            return

        pid = proc.pid
        pgid = pid  # start_new_session => pgid == pid
        res.pid, res.pgid = pid, pgid
        start = ProcessStart(pid=pid, pgid=pgid, start_ticks=proc_start_ticks(pid))
        yield ProcessEvent("started", start=start)

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def pump(reader: asyncio.StreamReader, kind: str, sink: _LogSink) -> None:
            red = self.redactor.stream()
            buf = b""
            counted = 0
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    counted += len(chunk)
                    buf += chunk
                    while True:
                        nl = buf.find(b"\n")
                        if nl == -1 or nl > MAX_LINE:
                            if len(buf) >= MAX_LINE:
                                raw, buf = buf[:MAX_LINE], buf[MAX_LINE:]  # forced split: keep every byte
                            else:
                                break
                        else:
                            raw, buf = buf[:nl], buf[nl + 1 :]
                        text = red.line(raw.decode("utf-8", "replace").rstrip("\r"))
                        sink.write(text)
                        await queue.put((kind, text))
                if buf:
                    text = red.line(buf.decode("utf-8", "replace").rstrip("\r"))
                    sink.write(text)
                    await queue.put((kind, text))
            finally:
                if kind == "stdout":
                    res.stdout_bytes = counted
                else:
                    res.stderr_bytes = counted

        if spec.stdin_data is not None and proc.stdin is not None:
            try:
                proc.stdin.write(spec.stdin_data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

        readers = [
            asyncio.create_task(pump(proc.stdout, "stdout", out_sink)),
            asyncio.create_task(pump(proc.stderr, "stderr", err_sink)),
        ]

        async def watchdog() -> None:
            waiters = {asyncio.create_task(asyncio.sleep(spec.timeout_seconds), name="timeout")}
            if cancel is not None:
                waiters.add(asyncio.create_task(cancel.wait(), name="cancel"))
            waiters.add(asyncio.create_task(proc.wait(), name="exit"))
            done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            names = {d.get_name() for d in done}
            if "exit" in names:
                return
            if "cancel" in names:
                res.cancelled = True
                res.cancel_reason = cancel.reason if cancel else None
            else:
                res.timed_out = True
            if spec.on_terminate is not None:
                try:
                    spec.on_terminate()
                except Exception:  # noqa: BLE001
                    pass
            await terminate_group(pgid, spec.kill_grace_seconds)

        wd = asyncio.create_task(watchdog())

        async def finisher() -> None:
            await asyncio.gather(*readers, return_exceptions=True)
            await queue.put(None)

        fin: asyncio.Task | None = None
        try:
            fin = asyncio.create_task(finisher())
            # Readers end when every holder of the pipes is gone. If the leader exits but a
            # descendant keeps the pipes open, the post-exit sweep below kills the group.
            sweeper = asyncio.create_task(self._sweep_after_exit(proc, pgid, readers))
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield ProcessEvent(item[0], line=item[1])
            await proc.wait()
            await wd
            sweeper.cancel()
            kill_group(pgid, signal.SIGKILL)  # reap stragglers left in the group
        finally:
            for t in (*readers, wd, *([fin] if fin else [])):
                if not t.done():
                    t.cancel()
            if proc.returncode is None:
                kill_group(pgid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except Exception:  # noqa: BLE001
                    pass
            out_sink.close()
            err_sink.close()

        rc = proc.returncode
        if rc is not None and rc < 0:
            res.term_signal = -rc
            res.exit_code = None
        else:
            res.exit_code = rc
        res.duration = time.monotonic() - started
        res.truncated = out_sink.truncated or err_sink.truncated
        yield ProcessEvent("exit", result=res)

    async def _sweep_after_exit(self, proc: asyncio.subprocess.Process, pgid: int,
                                readers: list[asyncio.Task]) -> None:
        await proc.wait()
        # Give pipes a moment to drain, then kill any descendants still holding them open.
        await asyncio.wait(readers, timeout=1.0)
        if not all(r.done() for r in readers):
            kill_group(pgid, signal.SIGKILL)
            await asyncio.wait(readers, timeout=2.0)

    async def run(self, spec: CommandSpec, *, cancel: CancellationToken | None = None,
                  keep_lines: int = 200) -> tuple[ProcessResult, list[str]]:
        """Run to completion; return the result and the last ``keep_lines`` output lines."""
        tail: list[str] = []
        result = ProcessResult()
        async for ev in self.stream(spec, cancel=cancel):
            if ev.kind in ("stdout", "stderr"):
                tail.append(ev.line)
                if len(tail) > keep_lines:
                    del tail[: len(tail) - keep_lines]
            elif ev.kind == "exit" and ev.result is not None:
                result = ev.result
        return result, tail
