"""The ``script`` job: selected stories -> validated ReelScript -> ``briefs`` + ``brief_items``.

Payload ``{"local_date": "YYYY-MM-DD", "device_id": int, "force": bool}`` (owner decision 8).
The ``days`` row must already exist (T3.5's prewarm creates it with the device timezone).

- Idempotent: a ``ready`` brief for the day is kept unless ``force`` is set.
- 0 candidates -> brief ``failed`` (T5.2 sends the text fallback push).
- Otherwise the provider chain or the template always yields a ``ready`` brief; provenance and
  ``brief_items`` (``primary`` per story scene, ``mention`` per duplicate) are written in one
  transaction.

The worker calls :func:`run` in a supervised subprocess; :func:`run_script_job` is the testable
core with injected session, config, clock and providers.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence

import structlog
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, col, delete, select

from chronos.config import get_settings
from chronos.content.rank import SelectedStory
from chronos.content.script.schema import MAX_SCENES, ScriptCandidate
from chronos.content.script.service import ProviderPlan, build_plans, generate_script
from chronos.content.selection import select_stories
from chronos.content.sources import SourcesConfig, load_sources_yaml
from chronos.db import create_db_engine
from chronos.enums import BriefItemRole, BriefStatus
from chronos.jobs.worker import JobContext
from chronos.models.content import Brief, BriefItem, Item
from chronos.models.wake import Day
from chronos.time import utc_now_iso

log = structlog.get_logger(__name__)

MAX_URL_CHARS = 2048
SOURCE_NAMES: dict[str, str] = {
    "hf_daily_papers": "Hugging Face Papers",
    "arxiv_cs_ai_cl_lg": "arXiv",
    "hn_front": "Hacker News",
    "bbc_world": "BBC News",
    "cbs_top": "CBS News",
    "ie_india": "Indian Express",
    "ie_tech": "Indian Express",
    "ndtv_top": "NDTV",
}


class ScriptJobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_date: datetime.date
    device_id: int
    force: bool = False


class DayMissingError(LookupError):
    """The ``days`` row for (local_date, device_id) does not exist."""


def to_candidates(session: Session, stories: Sequence[SelectedStory]) -> list[ScriptCandidate]:
    """Attach summaries and display names; drop over-long URLs; cap at the scene limit."""
    out: list[ScriptCandidate] = []
    for story in stories:
        if len(story.url) > MAX_URL_CHARS:
            log.warning("script_candidate_url_too_long", item_id=story.item_id)
            continue
        item = session.get(Item, story.item_id)
        out.append(
            ScriptCandidate(
                item_id=story.item_id,
                category=story.category,
                source_key=story.source_key,
                source_name=SOURCE_NAMES.get(story.source_key, story.source_key),
                url=story.url,
                title=story.title,
                summary=item.summary if item is not None else None,
            )
        )
    return out[: MAX_SCENES - 2]


def _upsert_brief(session: Session, payload: ScriptJobPayload, now: str) -> Brief:
    day = payload.local_date.isoformat()
    brief = session.exec(
        select(Brief).where(Brief.local_date == day, Brief.device_id == payload.device_id)
    ).first()
    if brief is None:
        brief = Brief(local_date=day, device_id=payload.device_id, created_at=now, updated_at=now)
        session.add(brief)
        session.flush()
    return brief


def run_script_job(
    session: Session,
    payload: ScriptJobPayload,
    sources: SourcesConfig,
    now: str,
    plans: Sequence[ProviderPlan],
    *,
    expected_digest: str = "",
) -> Brief:
    """Produce (or keep) the day's brief; commits. See the module docstring."""
    day_key = payload.local_date.isoformat()
    day = session.get(Day, (day_key, payload.device_id))
    if day is None:
        raise DayMissingError(f"no day row for {day_key} / device {payload.device_id}")

    existing = session.exec(
        select(Brief).where(Brief.local_date == day_key, Brief.device_id == payload.device_id)
    ).first()
    if existing is not None and existing.status == BriefStatus.ready.value and not payload.force:
        log.info("script_skipped_ready", local_date=day_key, brief_id=existing.id)
        return existing

    stories = select_stories(session, sources.selection, now)
    candidates = to_candidates(session, stories)
    brief = _upsert_brief(session, payload, now)
    assert brief.id is not None
    day.brief_id = brief.id
    session.add(day)
    session.exec(delete(BriefItem).where(col(BriefItem.brief_id) == brief.id))

    if not candidates:
        brief.status = BriefStatus.failed.value
        brief.script_json = "{}"
        brief.updated_at = now
        session.add(brief)
        session.commit()
        log.warning("script_no_candidates", local_date=day_key)
        return brief

    script, prov = generate_script(
        candidates, payload.local_date, plans, expected_digest=expected_digest
    )
    brief.status = BriefStatus.ready.value
    brief.script_json = script.model_dump_json()
    brief.llm_provider = prov.provider
    brief.llm_model = prov.model
    brief.model_digest = prov.digest
    brief.prompt_version = prov.prompt_version
    brief.attempts = prov.attempts
    brief.used_fallback_template = int(prov.used_fallback_template)
    brief.tokens_in = prov.tokens_in
    brief.tokens_out = prov.tokens_out
    brief.latency_ms = prov.latency_ms
    brief.updated_at = now
    session.add(brief)

    by_item = {s.item_id: s for s in stories}
    for scene in script.stories:
        assert scene.item_id is not None
        session.add(
            BriefItem(
                brief_id=brief.id, item_id=scene.item_id, scene_index=scene.index,
                role=BriefItemRole.primary.value,
            )
        )  # fmt: skip
        for mention in by_item[scene.item_id].mention_item_ids:
            session.add(
                BriefItem(
                    brief_id=brief.id, item_id=mention, scene_index=scene.index,
                    role=BriefItemRole.mention.value,
                )
            )  # fmt: skip
    session.commit()
    session.refresh(brief)
    return brief


def run(ctx: JobContext) -> None:
    """Worker entry point (registered by T3.5)."""
    payload = ScriptJobPayload.model_validate(json.loads(ctx.payload_json))
    settings = get_settings()
    sources = load_sources_yaml(settings.sources_path)
    engine = create_db_engine(settings.database_url)
    try:
        with Session(engine) as session:
            run_script_job(
                session,
                payload,
                sources,
                utc_now_iso(),
                build_plans(settings),
                expected_digest=settings.ollama_digest,
            )
    finally:
        engine.dispose()
