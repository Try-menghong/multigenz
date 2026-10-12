"""`multigenz doctor`: environment, isolation and adapter capability checks."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from multigenz.adapters.registry import build_adapters
from multigenz.config import Policy, default_state_home
from multigenz.errors import ConfigError
from multigenz.execution.sandbox import (
    ContainerBackend,
    NoSandbox,
    SandboxBackend,
    UsernsBackend,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True
    level: str = ""  # "", "warn"

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        return "FAIL" if self.required else "warn"


def check_python() -> Check:
    ok = sys.version_info >= (3, 12)
    return Check("python>=3.12", ok, sys.version.split()[0])


def check_git() -> Check:
    git = shutil.which("git")
    if not git:
        return Check("git", False, "git not found")
    out = subprocess.run([git, "--version"], capture_output=True, text=True).stdout.strip()
    m = re.search(r"(\d+)\.(\d+)", out)
    ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 38)
    return Check("git>=2.38 (merge-tree --write-tree)", ok, out)


def check_sqlite() -> Check:
    try:
        with tempfile.TemporaryDirectory() as td:
            con = sqlite3.connect(Path(td) / "t.db")
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            con.close()
        return Check("sqlite WAL", mode.lower() == "wal", f"sqlite {sqlite3.sqlite_version}, journal_mode={mode}")
    except sqlite3.Error as exc:
        return Check("sqlite WAL", False, str(exc))


def check_home(home: Path | None = None) -> Check:
    home = home or default_state_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home):
            pass
        return Check("state directory writable", True, str(home))
    except OSError as exc:
        return Check("state directory writable", False, f"{home}: {exc}")


async def check_isolation(policy: Policy) -> list[Check]:
    ex = policy.execution
    checks: list[Check] = []
    backends: list[SandboxBackend] = [UsernsBackend(extra_ro=ex.readonly_paths),
                                      ContainerBackend(ex.container_runtime, ex.container_image,
                                                       ex.container_network, ex.readonly_paths)]
    names = {"userns": "userns", "rootless-container": "rootless-container"}
    for b in backends:
        ok, reason = await b.check()
        required = names.get(ex.isolation) == b.name
        checks.append(Check(f"sandbox backend: {b.name}", ok, reason, required=required))
    if ex.isolation == "none":
        ok, reason = await NoSandbox().check()
        checks.append(Check("sandbox backend: none", True, "WARNING: " + reason, required=False))
    sel = [c for c in checks if c.required]
    if ex.isolation != "none" and not any(c.ok for c in sel):
        checks.append(Check("required isolation", False,
                            f"policy requires '{ex.isolation}' which is unavailable; runs will refuse to start "
                            "(no unsandboxed fallback)"))
    elif ex.isolation != "none":
        checks.append(Check("required isolation", True, f"policy requires '{ex.isolation}'"))
    return checks


async def check_adapters(policy: Policy) -> list[Check]:
    checks: list[Check] = []
    adapters = build_adapters(policy)
    usable = 0
    for name, ad in adapters.items():
        p = await ad.probe()
        if p.enabled:
            usable += 1
            detail = f"{p.invocation} modes={','.join(p.modes)}" + (f" version={p.version}" if p.version else "")
        else:
            detail = p.reason
        checks.append(Check(f"agent: {name}", p.enabled, detail, required=False))
        for w in p.warnings:
            checks.append(Check(f"agent: {name} note", True, w, required=False))
    checks.append(Check("at least one usable agent", usable > 0,
                        f"{usable} of {len(adapters)} adapters enabled"))
    return checks


def check_validation(policy: Policy) -> Check:
    v = policy.validation
    if v.require_tests and not v.profiles:
        return Check("validation profiles", False,
                     "require_tests is true but no validation.profiles are defined: tasks cannot be verified",
                     required=False)
    return Check("validation profiles", True, ", ".join(v.profiles) or "none required")


def check_optional() -> list[Check]:
    import importlib.util as iu

    return [Check(f"optional: {m}", iu.find_spec(m) is not None, "installed" if iu.find_spec(m) else "not installed",
                  required=False) for m in ("textual", "litellm")]


async def run_doctor(policy: Policy | None, *, policy_error: str | None = None,
                     home: Path | None = None, policy_file: Path | None | bool = True) -> list[Check]:
    """``policy_file``: the file the policy came from, ``None`` if built-in defaults were used (no file found)."""
    checks = [check_python(), check_git(), check_sqlite(), check_home(home)]
    if policy is None:
        checks.append(Check("policy", False, policy_error or "policy invalid"))
        return checks
    if policy_file is None:
        checks.append(Check("policy file", False, "none found ($MULTIGENZ_POLICY, ~/.config/multigenz/policy.yaml); "
                            "using built-in defaults (no agents). Run `multigenz init` to create one",
                            required=False))
    checks.append(Check("policy", True, "valid" + (f" ({policy_file})" if isinstance(policy_file, Path) else "")))
    checks += await check_isolation(policy)
    checks += await check_adapters(policy)
    checks.append(check_validation(policy))
    checks += check_optional()
    return checks
