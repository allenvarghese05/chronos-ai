"""One stoppable in-process orchestrator; killable execution lives behind the supervisor."""

from __future__ import annotations

import datetime as dt
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from chronos.enums import JobKind
from chronos.jobs.ownership import ExecutionLock
from chronos.jobs.queue import ErrorCode, JobQueue
from chronos.media.subprocess import run_handler


@dataclass(frozen=True)
class Handler:
    """Trusted importable function identity supplied by application code, never a job payload."""

    module: str
    function: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", self.module)
            or not self.function.isidentifier()
            or self.function.startswith("_")
        ):
            raise ValueError("handler must be a trusted importable module/function")


@dataclass(frozen=True)
class JobContext:
    """Immutable attempt inputs; parse a copy of payload_json when a handler needs mutable data."""

    job_id: int
    owner: str
    payload_json: str
    data_root: Path
    work_dir: Path


class Worker:
    """Serial execution within one canonical host data-root domain, with short DB transactions."""

    def __init__(
        self,
        queue: JobQueue,
        data_root: Path,
        handlers: Mapping[JobKind, Handler],
        *,
        clock: Callable[[], dt.datetime] | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        self.queue = queue
        self.data_root = data_root.resolve()
        self.handlers = dict(handlers)
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.poll_interval = poll_interval
        self.blocked = False

    def run_once(self, stop: threading.Event) -> bool:
        """Reclaim/claim only under real ownership; no new attempt follows uncertain cleanup."""
        if stop.is_set() or self.blocked or not self.handlers:
            return False
        ownership = ExecutionLock(self.data_root)
        if not ownership.acquire():
            return False
        try:
            self.queue.reclaim(self.clock())
            claim = self.queue.claim(set(self.handlers), self.clock())
            if claim is None:
                return False
            assert ownership.fd is not None
            directory = self.data_root / "jobs" / str(claim.id) / claim.owner
            directory.mkdir(parents=True, exist_ok=False)
            if not directory.resolve().is_relative_to(self.data_root):
                raise ValueError("job working directory escapes data root")
            handler = self.handlers[claim.kind]
            outcome = run_handler(
                {
                    "module": handler.module,
                    "function": handler.function,
                    "job_id": claim.id,
                    "owner": claim.owner,
                    "payload_json": claim.payload_json,
                    "data_root": str(self.data_root),
                    "work_dir": str(directory),
                },
                cwd=directory,
                data_root=self.data_root,
                lock_fd=ownership.fd,
                cancel=stop,
                timeout=claim.timeout_sec,
            )
            if outcome.status == "cleanup_uncertain":
                self.blocked = True
            elif outcome.status == "done":
                self.queue.complete(claim, self.clock(), outcome.duration_ms)
            else:
                from typing import cast

                self.queue.fail(
                    claim, self.clock(), outcome.duration_ms, cast(ErrorCode, outcome.status)
                )
            return True
        finally:
            ownership.close()

    def run_forever(self, stop: threading.Event) -> None:
        """Poll without blocking the API event loop; shutdown interrupts all waits/execution."""
        while not stop.is_set():
            if not self.run_once(stop):
                stop.wait(self.poll_interval)
