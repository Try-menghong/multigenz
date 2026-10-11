"""Resource ceilings applied to sandboxed processes."""

from __future__ import annotations

from dataclasses import dataclass

from multigenz.config import ResourceLimitsCfg


@dataclass(frozen=True)
class ResourceLimits:
    max_file_mb: int = 512
    max_open_files: int = 4096
    max_processes: int | None = None
    cpu_seconds: int | None = None
    address_space_mb: int | None = None
    memory_mb: int = 4096  # enforced by container backends only (cgroups)
    tmp_mb: int = 512
    pids_limit: int = 512

    @classmethod
    def from_cfg(cls, cfg: ResourceLimitsCfg, *, timeout_seconds: float | None = None) -> "ResourceLimits":
        cpu = cfg.cpu_seconds
        if cpu is None and timeout_seconds is not None:
            cpu = int(timeout_seconds) + 5
        return cls(
            max_file_mb=cfg.max_file_mb,
            max_open_files=cfg.max_open_files,
            max_processes=cfg.max_processes,
            cpu_seconds=cpu,
            address_space_mb=cfg.address_space_mb,
            memory_mb=cfg.memory_mb,
            tmp_mb=cfg.tmp_mb,
            pids_limit=cfg.pids_limit,
        )

    def rlimits(self) -> dict[str, int]:
        """Mapping of RLIMIT_* name -> value for the namespace bootstrap."""
        out = {"RLIMIT_FSIZE": self.max_file_mb * 1024 * 1024, "RLIMIT_NOFILE": self.max_open_files,
               "RLIMIT_CORE": 0}
        if self.max_processes is not None:
            out["RLIMIT_NPROC"] = self.max_processes
        if self.cpu_seconds is not None:
            out["RLIMIT_CPU"] = self.cpu_seconds
        if self.address_space_mb is not None:
            out["RLIMIT_AS"] = self.address_space_mb * 1024 * 1024
        return out
