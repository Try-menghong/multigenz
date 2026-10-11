"""Sandbox backends. The orchestrator fails closed when required isolation is unavailable.

* ``userns``              - built-in unprivileged Linux namespaces (user, mount, pid, ipc, uts, net)
                            + tmpfs root, read-only system binds, one writable workspace,
                            capabilities dropped, no_new_privs, rlimits (see nsboot.py).
* ``rootless-container``  - podman/docker rootless run with dropped capabilities, read-only
                            rootfs, only the workspace writable, pids/memory/cpu ceilings.
* ``none``                - explicit opt-in only (``allow_unsandboxed``); never a fallback.

Worktrees are not OS sandboxes; they are only a place for the sandbox to write.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from multigenz.config import Policy
from multigenz.errors import IsolationUnavailableError
from multigenz.execution.resources import ResourceLimits

SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
WORKSPACE = "/workspace"
INBOX = "/mg/in"
SANDBOX_HOME = "/home/agent"
LAUNCHER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ETC_RO = ["/etc/ssl", "/etc/alternatives", "/etc/ld.so.cache", "/etc/ld.so.conf",
          "/etc/ld.so.conf.d", "/etc/localtime"]


@dataclass
class SandboxRequest:
    argv: list[str]
    env: dict[str, str]
    workspace: Path  # host path, writable inside the sandbox
    state_dir: Path  # host per-attempt dir for launcher files (never mounted)
    sandbox_id: str
    inbox: Path | None = None  # host dir with prompt/task files, mounted read-only
    ro_mounts: list[tuple[str, str]] = field(default_factory=list)  # (host_src, sandbox_dst)
    rw_mounts: list[tuple[str, str]] = field(default_factory=list)
    network: str = "none"  # none | host | restricted
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    cwd: str | None = None  # inside the sandbox; default = workspace


@dataclass
class WrappedCommand:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    cleanup: Callable[[], None] = lambda: None
    terminate: Callable[[], None] | None = None
    identity: str = ""


class SandboxBackend(ABC):
    name: str

    @abstractmethod
    async def check(self) -> tuple[bool, str]:
        """(available, human-readable reason)."""

    @abstractmethod
    def wrap(self, req: SandboxRequest) -> WrappedCommand: ...

    def workspace_path(self, host_workspace: Path) -> str:
        return WORKSPACE

    def inbox_path(self, host_inbox: Path) -> str:
        return INBOX

    @property
    def sandboxed(self) -> bool:
        return True


# ---------------------------------------------------------------------------------------
class UsernsBackend(SandboxBackend):
    name = "userns"

    def __init__(self, extra_ro: Sequence[str] = ()):
        self.extra_ro = list(extra_ro)
        self._checked: tuple[bool, str] | None = None
        self.unshare = shutil.which("unshare")
        self.nsboot = str(Path(__file__).with_name("nsboot.py"))

    def _base_spec(self, req: SandboxRequest, *, argv_probe: bool = False) -> dict:
        ro: list[list[str]] = []
        symlinks: list[list[str]] = []
        ro.append(["/usr", "/usr"])
        for d in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
            if os.path.islink(d):
                symlinks.append([os.readlink(d), d.lstrip("/")])
            elif os.path.isdir(d):
                ro.append([d, d])
        def add_ro(src: str, dst: str) -> None:
            real = os.path.realpath(src)
            ro.append([real, dst])
            if dst == src and real != dst:
                # a symlinked directory (e.g. a uv-managed Python's unversioned prefix): symlinks inside the tree
                # often point at the *real* path, so it must be visible there as well
                ro.append([real, real])

        def add_ro_with_venv(src: str, dst: str) -> None:
            add_ro(src, dst)
            # A mounted virtualenv is only usable if the interpreter prefix named by its pyvenv.cfg ("home =") is
            # visible at that very path (it is often an unversioned symlink such as cpython-3.14-linux-x86_64-gnu).
            cfg = Path(src) / "pyvenv.cfg"
            if dst == src and cfg.is_file():
                for line in cfg.read_text(errors="replace").splitlines():
                    key, _, val = line.partition("=")
                    if key.strip() == "home" and os.path.isabs(val.strip()):
                        prefix = os.path.dirname(val.strip().rstrip("/"))
                        if os.path.isdir(prefix):
                            add_ro(prefix, prefix)

        for src in self.extra_ro:
            add_ro_with_venv(src, src)
        for src, dst in req.ro_mounts:
            add_ro_with_venv(src, dst)
        if req.inbox is not None:
            ro.append([str(req.inbox), INBOX])
        rw = [[str(req.workspace), WORKSPACE]] + [[s, d] for s, d in req.rw_mounts]
        env = {"PATH": SANDBOX_PATH, "HOME": SANDBOX_HOME, "TMPDIR": "/tmp", "LANG": "C.UTF-8"}
        env.update(req.env)
        etc_files = {
            "passwd": f"root:x:0:0:agent:{SANDBOX_HOME}:/bin/sh\nnobody:x:65534:65534:nobody:/:/usr/sbin/nologin\n",
            "group": "root:x:0:\nnogroup:x:65534:\n",
            "hosts": "127.0.0.1 localhost\n",
            "resolv.conf": "",
            "hostname": "mg-sandbox\n",
        }
        etc_ro = list(ETC_RO)
        if req.network == "host":
            # An explicitly host-networked agent (hosted-model CLI) needs working name resolution: expose the
            # host's resolver configuration read-only instead of the empty placeholder files.
            for f in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
                if os.path.exists(f):
                    etc_ro.append(f)
                    etc_files.pop(os.path.basename(f), None)
        return {
            "rootfs": str(req.state_dir / "rootfs"),
            "symlinks": symlinks,
            "ro_binds": ro,
            "rw_binds": rw,
            "etc_files": etc_files,
            "etc_ro": etc_ro,
            "tmpfs": [["/tmp", req.limits.tmp_mb], [SANDBOX_HOME, req.limits.tmp_mb]],
            "cwd": req.cwd or WORKSPACE,
            "env": env,
            "rlimits": req.limits.rlimits(),
            "hostname": "mg-sandbox",
            "loopback": req.network == "none",
        }

    def wrap(self, req: SandboxRequest) -> WrappedCommand:
        if self.unshare is None:
            raise IsolationUnavailableError("unshare(1) not found")
        if req.network == "restricted":
            raise IsolationUnavailableError(
                "userns backend cannot provide 'restricted' egress; use network: none, or a container "
                "backend with an administrator-defined restricted network"
            )
        req.state_dir.mkdir(parents=True, exist_ok=True)
        (req.state_dir / "rootfs").mkdir(exist_ok=True)
        spec = self._base_spec(req)
        # The command (and every hop of its symlink chain) must be visible inside the sandbox. Coverage is judged by
        # where things are mounted (destinations), not where they come from.
        def covered(path: str) -> bool:
            return any(path == d or path.startswith(d.rstrip("/") + "/")
                       for _, d in [*spec["ro_binds"], *spec["rw_binds"]]) or path.startswith("/usr/")

        exe = req.argv[0]
        if os.path.isabs(exe):
            hop, seen = exe, set()
            while True:
                if not covered(hop):
                    d = os.path.dirname(hop)
                    spec["ro_binds"].append([os.path.realpath(d), d])
                if not os.path.islink(hop) or hop in seen:
                    break
                seen.add(hop)
                tgt = os.readlink(hop)
                hop = tgt if os.path.isabs(tgt) else os.path.normpath(os.path.join(os.path.dirname(hop), tgt))
        # Script-style CLIs (`#!/usr/bin/env node`) find their runtime next to themselves (nvm, /opt/<tool>/bin):
        # make the command's own directories (outside /usr) part of the sandbox PATH.
        if os.path.isabs(exe) and "PATH" not in req.env:
            extra, hop, seen = [], exe, set()
            while True:
                d = os.path.dirname(hop)
                if not d.startswith("/usr/") and d not in extra:
                    extra.append(d)
                if not os.path.islink(hop) or hop in seen:
                    break
                seen.add(hop)
                tgt = os.readlink(hop)
                hop = tgt if os.path.isabs(tgt) else os.path.normpath(os.path.join(os.path.dirname(hop), tgt))
            if extra:
                spec["env"]["PATH"] = ":".join([*extra, spec["env"]["PATH"]])
        spec_path = req.state_dir / "nsboot.json"
        fd = os.open(spec_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        flags = ["--user", "--map-root-user", "--mount", "--pid", "--fork", "--kill-child", "--ipc", "--uts"]
        if req.network == "none":
            flags.append("--net")
        argv = [self.unshare, *flags, "--", sys.executable, "-I", self.nsboot, str(spec_path), "--",
                *req.argv]
        env = {"PATH": LAUNCHER_PATH, "LANG": "C.UTF-8", "MULTIGENZ_SANDBOX_ID": req.sandbox_id}

        def cleanup() -> None:
            try:
                spec_path.unlink()
            except OSError:
                pass

        return WrappedCommand(argv=argv, env=env, cwd=Path("/"), cleanup=cleanup,
                              identity=f"userns:{req.sandbox_id}")

    async def check(self) -> tuple[bool, str]:
        if self._checked is not None:
            return self._checked
        if self.unshare is None:
            self._checked = (False, "unshare(1) from util-linux not found")
            return self._checked
        if sys.platform != "linux":
            self._checked = (False, "userns backend requires Linux")
            return self._checked
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mg-probe-") as td:
            tdp = Path(td)
            ws = tdp / "ws"
            ws.mkdir()
            req = SandboxRequest(argv=["/usr/bin/true"], env={}, workspace=ws, state_dir=tdp / "st",
                                 sandbox_id="probe", network="none")
            try:
                w = self.wrap(req)
                proc = await asyncio.create_subprocess_exec(
                    *w.argv, env=w.env, cwd="/", stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
                out, _ = await asyncio.wait_for(proc.communicate(), 20)
                if proc.returncode == 0:
                    self._checked = (True, "unprivileged user/mount/pid/net namespaces available")
                else:
                    self._checked = (False, "namespace probe failed: " + out.decode(errors="replace").strip()[:300])
            except Exception as exc:  # noqa: BLE001
                self._checked = (False, f"namespace probe error: {exc}")
        return self._checked


# ---------------------------------------------------------------------------------------
class ContainerBackend(SandboxBackend):
    """Rootless podman/docker. Argv construction is unit-tested; a live runtime is required
    for actual execution and is verified by :meth:`check` (rootless mode only)."""

    def __init__(self, runtime: str, image: str | None, network_name: str | None,
                 extra_ro: Sequence[str] = ()):
        self.name = "rootless-container"
        self.runtime_pref = runtime
        self.runtime: str | None = None
        self.image = image
        self.network_name = network_name
        self.extra_ro = list(extra_ro)

    def _find_runtime(self) -> str | None:
        cands = ["podman", "docker"] if self.runtime_pref == "auto" else [self.runtime_pref]
        for c in cands:
            if shutil.which(c):
                return c
        return None

    async def check(self) -> tuple[bool, str]:
        rt = self._find_runtime()
        if rt is None:
            return False, "no container runtime (podman/docker) found"
        if not self.image:
            return False, "execution.container_image is not configured"
        fmt = "{{.Host.Security.Rootless}}" if rt == "podman" else "{{.SecurityOptions}}"
        try:
            proc = await asyncio.create_subprocess_exec(
                rt, "info", "--format", fmt, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"PATH": LAUNCHER_PATH, "HOME": os.environ.get("HOME", "/")})
            out, err = await asyncio.wait_for(proc.communicate(), 20)
        except Exception as exc:  # noqa: BLE001
            return False, f"{rt} info failed: {exc}"
        if proc.returncode != 0:
            return False, f"{rt} daemon unavailable: {err.decode(errors='replace').strip()[:200]}"
        text = out.decode().strip().lower()
        rootless = text == "true" if rt == "podman" else "rootless" in text
        if not rootless:
            return False, f"{rt} is not running rootless"
        self.runtime = rt
        return True, f"{rt} rootless"

    def build_argv(self, req: SandboxRequest, *, runtime: str, env_file: Path | None) -> list[str]:
        if not self.image:
            raise IsolationUnavailableError("execution.container_image is not configured")
        lim = req.limits
        if req.network == "restricted":
            if not self.network_name:
                raise IsolationUnavailableError(
                    "network: restricted requires execution.container_network (admin-defined egress policy)")
            net = self.network_name
        elif req.network == "host":
            net = "host"
        else:
            net = "none"
        argv = [runtime, "run", "--rm", "--name", f"mg-{req.sandbox_id}", "--network", net,
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                "--pids-limit", str(lim.pids_limit), "--memory", f"{lim.memory_mb}m",
                "--ulimit", f"fsize={lim.max_file_mb * 1024 * 1024}", "--ulimit", f"nofile={lim.max_open_files}",
                "--tmpfs", f"/tmp:rw,nosuid,size={lim.tmp_mb}m", "--tmpfs", f"{SANDBOX_HOME}:rw,nosuid,size={lim.tmp_mb}m",
                "-v", f"{req.workspace}:{WORKSPACE}:rw", "-w", req.cwd or WORKSPACE, "-e", f"HOME={SANDBOX_HOME}"]
        if req.inbox is not None:
            argv += ["-v", f"{req.inbox}:{INBOX}:ro"]
        for src in self.extra_ro:
            argv += ["-v", f"{src}:{src}:ro"]
        for src, dst in req.ro_mounts:
            argv += ["-v", f"{src}:{dst}:ro"]
        for src, dst in req.rw_mounts:
            argv += ["-v", f"{src}:{dst}:rw"]
        if env_file is not None:
            argv += ["--env-file", str(env_file)]
        return [*argv, self.image, *req.argv]

    def wrap(self, req: SandboxRequest) -> WrappedCommand:
        runtime = self.runtime or self._find_runtime()
        if runtime is None:
            raise IsolationUnavailableError("no container runtime found")
        req.state_dir.mkdir(parents=True, exist_ok=True)
        env_file = req.state_dir / "container.env"  # secrets stay out of argv / ps output
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for k, v in req.env.items():
                if "\n" in v or "\n" in k:
                    raise IsolationUnavailableError("newline in environment value")
                fh.write(f"{k}={v}\n")
        argv = self.build_argv(req, runtime=runtime, env_file=env_file)
        name = f"mg-{req.sandbox_id}"

        def terminate() -> None:
            subprocess.run([runtime, "kill", name], capture_output=True, check=False, timeout=15)

        def cleanup() -> None:
            try:
                env_file.unlink()
            except OSError:
                pass

        return WrappedCommand(argv=argv, env={"PATH": LAUNCHER_PATH, "HOME": os.environ.get("HOME", "/"),
                                              "MULTIGENZ_SANDBOX_ID": req.sandbox_id},
                              cwd=Path("/"), cleanup=cleanup, terminate=terminate,
                              identity=f"{runtime}:{name}")


# ---------------------------------------------------------------------------------------
class NoSandbox(SandboxBackend):
    """Explicit opt-in (policy: isolation none + allow_unsandboxed). Never selected implicitly."""

    name = "none"

    @property
    def sandboxed(self) -> bool:
        return False

    async def check(self) -> tuple[bool, str]:
        return True, "UNSANDBOXED execution explicitly allowed by policy"

    def workspace_path(self, host_workspace: Path) -> str:
        return str(host_workspace)

    def inbox_path(self, host_inbox: Path) -> str:
        return str(host_inbox)

    def wrap(self, req: SandboxRequest) -> WrappedCommand:
        env = {"PATH": SANDBOX_PATH, "LANG": "C.UTF-8", "HOME": str(req.state_dir / "home"),
               "TMPDIR": str(req.state_dir / "tmp")}
        (req.state_dir / "home").mkdir(parents=True, exist_ok=True)
        (req.state_dir / "tmp").mkdir(parents=True, exist_ok=True)
        env.update(req.env)
        env["MULTIGENZ_SANDBOX_ID"] = req.sandbox_id
        return WrappedCommand(argv=list(req.argv), env=env, cwd=Path(req.cwd or req.workspace),
                              identity=f"none:{req.sandbox_id}")


async def select_backend(policy: Policy) -> SandboxBackend:
    """Return the configured backend or raise IsolationUnavailableError (fail closed)."""
    ex = policy.execution
    backend: SandboxBackend
    if ex.isolation == "userns":
        backend = UsernsBackend(extra_ro=ex.readonly_paths)
    elif ex.isolation == "rootless-container":
        backend = ContainerBackend(ex.container_runtime, ex.container_image, ex.container_network,
                                   extra_ro=ex.readonly_paths)
    else:
        backend = NoSandbox()
    ok, reason = await backend.check()
    if not ok:
        raise IsolationUnavailableError(
            f"required isolation '{ex.isolation}' is unavailable: {reason}. "
            "MultiGenz never falls back to unsandboxed execution.")
    return backend
