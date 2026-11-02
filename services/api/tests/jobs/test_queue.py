"""Queue races use independent real SQLite connections, not a mocked claim order."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from pydantic import ValidationError

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import JobKind
from chronos.jobs.queue import Claim, JobInput, JobQueue
from chronos.models import Job

NOW = dt.datetime(2026, 11, 1, 6, tzinfo=dt.UTC)


@pytest.fixture
def queue(test_settings: Settings) -> Iterator[JobQueue]:
    engine = create_db_engine(test_settings.database_url)
    yield JobQueue(engine)
    engine.dispose()


def test_concurrent_enqueue_preserves_original(queue: JobQueue) -> None:
    def enqueue(n: int) -> Job:
        return queue.enqueue(JobInput(kind=JobKind.ingest, key="same", payload={"n": n}), NOW)

    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(enqueue, [1, 2]))
    assert rows[0].id == rows[1].id
    assert rows[0].payload_json == rows[1].payload_json
    assert rows[0].attempts == 0


def test_concurrent_claim_only_once(queue: JobQueue) -> None:
    queue.enqueue(JobInput(kind=JobKind.ingest, key="one"), NOW)

    def take(_: int) -> Claim | None:
        return queue.claim({JobKind.ingest}, NOW)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(take, range(2)))
    assert sum(row is not None for row in results) == 1
    claim = next(row for row in results if row is not None)
    assert claim.attempts == 1


def test_due_priority_and_unsupported_selection(queue: JobQueue) -> None:
    queue.enqueue(JobInput(kind=JobKind.render, key="unsupported", priority=-100), NOW)
    queue.enqueue(
        JobInput(kind=JobKind.ingest, key="future", priority=-90), NOW, NOW + dt.timedelta(hours=1)
    )
    first = queue.enqueue(JobInput(kind=JobKind.ingest, key="first", priority=0), NOW)
    queue.enqueue(JobInput(kind=JobKind.ingest, key="second", priority=10), NOW)
    claim = queue.claim({JobKind.ingest}, NOW)
    assert claim and claim.id == first.id
    assert queue.claim(set(), NOW) is None


def test_retries_exhaust_and_stale_owner_cannot_write(queue: JobQueue) -> None:
    row = queue.enqueue(JobInput(kind=JobKind.ingest, key="retry"), NOW)
    claim = queue.claim({JobKind.ingest}, NOW)
    assert claim is not None and row.id is not None
    assert queue.fail(claim, NOW, 12, "handler_failed")
    assert queue.claim({JobKind.ingest}, NOW + dt.timedelta(seconds=4)) is None
    second = queue.claim({JobKind.ingest}, NOW + dt.timedelta(seconds=5))
    assert second and second.owner != claim.owner and second.attempts == 2
    assert not queue.complete(claim, NOW, 0)
    assert not queue.fail(claim, NOW, 0, "timeout")
    assert not queue.complete(replace(second, owner="forged"), NOW, 0)
    assert queue.fail(second, NOW + dt.timedelta(seconds=5), 5, "timeout")
    third = queue.claim({JobKind.ingest}, NOW + dt.timedelta(seconds=15))
    assert third and third.attempts == 3
    assert queue.fail(third, NOW + dt.timedelta(seconds=15), 5, "handler_failed")
    final = queue.get(row.id)
    assert final and final.status == "failed" and final.finished_at
    assert queue.claim({JobKind.ingest}, NOW + dt.timedelta(days=1)) is None


def test_reclaim_is_fenced_and_after_deadline(queue: JobQueue) -> None:
    row = queue.enqueue(JobInput(kind=JobKind.ingest, key="lost", timeout_sec=5), NOW)
    claim = queue.claim({JobKind.ingest}, NOW)
    assert claim and row.id is not None
    assert queue.reclaim(NOW + dt.timedelta(seconds=4)) == 0
    assert queue.reclaim(NOW + dt.timedelta(seconds=5)) == 1
    assert not queue.complete(claim, NOW, 1)
    retry = queue.get(row.id)
    assert retry and retry.status == "queued" and retry.last_error == "worker_lost"


def test_aware_offset_normalized_and_naive_rejected(queue: JobQueue) -> None:
    offset = NOW.astimezone(dt.timezone(dt.timedelta(hours=-5)))
    row = queue.enqueue(JobInput(kind=JobKind.ingest, key="aware"), offset)
    assert row.run_after == "2026-11-01T06:00:00.000000Z"
    assert queue.claim({JobKind.ingest}, NOW)
    with pytest.raises(ValueError):
        queue.enqueue(JobInput(kind=JobKind.ingest, key="naive"), NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "change",
    [
        {"key": ""},
        {"priority": True},
        {"max_attempts": 0},
        {"timeout_sec": 0},
        {"payload": {"big": "x" * 17000}},
        {"payload": {"bad": float("nan")}},
    ],
)
def test_invalid_enqueue_input(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        JobInput.model_validate({"kind": "ingest", "key": "key", **change})
