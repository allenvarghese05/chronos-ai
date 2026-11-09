"""chronos.content.dedupe: linking rules, primaries and order-independence properties."""

from __future__ import annotations

import datetime
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chronos.content.dedupe import Candidate, Cluster, cluster, dedupe, linked
from chronos.content.normalise import content_tokens

T0 = datetime.datetime(2026, 10, 6, 6, 0, tzinfo=datetime.UTC)


def cand(
    item_id: int,
    *,
    source: str = "bbc_world",
    weight: float = 1.0,
    title: str = "Some distinct headline number one here",
    url_hash: str | None = None,
    hours_ago: float = 1.0,
    popularity: float = 1.0,
    category: str = "world",
) -> Candidate:
    return Candidate(
        item_id=item_id,
        source_key=source,
        source_kind="rss",
        source_weight=weight,
        category=category,
        url=f"https://x.example/{item_id}",
        title=title,
        url_hash=url_hash or f"h{item_id}",
        tokens=content_tokens(title),
        published_at=T0 - datetime.timedelta(hours=hours_ago),
        popularity=popularity,
    )


def _partition(clusters: list[Cluster]) -> set[frozenset[int]]:
    return {frozenset(m.item_id for m in c.members) for c in clusters}


def test_same_url_hash_links_regardless_of_title_and_time() -> None:
    # brief edge case 5
    a = cand(1, title="Live", url_hash="same")
    b = cand(2, title="Totally different words", url_hash="same", hours_ago=100)
    assert linked(a, b)


def test_near_title_within_window_links() -> None:
    # edge case 6
    a = cand(1, title="Global Climate Summit Opens With Pledge on Methane Cuts")
    b = cand(2, title="Climate summit opens with global pledge to cut methane", source="cbs_top")
    assert linked(a, b)


def test_near_title_outside_window_does_not_link() -> None:
    # edge case 7
    a = cand(1, title="Markets close higher as tech stocks rally strongly", hours_ago=0)
    b = cand(2, title="Markets close higher as tech stocks rally strongly", hours_ago=48)
    assert not linked(a, b)


def test_short_identical_titles_do_not_link() -> None:
    assert not linked(cand(1, title="Live updates"), cand(2, title="Live updates"))


def test_primary_is_highest_weight_then_popularity_then_newest() -> None:
    bbc = cand(1, source="bbc_world", weight=1.0, url_hash="u")
    cbs = cand(2, source="cbs_top", weight=0.8, url_hash="u", hours_ago=0)
    (only,) = cluster([cbs, bbc])
    assert only.primary.item_id == 1
    assert [m.item_id for m in only.mentions] == [2]
    assert only.n_sources == 2


def test_same_source_duplicates_cluster_but_count_one_source() -> None:
    # edge case 8
    a = cand(1, url_hash="u")
    b = cand(2, url_hash="u")
    (only,) = cluster([a, b])
    assert only.n_sources == 1
    assert only.primary.item_id == 1  # full tie: lower item_id wins (-item_id in primary_key)


def test_transitive_links_form_one_cluster() -> None:
    a = cand(1, url_hash="u1")
    b = cand(2, url_hash="u1", title="Earthquake strikes off coast of Japan, tsunami warning")
    c = cand(3, title="Earthquake Strikes Off Japan Coast; Tsunami Warning Issued")
    assert _partition(cluster([a, b, c])) == {frozenset({1, 2, 3})}


def test_duplicate_item_ids_rejected() -> None:
    with pytest.raises(ValueError):
        cluster([cand(1), cand(1)])


def test_empty() -> None:
    assert cluster([]) == []
    assert dedupe([]) == []


# --- properties -------------------------------------------------------------------------------

_TITLES = [
    "Global Climate Summit Opens With Pledge on Methane Cuts",
    "Climate summit opens with global pledge to cut methane",
    "Earthquake strikes off coast of Japan, tsunami warning issued",
    "Earthquake Strikes Off Japan Coast; Tsunami Warning Issued",
    "European Central Bank holds interest rates steady again",
    "Live updates",
    "Parliament passes data protection amendment bill",
]


@st.composite
def candidate_sets(draw: st.DrawFn) -> list[Candidate]:
    n = draw(st.integers(min_value=0, max_value=14))
    return [
        cand(
            i + 1,
            source=draw(st.sampled_from(["bbc_world", "cbs_top", "ndtv_top"])),
            weight=draw(st.sampled_from([0.8, 0.9, 1.0])),
            title=draw(st.sampled_from(_TITLES)),
            url_hash=draw(st.sampled_from(["u1", "u2", "u3", None])) or f"solo{i}",
            hours_ago=draw(st.sampled_from([0.0, 5.0, 30.0, 60.0])),
            popularity=draw(st.sampled_from([1.0, 1.3])),
        )
        for i in range(n)
    ]


@settings(max_examples=150, deadline=None)
@given(candidate_sets(), st.randoms(use_true_random=False))
def test_partition_order_independent(cands: list[Candidate], rnd: random.Random) -> None:
    shuffled = list(cands)
    rnd.shuffle(shuffled)
    a, b = cluster(cands), cluster(shuffled)
    assert _partition(a) == _partition(b)
    assert [c.primary.item_id for c in a] == [c.primary.item_id for c in b]


@settings(max_examples=150, deadline=None)
@given(candidate_sets())
def test_dedupe_idempotent(cands: list[Candidate]) -> None:
    once = dedupe(cands)
    assert dedupe(once) == once


@settings(max_examples=150, deadline=None)
@given(candidate_sets())
def test_partition_covers_every_candidate_once(cands: list[Candidate]) -> None:
    ids = [m.item_id for c in cluster(cands) for m in c.members]
    assert sorted(ids) == sorted(c.item_id for c in cands)
