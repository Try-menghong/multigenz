import asyncio
import os
import sys
import time

import pytest

from multigenz.contracts.agent import CancellationToken
from multigenz.core.redaction import Redactor
from multigenz.execution.subprocess import (CommandSpec, ProcessManager, build_env, pgid_alive)


def spec(tmp_path, argv, **kw):
    kw.setdefault("timeout_seconds", 20)
    kw.setdefault("env", build_env())
    return CommandSpec(argv=argv, cwd=tmp_path, **kw)


async def test_streams_stdout_stderr_exit(tmp_path):
    pm = ProcessManager()
    kinds = []
    async for ev in pm.stream(spec(tmp_path, ["/bin/sh", "-c", "echo out; echo err >&2; exit 3"])):
        kinds.append((ev.kind, ev.line))
        if ev.kind == "exit":
            assert ev.result.exit_code == 3 and not ev.result.timed_out
    assert ("stdout", "out") in kinds and ("stderr", "err") in kinds and kinds[0][0] == "started"


async def test_cwd_and_env_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_SECRET_TOKEN", "should-not-leak-1234")
    res, tail = await ProcessManager().run(spec(tmp_path, ["/bin/sh", "-c", "pwd; env"]))
    text = "\n".join(tail)
    assert str(tmp_path) in text and "HOST_SECRET_TOKEN" not in text and "should-not-leak" not in text


async def test_spawn_error_reported(tmp_path):
    res, _ = await ProcessManager().run(spec(tmp_path, ["/nonexistent/binary"]))
    assert res.spawn_error and "FileNotFoundError" in res.spawn_error


async def test_timeout_kills_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = f"sleep 300 & echo $! > {pidfile}; wait"
    t0 = time.monotonic()
    res, _ = await ProcessManager().run(spec(tmp_path, ["/bin/sh", "-c", script], timeout_seconds=1.0,
                                             kill_grace_seconds=1.0))
    assert res.timed_out and time.monotonic() - t0 < 10
    child = int(pidfile.read_text())
    await asyncio.sleep(0.2)
    assert not os.path.exists(f"/proc/{child}") or open(f"/proc/{child}/stat").read().split()[2] == "Z"
    assert not pgid_alive(res.pgid)


async def test_sigterm_ignoring_process_is_sigkilled(tmp_path):
    script = "trap '' TERM; while true; do sleep 1; done"
    res, _ = await ProcessManager().run(spec(tmp_path, ["/bin/sh", "-c", script], timeout_seconds=0.5,
                                             kill_grace_seconds=0.5))
    assert res.timed_out and res.term_signal == 9


async def test_cancellation_token(tmp_path):
    tok = CancellationToken()
    asyncio.get_running_loop().call_later(0.4, tok.cancel, "user")
    res, _ = await ProcessManager().run(spec(tmp_path, ["/bin/sh", "-c", "sleep 100"]), cancel=tok)
    assert res.cancelled and res.cancel_reason == "user" and not res.timed_out


async def test_background_survivor_after_leader_exit_is_reaped(tmp_path):
    pidfile = tmp_path / "bg.pid"
    script = f"sleep 300 & echo $! > {pidfile}; echo done"
    res, tail = await ProcessManager().run(spec(tmp_path, ["/bin/sh", "-c", script]))
    assert res.exit_code == 0 and "done" in tail
    await asyncio.sleep(0.3)
    pid = int(pidfile.read_text())
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"


async def test_output_is_redacted_before_logging(tmp_path):
    secret = "topsecretvalue987"
    pm = ProcessManager(Redactor([secret]))
    log = tmp_path / "o.log"
    res, tail = await pm.run(spec(tmp_path, ["/bin/sh", "-c", f"echo key={secret}; echo ok"], stdout_log=log))
    assert secret not in "\n".join(tail) and secret not in log.read_text() and "ok" in log.read_text()


async def test_log_cap_truncates(tmp_path):
    log = tmp_path / "o.log"
    res, _ = await ProcessManager().run(spec(tmp_path, [sys.executable, "-c", "print('x'*100)\n" * 0 +
                                                       "[print('y'*100) for _ in range(500)]"],
                                             stdout_log=log, max_log_bytes=2000))
    assert res.truncated and log.stat().st_size < 2200


async def test_long_line_without_newline_does_not_crash(tmp_path):
    res, tail = await ProcessManager().run(spec(tmp_path, [sys.executable, "-c", "print('z'*100000)"]))
    assert res.exit_code == 0 and sum(len(t) for t in tail) >= 100000
