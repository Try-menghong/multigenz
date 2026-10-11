"""Run ownership: one orchestrator process per run, enforced with an OS file lock.

flock is released by the kernel when the holder dies, so a SIGKILLed orchestrator never leaves a
stale lock - which is exactly what restart reconciliation relies on.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from multigenz.errors import RunLockedError


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RunLockedError(f"run is owned by another process ({self.path})") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    @staticmethod
    def is_held(path: Path) -> bool:
        """True if some process currently holds the lock (non-destructive probe)."""
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)
