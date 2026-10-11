#!/usr/bin/env python3
"""Namespace bootstrap executed *inside* fresh user/mount/pid/ipc/uts(/net) namespaces.

Standard library only, run with ``python -I``. It builds a minimal root filesystem
(tmpfs root; read-only binds of selected host paths; a single read-write workspace),
pivots into it, applies resource limits, drops every capability, sets no_new_privs and
finally execs the real command. Anything that cannot be set up aborts with a non-zero exit
(fail closed): the command is never started in a half-isolated state.

usage: nsboot.py <spec.json> -- <command> [args...]
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import platform
import resource
import socket
import struct
import sys

MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_NOATIME, MS_NODIRATIME, MS_BIND = 32, 1024, 2048, 4096
MS_REC, MS_PRIVATE = 16384, 1 << 18
MNT_DETACH = 2
PR_CAPBSET_DROP, PR_SET_NO_NEW_PRIVS, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 24, 38, 47, 4

SYSCALLS = {"x86_64": {"pivot_root": 155, "capset": 126}, "aarch64": {"pivot_root": 41, "capset": 91}}

libc = ctypes.CDLL(None, use_errno=True)


def die(msg: str) -> "None":
    sys.stderr.write(f"multigenz-sandbox: {msg}\n")
    sys.stderr.flush()
    os._exit(126)


def mount(src, tgt, fstype=None, flags=0, data=None):
    r = libc.mount(
        src.encode() if src else None,
        tgt.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    if r != 0:
        e = ctypes.get_errno()
        die(f"mount {src or fstype} -> {tgt} failed: {os.strerror(e)}")


def inherited_flags(path: str) -> int:
    st = os.statvfs(path)
    out = 0
    for st_flag, ms_flag in ((os.ST_NOSUID, MS_NOSUID), (os.ST_NODEV, MS_NODEV), (os.ST_NOEXEC, MS_NOEXEC),
                             (os.ST_NOATIME, MS_NOATIME), (os.ST_NODIRATIME, MS_NODIRATIME)):
        if st.f_flag & st_flag:
            out |= ms_flag
    return out


def ensure_target(root: str, dst: str, is_dir: bool) -> str:
    target = os.path.join(root, dst.lstrip("/"))
    if is_dir:
        os.makedirs(target, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not os.path.exists(target):
            open(target, "wb").close()
    return target


def bind(root: str, src: str, dst: str, ro: bool) -> None:
    if not os.path.exists(src):
        return
    is_dir = os.path.isdir(src)
    target = ensure_target(root, dst, is_dir)
    mount(src, target, None, MS_BIND)
    if ro:
        mount(None, target, None, MS_BIND | MS_REMOUNT | MS_RDONLY | inherited_flags(target))


def harden_proc(proc: str) -> None:
    """Like container runtimes: global sysctls read-only, sensitive kernel interfaces masked.

    Without this, an orchestrator running as host root could let a sandboxed process (uid 0 in its user
    namespace, same kuid as host root) pass the DAC check for *global* sysctls such as vm.swappiness.
    """
    for sub in ("sys", "sysrq-trigger", "irq", "bus", "fs"):
        path = os.path.join(proc, sub)
        if os.path.exists(path):
            mount(path, path, None, MS_BIND)
            mount(None, path, None, MS_BIND | MS_REMOUNT | MS_RDONLY | inherited_flags(path))
    for sub in ("kcore", "keys", "timer_list", "sched_debug", "scsi"):
        path = os.path.join(proc, sub)
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            mount("tmpfs", path, "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=0")
        else:
            mount("/dev/null", path, None, MS_BIND)


def drop_privileges() -> None:
    try:
        last = int(open("/proc/sys/kernel/cap_last_cap").read().strip())
    except OSError:
        last = 40
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    for cap in range(last + 1):
        libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        die("cannot set no_new_privs")
    arch = platform.machine()
    nr = SYSCALLS.get(arch, {}).get("capset")
    if nr is None:
        die(f"unsupported architecture {arch}")
    header = struct.pack("Ii", 0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3, pid 0
    data = b"\x00" * 24  # two zeroed cap_user_data_t structs (eff/perm/inh)
    hbuf = ctypes.create_string_buffer(header, len(header))
    dbuf = ctypes.create_string_buffer(data, len(data))
    if libc.syscall(nr, hbuf, dbuf) != 0:
        die(f"capset failed: {os.strerror(ctypes.get_errno())}")


def loopback_up() -> None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifreq = struct.pack("16sH14s", b"lo", 0x1 | 0x40, b"\x00" * 14)  # IFF_UP | IFF_RUNNING
        fcntl.ioctl(s, 0x8914, ifreq)  # SIOCSIFFLAGS
        s.close()
    except OSError:
        pass


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        die("usage: nsboot.py <spec.json> -- cmd...")
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    cmd = sys.argv[3:]
    root = spec["rootfs"]

    mount(None, "/", None, MS_REC | MS_PRIVATE)
    mount("tmpfs", root, "tmpfs", MS_NOSUID | MS_NODEV, "mode=755,size=16m")

    for link_target, name in spec.get("symlinks", []):
        os.symlink(link_target, os.path.join(root, name))
    # tmpfs first: binds whose destination lives under /tmp or $HOME must not be shadowed later.
    for path, size_mb in spec.get("tmpfs", []):
        t = ensure_target(root, path, True)
        mount("tmpfs", t, "tmpfs", MS_NOSUID | MS_NODEV, f"mode=1777,size={int(size_mb)}m")
    for src, dst in sorted(spec.get("ro_binds", []), key=lambda p: p[1].count("/")):
        bind(root, src, dst, ro=True)

    # /etc: generated minimal files plus read-only binds of selected host files.
    etc = os.path.join(root, "etc")
    os.makedirs(etc, exist_ok=True)
    mount("tmpfs", etc, "tmpfs", MS_NOSUID | MS_NODEV, "mode=755,size=1m")
    for name, content in spec.get("etc_files", {}).items():
        with open(os.path.join(etc, name), "w", encoding="utf-8") as fh:
            fh.write(content)
    for src in spec.get("etc_ro", []):
        bind(root, src, src, ro=True)
    mount(None, etc, None, MS_BIND | MS_REMOUNT | MS_RDONLY | inherited_flags(etc))

    # /dev: tmpfs with bind-mounted harmless device nodes.
    dev = os.path.join(root, "dev")
    os.makedirs(dev, exist_ok=True)
    mount("tmpfs", dev, "tmpfs", MS_NOSUID, "mode=755,size=1m")
    for name in ("null", "zero", "full", "random", "urandom", "tty"):
        src = f"/dev/{name}"
        if os.path.exists(src):
            open(os.path.join(dev, name), "wb").close()
            mount(src, os.path.join(dev, name), None, MS_BIND)
    for link, target in (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                         ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2")):
        os.symlink(target, os.path.join(dev, link))
    os.makedirs(os.path.join(dev, "shm"), exist_ok=True)
    mount(None, dev, None, MS_BIND | MS_REMOUNT | MS_RDONLY | inherited_flags(dev))
    mount("tmpfs", os.path.join(dev, "shm"), "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "mode=1777,size=64m")

    os.makedirs(os.path.join(root, "proc"), exist_ok=True)
    mount("proc", os.path.join(root, "proc"), "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    harden_proc(os.path.join(root, "proc"))

    for src, dst in sorted(spec.get("rw_binds", []), key=lambda p: p[1].count("/")):
        if not os.path.exists(src):
            die(f"required read-write path missing: {src}")
        bind(root, src, dst, ro=False)

    arch = platform.machine()
    nr = SYSCALLS.get(arch, {}).get("pivot_root")
    if nr is None:
        die(f"unsupported architecture {arch}")
    old = os.path.join(root, ".old")
    os.makedirs(old, exist_ok=True)
    os.chdir(root)
    if libc.syscall(nr, root.encode(), old.encode()) != 0:
        die(f"pivot_root failed: {os.strerror(ctypes.get_errno())}")
    os.chdir("/")
    if libc.umount2(b"/.old", MNT_DETACH) != 0:
        die(f"umount old root failed: {os.strerror(ctypes.get_errno())}")
    os.rmdir("/.old")
    # Make the new root itself read-only (workspace/tmpfs mounts stay writable).
    mount(None, "/", None, MS_BIND | MS_REMOUNT | MS_RDONLY | inherited_flags("/"))

    if spec.get("hostname"):
        socket.sethostname(spec["hostname"])
    if spec.get("loopback"):
        loopback_up()

    for name, value in spec.get("rlimits", {}).items():
        res = getattr(resource, name)
        resource.setrlimit(res, (value, value))

    drop_privileges()
    os.chdir(spec.get("cwd", "/"))
    env = spec.get("env", {})
    try:
        os.execvpe(cmd[0], cmd, env)
    except OSError as exc:
        # The sandbox itself is fine; the *command* could not be started. Distinct exit codes let the
        # orchestrator tell a missing/unusable agent binary (provider problem) from a broken sandbox (126).
        sys.stderr.write(f"multigenz-sandbox-exec: cannot execute {cmd[0]!r}: {exc}\n")
        sys.stderr.flush()
        os._exit(127 if exc.errno == errno.ENOENT else 125)


if __name__ == "__main__":
    main()
