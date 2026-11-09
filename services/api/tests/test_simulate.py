"""T5.5 simulate-morning: the whole morning on a fake clock (fast, with fake stage handlers)."""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config

from chronos.enums import JobKind
from chronos.jobs.worker import Handler
from chronos.simulate import render_report, simulate_morning
from chronos.wake.rules import reset_rules_cache
from tests.conftest import make_settings

FAKE = {
    kind: Handler("tests.pipeline_handlers", f"ok_{kind.value}")
    for kind in (JobKind.ingest, JobKind.script, JobKind.assets, JobKind.tts, JobKind.captions,
                 JobKind.render)
}  # fmt: skip


def test_simulated_morning_confirms_and_delivers(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'sim.db'}"
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    settings = make_settings(database_url=url, data_root=str(tmp_path / "data"))
    reset_rules_cache()
    report = asyncio.run(
        simulate_morning(
            settings,
            local_date=datetime.date(2026, 10, 6),
            wake_local=datetime.time(6, 40),
            tz="Asia/Kolkata",
            handlers=FAKE,
            pipeline_timeout_sec=120,
        )
    )
    checks = report.checks()
    print("\n" + render_report(report))
    assert checks["prewarm before window"]
    assert checks["pipeline completed"]
    assert [k for k, _ in report.pipeline] == ["ingest", "script", "assets", "tts", "captions",
                                               "render"]  # fmt: skip
    assert checks[">= 1 tick per 5 min"] and len(report.ticks) >= 8
    assert checks["confirmed within 10 min of wake"]
    assert checks["push delivered"]
    assert report.delivery is not None and report.delivery["type"] == "text_fallback"  # fake stages
    assert not checks["reel ready"]  # fake stages render nothing
