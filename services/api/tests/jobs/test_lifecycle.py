"""Worker lifecycle: enable/disable, lifespan integration, independent controls (T3.1 step 4)."""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.enums import JobKind
from chronos.jobs.queue import JobInput, JobQueue
from chronos.jobs.worker import Handler, Worker
from chronos.main import create_app
from tests.conftest import make_settings

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _run_migrations(db_url: str) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).parents[2] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def _make_worker_settings(tmp_path: Path, **overrides: object) -> Settings:
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    _run_migrations(db_url)
    base: dict[str, object] = {
        "database_url": db_url,
        "worker_enabled": True,
        "scheduler_enabled": False,
        "skip_rules_init": True,
    }
    base.update(overrides)
    return make_settings(**base)


# --------------------------------------------------------------------------
# Settings: effective_worker_enabled
# --------------------------------------------------------------------------


class TestEffectiveWorkerEnabled:
    def test_default_off_in_test_env(self) -> None:
        s = make_settings()
        assert s.env.value == "test"
        assert s.effective_worker_enabled is False

    def test_explicit_false_overrides(self) -> None:
        s = make_settings(worker_enabled=False)
        assert s.effective_worker_enabled is False

    def test_explicit_true_overrides(self) -> None:
        s = make_settings(worker_enabled=True)
        assert s.effective_worker_enabled is True


# --------------------------------------------------------------------------
# Lifespan: worker disabled (default test env)
# --------------------------------------------------------------------------


class TestLifespanWorkerDisabled:
    def test_app_starts_without_worker(self, test_settings: Settings) -> None:
        assert test_settings.effective_worker_enabled is False
        app = create_app(settings=test_settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200
            assert not hasattr(app.state, "worker")

    def test_health_returns_200_with_worker_disabled(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200


# --------------------------------------------------------------------------
# Lifespan: worker enabled with empty registry
# --------------------------------------------------------------------------


class TestLifespanWorkerEnabled:
    def test_worker_starts_and_stops(self, tmp_path: Path) -> None:
        settings = _make_worker_settings(tmp_path)
        app = create_app(settings=settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert hasattr(app.state, "worker")
            resp = client.get("/healthz")
            assert resp.status_code == 200

    def test_api_responsive_while_worker_polling(self, tmp_path: Path) -> None:
        """API requests are not blocked by the idle polling worker."""
        settings = _make_worker_settings(tmp_path)
        app = create_app(settings=settings)
        with TestClient(app, raise_server_exceptions=False) as client:
            for _ in range(5):
                resp = client.get("/healthz")
                assert resp.status_code == 200

    def test_no_default_handlers_registered(self, tmp_path: Path) -> None:
        settings = _make_worker_settings(tmp_path)
        app = create_app(settings=settings)
        with TestClient(app, raise_server_exceptions=False):
            worker: Worker = app.state.worker
            assert worker.handlers == {}

    def test_no_automatic_enqueue_on_startup(self, tmp_path: Path) -> None:
        settings = _make_worker_settings(tmp_path)
        app = create_app(settings=settings)
        with TestClient(app, raise_server_exceptions=False):
            engine = create_db_engine(settings.database_url)
            queue = JobQueue(engine)
            claim = queue.claim(set(JobKind), dt.datetime.now(dt.UTC))
            assert claim is None
            engine.dispose()


# --------------------------------------------------------------------------
# Worker.run_once: correct run_once behavior through worker directly
# --------------------------------------------------------------------------


class TestWorkerRunOnce:
    def test_run_once_false_when_no_handlers(self, test_settings: Settings, tmp_path: Path) -> None:
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        queue.enqueue(JobInput(kind=JobKind.ingest, key="noop"), dt.datetime.now(dt.UTC))
        worker = Worker(queue, tmp_path, {})
        assert not worker.run_once(threading.Event())
        engine.dispose()

    def test_run_once_false_when_stop_set(self, test_settings: Settings, tmp_path: Path) -> None:
        engine = create_db_engine(test_settings.database_url)
        queue = JobQueue(engine)
        queue.enqueue(JobInput(kind=JobKind.ingest, key="stoptest"), dt.datetime.now(dt.UTC))
        worker = Worker(
            queue, tmp_path, {JobKind.ingest: Handler("tests.jobs.handlers", "success")}
        )
        stop = threading.Event()
        stop.set()
        assert not worker.run_once(stop)
        engine.dispose()
