"""T3.5 pipeline: idempotent chains, render modes, prewarm timing, CLI and admin endpoint."""

from __future__ import annotations

import datetime
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from chronos import pipeline
from chronos.cli import app as cli_app
from chronos.config import Settings
from chronos.content.sources import HttpConfig, SelectionConfig, SourceDef, SourcesConfig
from chronos.db import create_db_engine
from chronos.enums import ContentCategory, JobKind, SourceKind
from chronos.http import GuardedHttpClient
from chronos.jobs.queue import JobQueue
from chronos.jobs.worker import Handler
from chronos.main import create_app
from chronos.models.content import Item, Source
from chronos.models.jobs import Job
from chronos.pipeline import (
    STAGE_ORDER,
    StagePayload,
    chain_key,
    enqueue_chain,
    enqueue_next,
    implemented,
    ingest_stage,
    job_key,
    prewarm_due,
    run_chain,
    stages_for,
)
from tests.test_config_wake import auth_headers, insert_device_for_test

NOW = datetime.datetime(2026, 10, 6, 13, 0, tzinfo=datetime.UTC)
DAY = datetime.date(2026, 10, 6)
START = datetime.datetime(2026, 10, 6, 13, 0, tzinfo=datetime.UTC)
END = datetime.datetime(2026, 10, 6, 18, 0, tzinfo=datetime.UTC)


# --- pure rules --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes_from_start", "due"),
    [(-31, False), (-30, True), (-1, True), (0, True), (299, True), (300, False)],
)
def test_prewarm_due_fake_clock(minutes_from_start: int, due: bool) -> None:
    now = START + datetime.timedelta(minutes=minutes_from_start)
    assert prewarm_due(now, START, END) is due


def test_stages_for_render_modes() -> None:
    every = implemented(STAGE_ORDER)
    assert every[:2] == [JobKind.ingest, JobKind.script]  # grows as M4 registers stages
    assert stages_for("eager", "prewarm") == every
    assert stages_for("eager", "confirm") == []
    assert stages_for("on_trigger", "prewarm") == [JobKind.ingest]
    assert stages_for("on_trigger", "confirm") == every[1:]


def test_chain_keys() -> None:
    assert chain_key(DAY, 7, False) == "2026-10-06:7"
    forced = chain_key(DAY, 7, True)
    assert forced.startswith("2026-10-06:7:force:") and forced != chain_key(DAY, 7, True)
    assert job_key(JobKind.script, "2026-10-06:7") == "script:2026-10-06:7"


# --- queue chaining ----------------------------------------------------------------------------


def test_enqueue_chain_idempotent_and_force(test_settings: Settings, tmp_db_url: str) -> None:
    queue = JobQueue(create_db_engine(tmp_db_url))
    stages = [JobKind.script, JobKind.ingest, JobKind.render]  # order + unimplemented fixed up
    first = enqueue_chain(queue, local_date=DAY, device_id=1, stages=stages, now=NOW)
    again = enqueue_chain(queue, local_date=DAY, device_id=1, stages=stages, now=NOW)
    assert first is not None and again is not None and first.id == again.id
    assert first.key == "ingest:2026-10-06:1"
    payload = StagePayload.model_validate_json(first.payload_json)
    assert payload.stages == [JobKind.ingest, JobKind.script]
    forced = enqueue_chain(queue, local_date=DAY, device_id=1, stages=stages, now=NOW, force=True)
    assert forced is not None and forced.id != first.id
    assert (
        enqueue_chain(queue, local_date=DAY, device_id=1, stages=[JobKind.render], now=NOW) is None
    )


def test_enqueue_next_walks_the_chain(test_settings: Settings, tmp_db_url: str) -> None:
    queue = JobQueue(create_db_engine(tmp_db_url))
    payload = StagePayload(
        local_date=DAY, device_id=1, chain="2026-10-06:1", stages=[JobKind.ingest, JobKind.script]
    )
    nxt = enqueue_next(queue, payload, JobKind.ingest, NOW)
    assert nxt is not None and nxt.kind == "script" and nxt.key == "script:2026-10-06:1"
    assert enqueue_next(queue, payload, JobKind.script, NOW) is None


# --- ingest stage ------------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><guid>a1</guid><title>Parliament passes data protection amendment bill</title>
<link>https://news.example.com/a1?utm_source=rss</link>
<pubDate>Tue, 06 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>"""


def test_ingest_stage_syncs_fetches_and_persists(test_settings: Settings, tmp_db_url: str) -> None:
    sources = SourcesConfig(
        version=1,
        http=HttpConfig(user_agent="test"),
        sources=[SourceDef(key="feed", kind=SourceKind.rss, url="https://news.example.com/rss",
                           category=ContentCategory.india)],
        selection=SelectionConfig(category_min={"india": 1}),
    )  # fmt: skip

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    client = GuardedHttpClient(
        "test",
        resolver=resolver,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=RSS, headers={"content-type": "text/xml"})
        ),
    )
    with Session(create_db_engine(tmp_db_url)) as session:
        report = ingest_stage(session, sources, client, "2026-10-06T13:00:00.000000Z")
        assert report.total_items == 1
        assert session.exec(select(Source)).one().last_status == "200"
        item = session.exec(select(Item)).one()
        assert item.url == "https://news.example.com/a1"


# --- run_chain through the real worker subprocess ----------------------------------------------


@pytest.fixture()
def chain_env(test_settings: Settings, tmp_path: Path) -> Path:
    return tmp_path


HANDLERS_OK = {
    JobKind.ingest: Handler("tests.pipeline_handlers", "ok_ingest"),
    JobKind.script: Handler("tests.pipeline_handlers", "ok_script"),
}


def _runs(markers: Path) -> list[str]:  # markers == the chain's data_root
    log = markers / "runs.log"
    return log.read_text().split("\n")[:-1] if log.exists() else []


def test_run_chain_twice_does_not_rerun_unless_forced(
    chain_env: Path, tmp_db_url: str, test_settings: Settings
) -> None:
    engine = create_db_engine(tmp_db_url)
    tmp_path = chain_env
    kw = {"local_date": DAY, "device_id": 1, "stages": [JobKind.ingest, JobKind.script],
          "handlers": HANDLERS_OK, "poll_sec": 0.05, "timeout_sec": 60}  # fmt: skip
    first = run_chain(engine, tmp_path, test_settings, **kw)  # type: ignore[arg-type]
    assert [(r.stage, r.status) for r in first] == [("ingest", "done"), ("script", "done")]
    assert _runs(chain_env) == ["ingest 2026-10-06:1", "script 2026-10-06:1"]

    second = run_chain(engine, tmp_path, test_settings, **kw)  # type: ignore[arg-type]
    assert all(r.status == "done" for r in second)
    assert len(_runs(chain_env)) == 2  # nothing re-ran

    forced = run_chain(engine, tmp_path, test_settings, force=True, **kw)  # type: ignore[arg-type]
    assert all(r.status == "done" for r in forced)
    assert len(_runs(chain_env)) == 4


def test_stage_failure_stops_chain_and_keeps_earlier_work(
    chain_env: Path, tmp_db_url: str, test_settings: Settings
) -> None:
    engine = create_db_engine(tmp_db_url)
    tmp_path = chain_env
    handlers = {
        JobKind.ingest: Handler("tests.pipeline_handlers", "ok_ingest"),
        JobKind.script: Handler("tests.pipeline_handlers", "failing_script"),
    }
    results = run_chain(
        engine, tmp_path, test_settings, local_date=DAY, device_id=1,
        stages=[JobKind.ingest, JobKind.script], handlers=handlers, poll_sec=0.05,
        timeout_sec=60,
    )  # fmt: skip
    assert [(r.stage, r.status) for r in results] == [("ingest", "done"), ("script", "failed")]
    assert _runs(chain_env) == ["ingest 2026-10-06:1"]  # earlier stage's work stands


# --- admin endpoint ----------------------------------------------------------------------------


def test_admin_run_enqueues_idempotently(test_settings: Settings, tmp_db_url: str) -> None:
    token, _device = insert_device_for_test(test_settings)
    with _ClientCtx(test_settings) as client:
        body = {"local_date": "2026-10-06"}
        r1 = client.post("/v1/admin/run", headers=auth_headers(token), json=body)
        r2 = client.post("/v1/admin/run", headers=auth_headers(token), json=body)
        assert r1.status_code == 202, r1.text
        assert r1.json()["job_id"] == r2.json()["job_id"]
        assert r1.json()["stages"] == ["ingest", "script"]
        forced = client.post(
            "/v1/admin/run", headers=auth_headers(token), json={**body, "force": True}
        )
        assert forced.json()["job_id"] != r1.json()["job_id"]
        bad = client.post("/v1/admin/run", headers=auth_headers(token), json={"stages": ["render"]})
        assert bad.status_code == 422
        assert client.post("/v1/admin/run", json=body).status_code == 401
    with Session(create_db_engine(tmp_db_url)) as session:
        assert len(session.exec(select(Job)).all()) == 2


class _ClientCtx:
    def __init__(self, settings: Settings) -> None:
        from fastapi.testclient import TestClient

        self.client = TestClient(create_app(settings))

    def __enter__(self):
        return self.client.__enter__()

    def __exit__(self, *exc: object) -> None:
        self.client.__exit__(*exc)


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_run_reports_stage_results(
    test_settings: Settings, tmp_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_device_for_test(test_settings)
    monkeypatch.setattr("chronos.config.get_settings", lambda: test_settings)
    from chronos.wake.rules import reset_rules_cache

    reset_rules_cache()
    seen: dict[str, object] = {}

    def fake_run_chain(
        engine: object, data_root: object, settings: object, **kw: object
    ) -> list[pipeline.StageResult]:
        seen.update(kw)
        return [pipeline.StageResult("ingest", "done", 1, 1200, None),
                pipeline.StageResult("script", "failed", 1, None, "handler_failed")]  # fmt: skip

    monkeypatch.setattr(pipeline, "run_chain", fake_run_chain)
    result = CliRunner().invoke(
        cli_app, ["run", "--date", "2026-10-06", "--stages", "script,ingest,render", "--force"]
    )
    assert result.exit_code == 1, result.output
    assert "not implemented yet, skipped: render" in result.output
    assert "✓ ingest" in result.output and "✗ script" in result.output
    assert seen["local_date"] == DAY and seen["force"] is True
    assert seen["stages"] == [JobKind.ingest, JobKind.script]


def test_cli_run_rejects_unknown_stage() -> None:
    result = CliRunner().invoke(cli_app, ["run", "--stages", "ingest,bogus"])
    assert result.exit_code == 2


def test_settings_snapshot_round_trip_reveals_secrets(test_settings: Settings) -> None:
    from chronos.config import settings_from_snapshot, settings_snapshot

    text = settings_snapshot(test_settings)
    assert "test-secret-key-32-bytes-exactly!" in text  # handed to the job, 0600, deleted on read
    again = settings_from_snapshot(text)
    assert again.database_url == test_settings.database_url
    assert again.secret_key.get_secret_value() == test_settings.secret_key.get_secret_value()


def test_worker_writes_private_snapshot_and_handler_deletes_it(
    test_settings: Settings, tmp_path: Path
) -> None:
    import json
    import stat

    from chronos.config import settings_snapshot
    from chronos.jobs.worker import JobContext, write_private
    from chronos.pipeline import _deps  # pyright: ignore[reportPrivateUsage]

    snapshot = tmp_path / "settings.json"
    write_private(snapshot, settings_snapshot(test_settings))
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_private(snapshot, "{}")  # never overwrites / follows an existing file
    ctx = JobContext(job_id=1, owner="o", payload_json=json.dumps({}), data_root=tmp_path,
                     work_dir=tmp_path)  # fmt: skip
    deps = _deps(ctx)
    assert deps.settings.database_url == test_settings.database_url
    assert not snapshot.exists()
    deps.engine.dispose()


# --- scheduler prewarm (fake clock) -------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("render_mode", ["eager", "on_trigger"])
async def test_scheduler_enqueues_prewarm_at_window_start_minus_30(
    test_settings: Settings, render_mode: str
) -> None:
    """AC: the scheduler enqueues prewarm at window start - 30 min (fake clock)."""
    from chronos.delivery.push import FakePushClient
    from chronos.scheduler import Scheduler
    from chronos.wake.rules import ensure_initial_version, get_rules_cache, reset_rules_cache
    from tests.conftest import make_settings
    from tests.factories import make_device

    settings = make_settings(database_url=test_settings.database_url, render_mode=render_mode)
    engine = create_db_engine(settings.database_url)

    def factory() -> Session:
        return Session(engine)

    reset_rules_cache()
    with factory() as session:
        ensure_initial_version(session, Path(__file__).parents[1] / "config" / "wake_rules.yaml")
        session.commit()
        get_rules_cache().get_effective(session)
        make_device(session, tz="America/Los_Angeles", expo_push_token=None, status="active")
        session.commit()
    scheduler = Scheduler(session_factory=factory, push_client=FakePushClient(), settings=settings)

    def jobs() -> list[Job]:
        with factory() as session:
            return list(session.exec(select(Job)).all())

    # Tue 2026-10-06, window 06:00 PDT = 13:00 UTC; prewarm from 05:30 PDT = 12:30 UTC.
    await scheduler.run_due(datetime.datetime(2026, 10, 6, 12, 29, tzinfo=datetime.UTC))
    assert jobs() == []
    await scheduler.run_due(datetime.datetime(2026, 10, 6, 12, 30, tzinfo=datetime.UTC))
    (job,) = jobs()
    assert job.kind == "ingest" and job.key == "ingest:2026-10-06:1"
    payload = StagePayload.model_validate_json(job.payload_json)
    every = [s.value for s in implemented(STAGE_ORDER)]
    expected = every if render_mode == "eager" else ["ingest"]
    assert [s.value for s in payload.stages] == expected
    await scheduler.run_due(datetime.datetime(2026, 10, 6, 12, 31, tzinfo=datetime.UTC))
    assert len(jobs()) == 1  # idempotent; no push token needed for prewarm
