"""Content selection orchestrator (T3.3): persist → candidates → cluster → select.

T3.5's ``ingest`` job calls :func:`persist_outcomes` after ``fetch_all`` and
:func:`select_stories` when building the day's candidate list; T3.4 consumes the result.

The candidate pool is a DB window (owner decision 3): items of *enabled* sources whose
``fetched_at`` or ``published_at`` falls within :data:`CANDIDATE_WINDOW` of ``now``. A source
that answered 304 Not Modified this morning therefore still contributes yesterday's items.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable

from sqlmodel import Session, col, or_, select

from chronos.content.dedupe import Candidate, cluster
from chronos.content.ingest import SourceFetchOutcome
from chronos.content.normalise import NormalisedItem, content_tokens, normalise
from chronos.content.rank import SelectedStory, popularity
from chronos.content.rank import select as select_clusters
from chronos.content.sources import SelectionConfig
from chronos.content.store import upsert_items
from chronos.models.content import Item, Source
from chronos.time import format_utc_iso, parse_utc_iso

CANDIDATE_WINDOW = datetime.timedelta(hours=36)


def persist_outcomes(
    session: Session, outcomes: Iterable[SourceFetchOutcome], now: str
) -> dict[str, int]:
    """Normalise and upsert the items of every successful outcome.

    Commits once per source (short transactions). Returns ``{source_key: rows_written}``.
    Outcomes for unknown source keys are ignored; unusable items are skipped by
    :func:`~chronos.content.normalise.normalise`.
    """
    sources = {s.key: s for s in session.exec(select(Source)).all()}
    written: dict[str, int] = {}
    for outcome in outcomes:
        source = sources.get(outcome.source_key)
        if outcome.status != "ok" or source is None:
            continue
        normalised: list[NormalisedItem] = [
            n for n in (normalise(item, now) for item in outcome.items) if n is not None
        ]
        written[outcome.source_key] = upsert_items(session, source, normalised)
        session.commit()
    return written


def load_candidates(session: Session, now: datetime.datetime) -> list[Candidate]:
    """Items of enabled sources seen or published within the candidate window."""
    cutoff = format_utc_iso(now - CANDIDATE_WINDOW)
    rows = session.exec(
        select(Item, Source)
        .join(Source, col(Item.source_id) == col(Source.id))
        .where(Source.enabled == 1)
        .where(or_(col(Item.fetched_at) >= cutoff, col(Item.published_at) >= cutoff))
        .order_by(col(Item.id))
    ).all()
    candidates: list[Candidate] = []
    for item, source in rows:
        assert item.id is not None
        candidates.append(
            Candidate(
                item_id=item.id,
                source_key=source.key,
                source_kind=source.kind,
                source_weight=source.weight,
                category=source.category,
                url=item.url,
                title=item.title,
                url_hash=item.url_hash,
                tokens=content_tokens(item.title),
                published_at=parse_utc_iso(item.published_at or item.fetched_at),
                popularity=popularity(source.kind, item.extra_json),
            )
        )
    return candidates


def select_stories(session: Session, cfg: SelectionConfig, now: str) -> list[SelectedStory]:
    """The day's ordered story list (narrative order), with duplicates as mentions."""
    instant = parse_utc_iso(now)
    quotas = {s.key: s.quota for s in session.exec(select(Source).where(Source.enabled == 1))}
    return select_clusters(cluster(load_candidates(session, instant)), quotas, cfg, instant)
