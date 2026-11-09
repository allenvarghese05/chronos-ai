"""chronos.content.selection: persist → candidates → cluster → select, incl. golden day."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, cast

from sqlmodel import Session, select

from chronos.config import Settings
from chronos.content.fetchers import ParsedItem
from chronos.content.ingest import SourceFetchOutcome
from chronos.content.rank import SelectedStory
from chronos.content.selection import load_candidates, persist_outcomes, select_stories
from chronos.content.sources import SourcesConfig, load_sources_yaml, sync_sources
from chronos.db import create_db_engine
from chronos.models.content import Item, Source
from chronos.time import parse_utc_iso

API_ROOT = Path(__file__).parents[2]
FIXTURE = API_ROOT / "tests" / "fixtures" / "content" / "day_2026-10-06.json"
EXPECTED = API_ROOT / "tests" / "fixtures" / "content" / "day_2026-10-06.expected.json"
CONFIG: SourcesConfig = load_sources_yaml(API_ROOT / "config" / "sources.yaml")


def _fixture() -> tuple[str, dict[str, list[ParsedItem]]]:
    raw = cast("dict[str, Any]", json.loads(FIXTURE.read_text()))
    items = {
        key: [ParsedItem(**entry) for entry in entries]
        for key, entries in cast("dict[str, list[dict[str, Any]]]", raw["items"]).items()
    }
    return cast("str", raw["now"]), items


def _outcomes(
    items: dict[str, list[ParsedItem]], rnd: random.Random | None = None
) -> list[SourceFetchOutcome]:
    out = [SourceFetchOutcome(key, "ok", list(v)) for key, v in items.items()]
    if rnd is not None:
        rnd.shuffle(out)
        for o in out:
            rnd.shuffle(o.items)
    return out


def _describe(session: Session, stories: list[SelectedStory]) -> list[dict[str, Any]]:
    rows = {i.id: i for i in session.exec(select(Item)).all()}
    keys = {s.id: s.key for s in session.exec(select(Source)).all()}

    def ref(item_id: int) -> list[str]:
        item = rows[item_id]
        return [keys[item.source_id], item.external_id]

    return [
        {
            "source_key": s.source_key,
            "external_id": rows[s.item_id].external_id,
            "category": s.category,
            "mentions": sorted(ref(m) for m in s.mention_item_ids),
        }
        for s in stories
    ]


def _run(tmp_db_url: str, rnd: random.Random | None = None) -> list[dict[str, Any]]:
    now, items = _fixture()
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        sync_sources(session, CONFIG, now)
        session.commit()
        persist_outcomes(session, _outcomes(items, rnd), now)
        return _describe(session, select_stories(session, CONFIG.selection, now))


def test_golden_fixture_day(test_settings: Settings, tmp_db_url: str) -> None:
    """Golden selection for 2026-10-06 06:00Z with the real config/sources.yaml.

    Hand-computed (recency r(h) = 1 - 0.035h, freshness 20 h; boost 1.25 for 2 sources):

    - ai: hf-1 = 1.3 * r(2) * 1.6 * 1.25 (arXiv ax-3 is the same paper -> mention) = 2.418;
      hf-2 = 1.3 * r(4) * 1.361 = 1.522; hf-3 = 1.220 (hf quota 2 already used);
      2610.00001 = 0.9 * r(10) = 0.585; 2610.00002 is 30 h old (stale, ai not empty).
    - tech: 41000001 = r(3) * 1.6 = 1.432; 41000002 = r(5) * 1.461 = 1.205 (hn quota 1);
      iet-1 = 0.7 * r(1) = 0.676.
    - world: bbc-1 + cbs-1 (same token set) = r(1) * 1.25 = 1.206; bbc-2 + cbs-3 (same URL
      once www/utm are folded) = r(2) * 1.25 = 1.163; bbc-3 / cbs-2 "Live updates" never merge
      (< 4 content tokens); bbc-4 = 0.72.
    - india: iei-1 = r(3) = 0.895; nd-1 (AMP URL) = 0.9 * r(2) = 0.837; iei-2 = 0.755.

    Phase 1 (minimums ai 2, world 1, india 1): hf-1, hf-2, bbc-1, iei-1.
    Phase 2 by score: 41000001 (1.432); hf-3 skipped (quota), 41000002 skipped (quota);
    bbc-2 (1.163, bbc quota 2). Narrative order, best first within a category.
    """
    expected = json.loads(EXPECTED.read_text())
    assert _run(tmp_db_url) == expected


def test_golden_day_independent_of_input_order(test_settings: Settings, tmp_db_url: str) -> None:
    expected = json.loads(EXPECTED.read_text())
    assert _run(tmp_db_url, random.Random(7)) == expected  # noqa: S311 - seeded test shuffle


def test_persist_is_idempotent(test_settings: Settings, tmp_db_url: str) -> None:
    now, items = _fixture()
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        sync_sources(session, CONFIG, now)
        session.commit()
        first = persist_outcomes(session, _outcomes(items), now)
        persist_outcomes(session, _outcomes(items), now)
        total = sum(len(v) for v in items.values())
        assert sum(first.values()) == total
        assert len(session.exec(select(Item)).all()) == total


def test_304_source_still_contributes_from_db_window(
    test_settings: Settings, tmp_db_url: str
) -> None:
    """Owner decision 3: a not-modified source keeps yesterday's items in the pool."""
    now, items = _fixture()
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        sync_sources(session, CONFIG, now)
        session.commit()
        persist_outcomes(session, _outcomes(items), now)
        later = "2026-10-06T08:00:00.000000Z"
        rerun = [SourceFetchOutcome("bbc_world", "not_modified", [])]
        persist_outcomes(session, rerun, later)
        keys = {s.source_key for s in select_stories(session, CONFIG.selection, later)}
        assert "bbc_world" in keys


def test_disabled_source_and_old_items_excluded(test_settings: Settings, tmp_db_url: str) -> None:
    now, items = _fixture()
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        sync_sources(session, CONFIG, now)
        session.commit()
        persist_outcomes(session, _outcomes(items), now)
        bbc = session.exec(select(Source).where(Source.key == "bbc_world")).one()
        bbc.enabled = 0
        session.add(bbc)
        session.commit()
        cands = load_candidates(session, parse_utc_iso(now))
        assert all(c.source_key != "bbc_world" for c in cands)
        # three days later nothing is inside the 36 h window any more
        assert load_candidates(session, parse_utc_iso("2026-10-09T06:00:00.000000Z")) == []


def test_unknown_and_failed_outcomes_ignored(test_settings: Settings, tmp_db_url: str) -> None:
    now, items = _fixture()
    engine = create_db_engine(tmp_db_url)
    with Session(engine) as session:
        sync_sources(session, CONFIG, now)
        session.commit()
        written = persist_outcomes(
            session,
            [
                SourceFetchOutcome("nope", "ok", items["bbc_world"]),
                SourceFetchOutcome("bbc_world", "error", [], error="boom"),
            ],
            now,
        )
        assert written == {}
        assert session.exec(select(Item)).all() == []
