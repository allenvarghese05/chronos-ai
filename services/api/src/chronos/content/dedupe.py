"""Cross-source story clustering (T3.3).

Two candidates are linked when they share a URL dedupe hash, or when their titles are near
duplicates (ADR D35) and they were published within :data:`TITLE_WINDOW` of each other.
Clusters are the connected components of that link graph (union-find), so the partition
depends only on the set of candidates, never on their order.

The primary of a cluster is the member with the highest
``(source_weight, popularity, published_at, -item_id)``; the rest are mentions.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from chronos.content.normalise import titles_near_duplicate

TITLE_WINDOW = datetime.timedelta(hours=36)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One stored item, with what clustering and ranking need about it and its source."""

    item_id: int
    source_key: str
    source_kind: str
    source_weight: float
    category: str
    url: str
    title: str
    url_hash: str
    tokens: frozenset[str]
    published_at: datetime.datetime
    popularity: float

    @property
    def primary_key(self) -> tuple[float, float, datetime.datetime, int]:
        return (self.source_weight, self.popularity, self.published_at, -self.item_id)


@dataclass(frozen=True, slots=True)
class Cluster:
    """A story: its primary item and the duplicates (mentions) from any source."""

    primary: Candidate
    mentions: tuple[Candidate, ...]

    @property
    def members(self) -> tuple[Candidate, ...]:
        return (self.primary, *self.mentions)

    @property
    def n_sources(self) -> int:
        return len({m.source_key for m in self.members})


def linked(a: Candidate, b: Candidate) -> bool:
    """True when *a* and *b* are the same story."""
    if a.url_hash == b.url_hash:
        return True
    return abs(a.published_at - b.published_at) <= TITLE_WINDOW and titles_near_duplicate(
        a.tokens, b.tokens
    )


def cluster(candidates: Iterable[Candidate]) -> list[Cluster]:
    """Partition *candidates* into stories; output sorted by primary ``item_id``."""
    items = sorted(candidates, key=lambda c: c.item_id)
    if len({c.item_id for c in items}) != len(items):
        raise ValueError("duplicate item_id in candidates")
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if linked(items[i], items[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[Candidate]] = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)

    clusters: list[Cluster] = []
    for members in groups.values():
        primary = max(members, key=lambda c: c.primary_key)
        mentions = tuple(m for m in members if m.item_id != primary.item_id)
        clusters.append(Cluster(primary=primary, mentions=mentions))
    clusters.sort(key=lambda c: c.primary.item_id)
    return clusters


def dedupe(candidates: Sequence[Candidate]) -> list[Candidate]:
    """The primaries only: one candidate per story."""
    return [c.primary for c in cluster(candidates)]
