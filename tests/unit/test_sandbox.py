import asyncio
import os
from pathlib import Path

import pytest

from multigenz.config import parse_policy
from multigenz.errors import IsolationUnavailableError
from multigenz.execution.resources import ResourceLimits
from multigenz.execution.sandbox import (ContainerBackend, NoSandbox, SandboxRequest, UsernsBackend,
                                         select_backend)
from multigenz.execution.subprocess import CommandSpec, ProcessManager

pytestmark = pytest.mark.needs_userns


async def sh(tmp_path, script, *, network="none", ro=None, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    b = UsernsBackend(extra_ro=ro or [])
    req = SandboxRequest(argv=["/bin/sh", "-c", script], env=kw.get("env", {}), workspace=ws,
                         state_dir=tmp_path / "st", sandbox_id="t", network=network,
                         limits=kw.get("limits", ResourceLimits()))
    w = b.wrap(req)
    res, tail = await ProcessManager().run(CommandSpec(argv=w.argv, cwd=w.cwd, env=w.env, timeout_seconds=30))
    return res, "\n".join(tail), ws


async def test_workspace_writable_system_readonly(tmp_path):
    res, out, ws = await sh(tmp_path, "touch /workspace/a && echo W-OK; touch /usr/x 2>&1; touch /etc/x 2>&1; "
                                      "touch /newtop 2>&1; echo hi > /tmp/t && echo TMP-OK")
    assert "W-OK" in out and "TMP-OK" in out and (ws / "a").exists()
    assert out.count("Read-only file system") == 3


async def test_host_env_and_home_invisible(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hostsecretvalue99")
    fake_home = tmp_path / "home" / ".ssh"
    fake_home.mkdir(parents=True)
    (fake_home / "id_rsa").write_text("PRIVATE")
    res, out, _ = await sh(tmp_path, f"env; cat {fake_home}/id_rsa 2>&1; ls /root /home/user 2>&1; ls /")
    assert "hostsecretvalue99" not in out and "AWS_SECRET" not in out and "PRIVATE" not in out
    assert "No such file" in out


async def test_capabilities_dropped_and_no_new_privs(tmp_path):
    res, out, _ = await sh(tmp_path, "grep -E 'CapEff|CapBnd|CapPrm|CapInh|NoNewPrivs' /proc/self/status")
    for line in out.splitlines():
        k, v = line.split(":")
        assert int(v.strip(), 16 if k.startswith("Cap") else 10) == (1 if k == "NoNewPrivs" else 0), line


async def test_cannot_mount_or_escape(tmp_path):
    res, out, _ = await sh(tmp_path, "mount -t tmpfs x /tmp 2>&1; echo rc=$?; ls /proc/1/root 2>&1 | head -2; "
                                     "chroot /tmp /bin/true 2>&1; echo c=$?")
    assert "rc=0" not in out.splitlines()[0:2] or "permitted" in out.lower() or "denied" in out.lower()


async def test_pid_namespace_isolated(tmp_path):
    res, out, _ = await sh(tmp_path, "ls /proc | grep -c '^[0-9]*$'")
    assert int(out.strip().splitlines()[-1]) <= 4


async def test_network_none_has_no_route(tmp_path):
    py = "import socket;s=socket.socket();s.settimeout(2)\ntry: s.connect(('1.1.1.1',53));print('NET-OK')\nexcept OSError as e: print('NET-FAIL')"
    res, out, _ = await sh(tmp_path, f"/usr/bin/python3 -c \"{py}\"")
    assert "NET-FAIL" in out


async def test_rlimit_fsize_enforced(tmp_path):
    res, out, ws = await sh(tmp_path, "head -c 3000000 /dev/zero > /workspace/big; ls -l /workspace/big | awk '{print $5}'",
                            limits=ResourceLimits(max_file_mb=1))
    assert (ws / "big").stat().st_size <= 1024 * 1024


async def test_sandbox_failure_fails_closed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    b = UsernsBackend()
    bad = SandboxRequest(argv=["/bin/true"], env={}, workspace=tmp_path / "does-not-exist", state_dir=tmp_path / "st",
                         sandbox_id="x")
    w = b.wrap(bad)
    res, tail = await ProcessManager().run(CommandSpec(argv=w.argv, cwd=w.cwd, env=w.env, timeout_seconds=20))
    assert res.exit_code == 126 and "multigenz-sandbox" in "\n".join(tail)


def test_restricted_network_unsupported_by_userns(tmp_path):
    with pytest.raises(IsolationUnavailableError):
        UsernsBackend().wrap(SandboxRequest(argv=["/bin/true"], env={}, workspace=tmp_path, state_dir=tmp_path / "s",
                                            sandbox_id="x", network="restricted"))


async def test_ro_extra_path_visible_readonly(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "f").write_text("data")
    res, out, _ = await sh(tmp_path, f"cat {extra}/f; touch {extra}/g 2>&1", ro=[str(extra)])
    assert "data" in out and "Read-only" in out


# --- backends that do not need a user namespace -----------------------------------------
async def test_select_backend_fails_closed_without_container_runtime(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    pol = parse_policy({"execution": {"isolation": "rootless-container", "container_image": "img"}})
    with pytest.raises(IsolationUnavailableError, match="never falls back"):
        await select_backend(pol)


async def test_select_backend_none_requires_explicit_optin():
    pol = parse_policy({"execution": {"isolation": "none", "allow_unsandboxed": True}})
    b = await select_backend(pol)
    assert isinstance(b, NoSandbox) and not b.sandboxed


def test_container_argv_is_locked_down(tmp_path):
    b = ContainerBackend("podman", "registry/img:1", "egress-net")
    req = SandboxRequest(argv=["agent", "--x"], env={"K": "V"}, workspace=tmp_path / "ws", state_dir=tmp_path / "st",
                         sandbox_id="abc", inbox=tmp_path / "in", network="restricted",
                         limits=ResourceLimits(memory_mb=1024, pids_limit=100))
    argv = b.build_argv(req, runtime="podman", env_file=tmp_path / "e")
    j = " ".join(argv)
    for needle in ("--cap-drop ALL", "no-new-privileges", "--read-only", "--network egress-net", "--pids-limit 100",
                   "--memory 1024m", f"{tmp_path / 'ws'}:/workspace:rw", ":/mg/in:ro", "--env-file"):
        assert needle in j, needle
    assert "K=V" not in j and "--privileged" not in j and "docker.sock" not in j and argv[-2:] == ["agent", "--x"]


def test_container_restricted_requires_admin_network(tmp_path):
    b = ContainerBackend("podman", "img", None)
    with pytest.raises(IsolationUnavailableError):
        b.build_argv(SandboxRequest(argv=["a"], env={}, workspace=tmp_path, state_dir=tmp_path, sandbox_id="x",
                                    network="restricted"), runtime="podman", env_file=None)


async def test_exec_failure_inside_working_sandbox_is_not_a_sandbox_failure(tmp_path):
    from multigenz.adapters.base import classify_failure
    for exe, code, want in (("/nonexistent/agent", 127, "binary_missing"), ("/etc/passwd", 125, "spawn_failed")):
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        req = SandboxRequest(argv=[exe], env={}, workspace=ws, state_dir=tmp_path / "st", sandbox_id="t")
        w = UsernsBackend().wrap(req)
        res, tail = await ProcessManager().run(CommandSpec(argv=w.argv, cwd=w.cwd, env=w.env, timeout_seconds=30))
        assert res.exit_code == code
        assert classify_failure(exit_code=res.exit_code, timed_out=False, cancelled=False, spawn_error=None,
                                term_signal=None, tail=tail) == want


async def test_proc_sys_is_read_only_and_kernel_interfaces_masked(tmp_path):
    res, out, _ = await sh(tmp_path, "echo x > /proc/sys/kernel/hostname 2>&1; echo x > /proc/sys/vm/swappiness 2>&1; "
                                     "head -c 10 /proc/kcore 2>/dev/null | wc -c; cat /proc/keys 2>/dev/null | wc -c")
    lines = out.splitlines()
    assert sum("Read-only file system" in l for l in lines) == 2
    assert lines[-2:] == ["0", "0"]  # masked with /dev/null


async def test_symlinked_prefix_and_exe_symlink_chain_are_visible(tmp_path):
    """uv-style layout: venv/bin/python -> <versioned>/bin/tool, while the prefix is given by an unversioned symlink dir."""
    versioned = tmp_path / "py" / "cpython-1.2.3"
    (versioned / "bin").mkdir(parents=True)
    tool = versioned / "bin" / "tool3"
    tool.write_text("#!/bin/sh\necho TOOL-RAN\n")
    tool.chmod(0o755)
    (tmp_path / "py" / "cpython-1.2").symlink_to(versioned)           # unversioned prefix = symlinked directory
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "tool").symlink_to(versioned / "bin" / "tool3")   # exe symlink points at the versioned real path
    ws = tmp_path / "ws"
    ws.mkdir()
    b = UsernsBackend()
    req = SandboxRequest(argv=[str(venv / "bin" / "tool")], env={}, workspace=ws, state_dir=tmp_path / "st",
                         sandbox_id="t", ro_mounts=[(str(venv), str(venv)), (str(tmp_path / "py" / "cpython-1.2"),
                                                                              str(tmp_path / "py" / "cpython-1.2"))])
    w = b.wrap(req)
    res, tail = await ProcessManager().run(CommandSpec(argv=w.argv, cwd=w.cwd, env=w.env, timeout_seconds=30))
    assert res.exit_code == 0 and "TOOL-RAN" in tail, tail


async def test_mounted_venv_gets_the_interpreter_prefix_named_in_pyvenv_cfg(tmp_path):
    real = tmp_path / "py" / "cpython-9.9.9"
    (real / "bin").mkdir(parents=True)
    (real / "lib").mkdir()
    (real / "lib" / "marker").write_text("STDLIB-HERE")
    (real / "bin" / "tool").write_text("#!/bin/sh\ncat \"$(dirname $0)/../lib/marker\"\n")
    (real / "bin" / "tool").chmod(0o755)
    (tmp_path / "py" / "cpython-9.9").symlink_to(real)  # the name pyvenv.cfg uses; unrelated to the path we mount
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'py' / 'cpython-9.9' / 'bin'}\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    req = SandboxRequest(argv=["/bin/sh", "-c", f"cat {tmp_path / 'py' / 'cpython-9.9' / 'lib' / 'marker'}"], env={}, workspace=ws,
                         state_dir=tmp_path / "st", sandbox_id="t", ro_mounts=[(str(venv), str(venv))])
    w = UsernsBackend().wrap(req)
    res, tail = await ProcessManager().run(CommandSpec(argv=w.argv, cwd=w.cwd, env=w.env, timeout_seconds=30))
    assert res.exit_code == 0 and "STDLIB-HERE" in tail, tail


@pytest.mark.skipif(not os.path.exists("/etc/resolv.conf"), reason="host has no /etc/resolv.conf")
async def test_host_network_gets_host_resolver_config_but_isolated_network_does_not(tmp_path):
    host = Path("/etc/resolv.conf").read_text()
    for d in ("h", "n", "w"):
        (tmp_path / d).mkdir()
    res, out_host, _ = await sh(tmp_path / "h", "cat /etc/resolv.conf", network="host")
    res, out_none, _ = await sh(tmp_path / "n", "cat /etc/resolv.conf", network="none")
    assert out_host.strip() == host.strip() and out_none.strip() == ""
    res, out, _ = await sh(tmp_path / "w", "echo x >> /etc/resolv.conf 2>&1", network="host")
    assert "Read-only" in out  # still read-only inside the sandbox


async def test_script_cli_finds_its_runtime_next_to_itself(tmp_path):
    """`#!/usr/bin/env mini-node` with the runtime in the same non-/usr directory (nvm / /opt/<tool>/bin layout)."""
    bindir = tmp_path / "opt" / "toolchain" / "bin"
    bindir.mkdir(parents=True)
    rt = bindir / "mini-node"
    rt.write_text("#!/bin/sh\necho RUNTIME-OK \"$@\"\n")
    rt.chmod(0o755)
    cli = bindir / "agentcli"
    cli.write_text("#!/usr/bin/env mini-node\n")
    cli.chmod(0o755)
    ws = tmp_path / "ws"
    ws.mkdir()
    req = SandboxRequest(argv=[str(cli), "arg1"], env={}, workspace=ws, state_dir=tmp_path / "st", sandbox_id="t")
    w = UsernsBackend().wrap(req)
    res, tail = await ProcessManager().run(CommandSpec(argv=w.argv, cwd=w.cwd, env=w.env, timeout_seconds=30))
    assert res.exit_code == 0 and any("RUNTIME-OK" in t for t in tail), tail
