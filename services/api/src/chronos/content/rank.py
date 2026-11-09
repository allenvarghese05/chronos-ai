"""Story scoring and selection (T3.3, docs/06 §3).

``score = weight * recency * popularity * (1 + cross_source_boost * (n_sources - 1))``, all
taken from the cluster's primary item:

- recency: linear 1.0 at age 0 → 0.3 at ``freshness_hours`` (age clamped at 0 for future
  dates); a cluster older than ``freshness_hours`` is *stale*;
- popularity: ``1 + 0.6 * min(1, log10(1 + x) / log10(1 + cap))`` for HF upvotes (cap 300)
  and HN points (cap 1000); every other source 1.0 (owner decision 2).

Selection: stale clusters are dropped except the single best stale cluster of a category
that has no fresh cluster at all; then category minimums are filled in narrative order,
then remaining slots by score. Both phases honour each source's ``quota`` (counted on the
primary's source) and stop at ``total_stories``. Output is in narrative order (ai, tech,
world, india), best first within a category. Ties break on (score desc, newer first,
lower item id).
"""

from __future__ import annotations

import datetime
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from chronos.content.dedupe import Candidate, Cluster
from chronos.content.sources import SelectionConfig
from chronos.enums import ContentCategory, SourceKind

NARRATIVE_ORDER: tuple[str, ...] = (
    ContentCategory.ai.value,
    ContentCategory.tech.value,
    ContentCategory.world.value,
    ContentCategory.india.value,
)
RECENCY_FLOOR = 0.3
POPULARITY_MAX_BONUS = 0.6
POPULARITY_CAPS: Mapping[str, tuple[str, int]] = {
    SourceKind.hf_api.value: ("upvotes", 300),
    SourceKind.hn_rss.value: ("points", 1000),
}


def recency(age_hours: float, freshness_hours: float) -> float:
    """1.0 at age 0 falling linearly to :data:`RECENCY_FLOOR` at ``freshness_hours``."""
    age = min(max(age_hours, 0.0), freshness_hours)
    return 1.0 - (1.0 - RECENCY_FLOOR) * age / freshness_hours


def popularity(source_kind: str, extra_json: str | None) -> float:
    """Log-scaled popularity in [1.0, 1.6]; 1.0 when unknown or not applicable."""
    spec = POPULARITY_CAPS.get(source_kind)
    if spec is None or not extra_json:
        return 1.0
    field, cap = spec
    try:
        data: object = json.loads(extra_json)
    except ValueError:
        return 1.0
    if not isinstance(data, dict):
        return 1.0
    value = cast("dict[str, object]", data).get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return 1.0
    return 1.0 + POPULARITY_MAX_BONUS * min(1.0, math.log10(1 + value) / math.log10(1 + cap))


def age_hours(candidate: Candidate, now: datetime.datetime) -> float:
    return (now - candidate.published_at).total_seconds() / 3600.0


def score(cluster: Cluster, cfg: SelectionConfig, now: datetime.datetime) -> float:
    p = cluster.primary
    boost = 1.0 + cfg.cross_source_boost * (cluster.n_sources - 1)
    return p.source_weight * recency(age_hours(p, now), cfg.freshness_hours) * p.popularity * boost


@dataclass(frozen=True, slots=True)
class SelectedStory:
    """One chosen story for the reel; T3.4 builds its scene from the primary item."""

    item_id: int
    category: str
    score: float
    source_key: str
    url: str
    title: str
    mention_item_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Scored:
    cluster: Cluster
    score: float
    stale: bool

    @property
    def sort_key(self) -> tuple[float, float, int]:
        p = self.cluster.primary
        return (-self.score, -p.published_at.timestamp(), p.item_id)


def select(
    clusters: Sequence[Cluster],
    quotas: Mapping[str, int],
    cfg: SelectionConfig,
    now: datetime.datetime,
) -> list[SelectedStory]:
    """Choose up to ``cfg.total_stories`` stories; see the module docstring for the rules.

    *quotas* maps source key → max stories from that source; a source missing from the
    map contributes nothing.
    """
    scored = sorted(
        (
            _Scored(c, score(c, cfg, now), age_hours(c.primary, now) > cfg.freshness_hours)
            for c in clusters
        ),
        key=lambda s: s.sort_key,
    )
    pool = [s for s in scored if not s.stale]
    fresh_categories = {s.cluster.primary.category for s in pool}
    for category in NARRATIVE_ORDER:
        if category not in fresh_categories:
            rescue = next(
                (s for s in scored if s.stale and s.cluster.primary.category == category), None
            )
            if rescue is not None:
                pool.append(rescue)
    pool.sort(key=lambda s: s.sort_key)

    chosen: list[_Scored] = []
    chosen_ids: set[int] = set()
    used: dict[str, int] = {}

    def take(candidate: _Scored) -> bool:
        key = candidate.cluster.primary.source_key
        item_id = candidate.cluster.primary.item_id
        if item_id in chosen_ids or used.get(key, 0) >= quotas.get(key, 0):
            return False
        chosen.append(candidate)
        chosen_ids.add(item_id)
        used[key] = used.get(key, 0) + 1
        return True

    total = cfg.total_stories
    for category in NARRATIVE_ORDER:
        need = cfg.category_min.get(category, 0)
        for s in pool:
            if need <= 0 or len(chosen) >= total:
                break
            if s.cluster.primary.category == category and take(s):
                need -= 1
    for s in pool:
        if len(chosen) >= total:
            break
        take(s)

    def narrative(s: _Scored) -> tuple[int, tuple[float, float, int]]:
        category = s.cluster.primary.category
        rank = (
            NARRATIVE_ORDER.index(category) if category in NARRATIVE_ORDER else len(NARRATIVE_ORDER)
        )
        return (rank, s.sort_key)

    return [
        SelectedStory(
            item_id=s.cluster.primary.item_id,
            category=s.cluster.primary.category,
            score=s.score,
            source_key=s.cluster.primary.source_key,
            url=s.cluster.primary.url,
            title=s.cluster.primary.title,
            mention_item_ids=tuple(m.item_id for m in s.cluster.mentions),
        )
        for s in sorted(chosen, key=narrative)
    ]
