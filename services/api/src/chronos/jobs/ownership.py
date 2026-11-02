"""POSIX host execution ownership: inherited FDs keep one flock alive after worker death."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path


class ExecutionLock:
    """One non-unlinked lock inode per canonical configured data-root execution domain.

    Close, never LOCK_UN: descendants share the open file description and must keep ownership
    until they stop. Changing the configured data root deliberately changes the execution domain.
    """

    def __init__(self, data_root: Path) -> None:
        self.root = data_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / "jobs"
        directory.mkdir(exist_ok=True)
        if not directory.resolve().is_relative_to(self.root):
            raise ValueError("execution lock directory escapes data root")
        self.path = directory / "execution.lock"
        self.fd: int | None = None

    def acquire(self) -> bool:
        """Acquire without waiting; symlinks/nonregular/multiply-linked lock files are refused."""
        if self.fd is not None:
            raise RuntimeError("execution lock already acquired")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            os.close(descriptor)
            raise ValueError("invalid execution lock inode")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        self.fd = descriptor
        return True

    def close(self) -> None:
        """Release this process's reference; a live supervisor/descendant retains ownership."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
