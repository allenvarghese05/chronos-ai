"""Seriality and crash recovery are tested with real independent worker processes."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import JobKind
from chronos.jobs.queue import JobInput, JobQueue
from chronos.jobs.worker import Handler, Worker

HARNESS = """
import datetime as dt,sys,threading,time
from pathlib import Path
from chronos.db import create_db_engine
from chronos.enums import JobKind
from chronos.jobs.queue import JobQueue
from chronos.jobs.worker import Handler,Worker
engine=create_db_engine(sys.argv[1])
queue=JobQueue(engine)
clock=lambda: dt.datetime.now(dt.UTC)+dt.timedelta(seconds=int(sys.argv[4]))
worker=Worker(queue,Path(sys.argv[2]),{JobKind.ingest:Handler('tests.jobs.handlers',sys.argv[3])},clock=clock)
stop=threading.Event()
deadline=time.monotonic()+8
while time.monotonic()<deadline:
    if worker.run_once(stop): break
    time.sleep(0.02)
engine.dispose()
"""


def process(
    settings: Settings, root: Path, handler: str, offset: int = 0
) -> subprocess.Popen[bytes]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2])
    return subprocess.Popen(
        [sys.executable, "-c", HARNESS, settings.database_url, str(root), handler, str(offset)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_file(path: Path, timeout: float = 5) -> str:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError("supervised handler did not start")


def dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def test_handler_success_failure_and_unsupported(test_settings: Settings, tmp_path: Path) -> None:
    engine = create_db_engine(test_settings.database_url)
    queue = JobQueue(engine)
    now = dt.datetime.now(dt.UTC)
    row = queue.enqueue(JobInput(kind=JobKind.ingest, key="success"), now)
    queue.enqueue(JobInput(kind=JobKind.render, key="unregistered"), now)
    worker = Worker(queue, tmp_path, {JobKind.ingest: Handler("tests.jobs.handlers", "success")})
    assert worker.run_once(threading.Event())
    assert row.id is not None
    completed = queue.get(row.id)
    assert completed and completed.status == "done"
    assert not worker.run_once(threading.Event())
    failed = queue.enqueue(JobInput(kind=JobKind.ingest, key="failure", max_attempts=1), now)
    worker.handlers[JobKind.ingest] = Handler("tests.jobs.handlers", "failing")
    assert worker.run_once(threading.Event())
    assert failed.id is not None
    final = queue.get(failed.id)
    assert final and final.status == "failed" and final.last_error == "handler_failed"
    engine.dispose()


def test_real_python_and_term_ignoring_descendants_timeout(
    test_settings: Settings, tmp_path: Path
) -> None:
    engine = create_db_engine(test_settings.database_url)
    queue = JobQueue(engine)
    for name in ("sleeper", "descendants"):
        row = queue.enqueue(
            JobInput(kind=JobKind.ingest, key=name, timeout_sec=2, max_attempts=1),
            dt.datetime.now(dt.UTC),
        )
        worker = Worker(queue, tmp_path, {JobKind.ingest: Handler("tests.jobs.handlers", name)})
        started = time.monotonic()
        assert worker.run_once(threading.Event())
        assert time.monotonic() - started < 6
        assert row.id is not None
        final = queue.get(row.id)
        assert final and final.status == "failed" and final.last_error == "timeout"
        filename = "sleeper.pid" if name == "sleeper" else "descendants.pid"
        pids = wait_file(tmp_path / filename).split(",")
        assert all(dead(int(pid)) for pid in pids)
    engine.dispose()


def test_distinct_workers_different_jobs_never_overlap(
    test_settings: Settings, tmp_path: Path
) -> None:
    engine = create_db_engine(test_settings.database_url)
    queue = JobQueue(engine)
    for number in range(2):
        queue.enqueue(JobInput(kind=JobKind.ingest, key=str(number)), dt.datetime.now(dt.UTC))
    workers = [process(test_settings, tmp_path, "interval") for _ in range(2)]
    try:
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=12)
            assert worker.returncode == 0, (stdout, stderr)
        intervals = [json.loads(line) for line in (tmp_path / "intervals").read_text().splitlines()]
        assert [line[0] for line in intervals] == ["start", "end", "start", "end"]
        assert intervals[1][2] <= intervals[2][2]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    engine.dispose()


def test_worker_crash_reaps_before_reclaim_and_retry(
    test_settings: Settings, tmp_path: Path
) -> None:
    engine = create_db_engine(test_settings.database_url)
    queue = JobQueue(engine)
    now = dt.datetime.now(dt.UTC)
    row = queue.enqueue(JobInput(kind=JobKind.ingest, key="crash", timeout_sec=3), now)
    first = process(test_settings, tmp_path, "crash_first")
    try:
        child_pid = int(wait_file(tmp_path / "crash-first.pid"))
        contender = Worker(
            queue,
            tmp_path,
            {JobKind.ingest: Handler("tests.jobs.handlers", "crash_first")},
            clock=lambda: now + dt.timedelta(seconds=10),
        )
        assert not contender.run_once(threading.Event())
        first.kill()
        first.wait(timeout=2)
        until = time.monotonic() + 3
        while not dead(child_pid) and time.monotonic() < until:
            time.sleep(0.02)
        assert dead(child_pid)
        # Wait for the guardian's assertion/FD cleanup, without running another heavy attempt.
        from chronos.jobs.ownership import ExecutionLock

        probe = ExecutionLock(tmp_path)
        while not probe.acquire() and time.monotonic() < until:
            time.sleep(0.02)
        assert probe.fd is not None
        probe.close()
        assert not contender.run_once(threading.Event())
        assert row.id is not None
        reclaimed = queue.get(row.id)
        assert reclaimed and reclaimed.status == "queued" and reclaimed.last_error == "worker_lost"
        contender.clock = lambda: now + dt.timedelta(seconds=20)
        assert contender.run_once(threading.Event())
        final = queue.get(row.id)
        assert final and final.status == "done" and final.attempts == 2
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()
    engine.dispose()
