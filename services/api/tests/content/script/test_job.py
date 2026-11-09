"""The script job end to end on a temp DB with the T3.3 golden fixture day."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.content.fetchers import ParsedItem
from chronos.content.ingest import SourceFetchOutcome
from chronos.content.script import job as job_module
from chronos.content.script.job import (
    DayMissingError,
    ScriptJobPayload,
    run,
    run_script_job,
    to_candidates,
)
from chronos.content.script.llm import FakeScriptGenerator, ProviderUnavailable
from chronos.content.script.prompt import prompt_version
from chronos.content.script.schema import ReelScript, ScriptCandidate
from chronos.content.script.service import ProviderPlan
from chronos.content.selection import persist_outcomes, select_stories
from chronos.content.sources import load_sources_yaml, sync_sources
from chronos.db import create_db_engine
from chronos.jobs.worker import JobContext
from chronos.models.content import Brief, BriefItem
from chronos.models.wake import Day
from tests.conftest import make_settings
from tests.content.script.helpers import frame_reply, story_reply
from tests.content.test_selection import _fixture, _outcomes  # pyright: ignore[reportPrivateUsage]
from tests.factories import make_day, make_device

API_ROOT = Path(__file__).parents[3]
SOURCES = load_sources_yaml(API_ROOT / "config" / "sources.yaml")
DAY_STR = "2026-10-06"
PAYLOAD = ScriptJobPayload.model_validate({"local_date": DAY_STR, "device_id": 1})


def seed_day(session: Session, *, with_items: bool = True) -> tuple[str, int]:
    now, items = _fixture()
    sync_sources(session, SOURCES, now)
    device = make_device(session)
    make_day(session, device, local_date=DAY_STR)
    session.commit()
    if with_items:
        persist_outcomes(session, _outcomes(items), now)
    assert device.id is not None
    return now, device.id


def candidates_for(session: Session, now: str) -> list[ScriptCandidate]:
    return to_candidates(session, select_stories(session, SOURCES.selection, now))


def _plan(*replies: str | Exception) -> ProviderPlan:
    return ProviderPlan(FakeScriptGenerator(list(replies)), attempts=3, timeout_sec=60)


def _payload(device_id: int, **extra: Any) -> ScriptJobPayload:
    return ScriptJobPayload.model_validate({"local_date": DAY_STR, "device_id": device_id, **extra})


def test_fake_generator_end_to_end(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        now, device_id = seed_day(session)
        cands = candidates_for(session, now)
        assert [c.source_name for c in cands][:2] == ["Hugging Face Papers"] * 2
        replies = [frame_reply(), *[story_reply(20, c.title[:60]) for c in cands]]

        brief = run_script_job(session, _payload(device_id), SOURCES, now, [_plan(*replies)])

        assert brief.status == "ready"
        assert (brief.llm_provider, brief.llm_model, brief.attempts) == ("fake", "fake-model", 7)
        assert brief.used_fallback_template == 0
        assert brief.prompt_version == prompt_version()  # provenance stored
        script = ReelScript.model_validate_json(brief.script_json)
        assert [s.item_id for s in script.stories] == [c.item_id for c in cands]

        rows = session.exec(select(BriefItem).where(BriefItem.brief_id == brief.id)).all()
        assert sum(r.role == "primary" for r in rows) == 6
        assert sum(r.role == "mention" for r in rows) == 3  # ax-3, cbs-1, cbs-3 (T3.3 golden)
        day = session.get(Day, (DAY_STR, device_id))
        assert day is not None and day.brief_id == brief.id


def test_ready_brief_is_kept_unless_forced(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        now, device_id = seed_day(session)
        first = run_script_job(session, _payload(device_id), SOURCES, now, [])  # template
        assert first.used_fallback_template == 1 and first.status == "ready"

        untouched = _plan()  # would raise if called
        again = run_script_job(session, _payload(device_id), SOURCES, now, [untouched])
        assert again.id == first.id
        assert isinstance(untouched.generator, FakeScriptGenerator)
        assert untouched.generator.calls == []

        forced = run_script_job(
            session, _payload(device_id, force=True), SOURCES, now,
            [_plan(ProviderUnavailable("down"))],
        )  # fmt: skip
        assert forced.id == first.id and forced.attempts == 1
        rows = session.exec(select(BriefItem).where(BriefItem.brief_id == forced.id)).all()
        assert len(rows) == 9  # replaced, not duplicated
        assert len(session.exec(select(Brief)).all()) == 1


def test_no_candidates_marks_brief_failed(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        now, device_id = seed_day(session, with_items=False)
        brief = run_script_job(session, _payload(device_id), SOURCES, now, [_plan()])
        assert brief.status == "failed" and brief.script_json == "{}"


def test_missing_day_row_fails_clearly(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session, pytest.raises(DayMissingError):
        run_script_job(session, PAYLOAD, SOURCES, "2026-10-06T06:00:00.000000Z", [])


def test_long_urls_dropped_from_candidates(test_settings: Settings, tmp_db_url: str) -> None:
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        now, _ = seed_day(session, with_items=False)
        long_story = ParsedItem(
            external_id="long",
            url="https://bbc.co.uk/" + "a" * 2100,
            title="A very long url story about something",
            published_at=now,
        )
        persist_outcomes(session, [SourceFetchOutcome("bbc_world", "ok", [long_story])], now)
        assert select_stories(session, SOURCES.selection, now)  # it was selected...
        assert candidates_for(session, now) == []  # ...but never offered to the script writer


def test_worker_entry_point_uses_settings(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run(ctx): Ollama unreachable -> template; proves the subprocess wiring end to end."""
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        now, device_id = seed_day(session)
    settings = make_settings(
        database_url=tmp_db_url, ollama_endpoint="http://127.0.0.1:9", ollama_timeout_sec=2.0
    )
    monkeypatch.setattr(job_module, "get_settings", lambda: settings)
    monkeypatch.setattr(job_module, "utc_now_iso", lambda: now)
    body = json.dumps({"local_date": DAY_STR, "device_id": device_id})
    ctx = JobContext(job_id=1, owner="o", payload_json=body, data_root=tmp_path, work_dir=tmp_path)
    run(ctx)
    with Session(engine) as session:
        brief = session.exec(select(Brief)).one()
        assert brief.status == "ready" and brief.used_fallback_template == 1
        assert brief.llm_provider == "template"
