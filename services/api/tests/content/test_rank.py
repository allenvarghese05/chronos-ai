"""chronos.content.rank: recency, popularity, score and selection rules + properties."""

from __future__ import annotations

import datetime
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chronos.content.dedupe import Candidate, Cluster
from chronos.content.normalise import content_tokens
from chronos.content.rank import SelectedStory, popularity, recency, score, select
from chronos.content.sources import SelectionConfig

NOW = datetime.datetime(2026, 10, 6, 6, 0, tzinfo=datetime.UTC)
CFG = SelectionConfig(
    total_stories=6,
    category_min={"ai": 2, "world": 1, "india": 1},
    freshness_hours=20,
    cross_source_boost=0.25,
)
QUOTAS = {"ai1": 2, "ai2": 1, "tech1": 1, "world1": 2, "world2": 1, "india1": 1, "india2": 1}


def cand(
    item_id: int,
    source: str,
    category: str,
    *,
    weight: float = 1.0,
    hours_ago: float = 1.0,
    pop: float = 1.0,
) -> Candidate:
    return Candidate(
        item_id=item_id,
        source_key=source,
        source_kind="rss",
        source_weight=weight,
        category=category,
        url=f"https://x.example/{item_id}",
        title=f"Story {item_id}",
        url_hash=f"h{item_id}",
        tokens=content_tokens(f"Story {item_id}"),
        published_at=NOW - datetime.timedelta(hours=hours_ago),
        popularity=pop,
    )


def solo(c: Candidate) -> Cluster:
    return Cluster(primary=c, mentions=())


# --- recency / popularity / score --------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [(0, 1.0), (10, 0.65), (20, 0.3), (40, 0.3), (-3, 1.0)],  # edge case 10: future -> 1.0
)
def test_recency(age: float, expected: float) -> None:
    assert recency(age, 20) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("kind", "extra", "expected"),
    [
        ("hn_rss", '{"points": 1000}', 1.6),
        ("hn_rss", '{"points": 4000}', 1.6),  # edge case 19: capped
        ("hn_rss", '{"points": null}', 1.0),  # edge case 18
        ("hn_rss", '{"points": true}', 1.0),
        ("hn_rss", '{"points": -5}', 1.0),
        ("hn_rss", "not json", 1.0),
        ("hn_rss", "[1, 2]", 1.0),
        ("hn_rss", None, 1.0),
        ("hf_api", '{"upvotes": 300}', 1.6),
        ("arxiv_api", '{"categories": ["cs.AI"]}', 1.0),
        ("rss", '{"points": 999}', 1.0),
    ],
)
def test_popularity_table(kind: str, extra: str | None, expected: float) -> None:
    assert popularity(kind, extra) == pytest.approx(expected)


def test_popularity_log_curve() -> None:
    expected = 1 + 0.6 * math.log10(31) / math.log10(301)
    assert popularity("hf_api", '{"upvotes": 30}') == pytest.approx(expected)


def test_score_formula_with_cross_source_boost() -> None:
    p = cand(1, "world1", "world", weight=1.0, hours_ago=1, pop=1.2)
    m = cand(2, "world2", "world", weight=0.8)
    assert score(Cluster(p, (m,)), CFG, NOW) == pytest.approx(1.0 * 0.965 * 1.2 * 1.25)
    same_source = cand(3, "world1", "world")
    assert score(Cluster(p, (same_source,)), CFG, NOW) == pytest.approx(1.0 * 0.965 * 1.2)


# --- selection ---------------------------------------------------------------------------------


def _ids(stories: list[SelectedStory]) -> list[int]:
    return [s.item_id for s in stories]


def test_minimums_first_then_score_then_narrative_order() -> None:
    clusters = [
        solo(cand(1, "ai1", "ai", hours_ago=1)),
        solo(cand(2, "ai1", "ai", hours_ago=2)),
        solo(cand(3, "tech1", "tech", weight=1.5)),
        solo(cand(4, "world1", "world", hours_ago=3)),
        solo(cand(5, "india1", "india", hours_ago=4)),
        solo(cand(6, "world1", "world", weight=1.4)),
        solo(cand(7, "india2", "india", weight=0.1)),
    ]
    out = select(clusters, QUOTAS, CFG, NOW)
    assert len(out) == 6
    # ai(1,2) tech(3) world(6 scores above 4) india(5); 7 is lowest and left out
    assert _ids(out) == [1, 2, 3, 6, 4, 5]
    assert [s.category for s in out] == ["ai", "ai", "tech", "world", "world", "india"]


def test_quota_binds() -> None:
    # edge case 13: ai1 quota 2, three strong ai1 items
    clusters = [solo(cand(i, "ai1", "ai", weight=2.0, hours_ago=i)) for i in (1, 2, 3)]
    clusters.append(solo(cand(9, "world1", "world")))
    out = select(clusters, QUOTAS, CFG, NOW)
    assert sum(1 for s in out if s.source_key == "ai1") == 2


def test_source_without_quota_contributes_nothing() -> None:
    out = select([solo(cand(1, "unknown", "ai"))], QUOTAS, CFG, NOW)
    assert out == []


def test_category_short_fills_by_score() -> None:
    # edge case 12: only one ai item; remaining slots by score
    clusters = [
        solo(cand(1, "ai2", "ai")),
        solo(cand(2, "world1", "world")),
        solo(cand(3, "world1", "world", hours_ago=2)),
        solo(cand(4, "india1", "india")),
        solo(cand(5, "tech1", "tech")),
        solo(cand(6, "world2", "world")),
        solo(cand(7, "india2", "india")),
    ]
    out = select(clusters, QUOTAS, CFG, NOW)
    assert len(out) == 6
    assert sum(1 for s in out if s.category == "ai") == 1


def test_too_few_items_returns_all() -> None:
    # edge case 14
    clusters = [solo(cand(i, s, c)) for i, s, c in [(1, "ai1", "ai"), (2, "world1", "world")]]
    assert len(select(clusters, QUOTAS, CFG, NOW)) == 2


def test_stale_ignored_unless_category_empty() -> None:
    # edge case 11
    clusters = [
        solo(cand(1, "world1", "world", hours_ago=30)),  # stale, world has a fresh one
        solo(cand(2, "world2", "world", hours_ago=1)),
        solo(cand(3, "india1", "india", hours_ago=30)),  # stale but india otherwise empty
        solo(cand(4, "india2", "india", hours_ago=40)),  # older stale india: not rescued
    ]
    out = select(clusters, QUOTAS, CFG, NOW)
    assert sorted(_ids(out)) == [2, 3]


def test_minimums_exceeding_total_never_exceed_total() -> None:
    # edge case 21
    cfg = SelectionConfig(
        total_stories=3, category_min={"ai": 4, "world": 3}, freshness_hours=20,
        cross_source_boost=0.25,
    )  # fmt: skip
    clusters = [solo(cand(i, "ai1" if i < 3 else "ai2", "ai")) for i in range(1, 4)]
    clusters += [solo(cand(i, "world1", "world")) for i in range(4, 7)]
    out = select(clusters, QUOTAS, cfg, NOW)
    assert len(out) == 3
    assert all(s.category == "ai" for s in out)


def test_unknown_category_min_keys_ignored() -> None:
    cfg = CFG.model_copy(update={"category_min": {"sports": 3, "ai": 1}})
    out = select([solo(cand(1, "ai1", "ai"))], QUOTAS, cfg, NOW)
    assert _ids(out) == [1]


def test_mentions_carried() -> None:
    p = cand(1, "world1", "world")
    m = cand(2, "world2", "world", weight=0.8)
    (story,) = select([Cluster(p, (m,))], QUOTAS, CFG, NOW)
    assert story.mention_item_ids == (2,)
    assert story.url == "https://x.example/1"


def test_tie_break_newer_then_lower_id() -> None:
    cfg = CFG.model_copy(update={"total_stories": 1, "category_min": {}})
    a = solo(cand(5, "world1", "world", hours_ago=2))
    b = solo(cand(3, "world2", "world", hours_ago=2))
    assert _ids(select([a, b], QUOTAS, cfg, NOW)) == [3]


# --- properties --------------------------------------------------------------------------------

_SOURCES = [("ai1", "ai"), ("ai2", "ai"), ("tech1", "tech"), ("world1", "world"),
            ("world2", "world"), ("india1", "india"), ("india2", "india")]  # fmt: skip


@st.composite
def cluster_sets(draw: st.DrawFn) -> list[Cluster]:
    n = draw(st.integers(min_value=0, max_value=20))
    out: list[Cluster] = []
    for i in range(n):
        source, category = draw(st.sampled_from(_SOURCES))
        out.append(
            solo(
                cand(
                    i + 1,
                    source,
                    category,
                    weight=draw(st.sampled_from([0.7, 1.0, 1.3])),
                    hours_ago=draw(st.sampled_from([0.0, 3.0, 12.0, 25.0])),
                    pop=draw(st.sampled_from([1.0, 1.4])),
                )
            )
        )
    return out


@settings(max_examples=200, deadline=None)
@given(cluster_sets())
def test_selection_invariants(clusters: list[Cluster]) -> None:
    out = select(clusters, QUOTAS, CFG, NOW)
    ids = _ids(out)
    assert len(ids) == len(set(ids)) <= CFG.total_stories
    for key, quota in QUOTAS.items():
        assert sum(1 for s in out if s.source_key == key) <= quota
    order = [("ai", "tech", "world", "india").index(s.category) for s in out]
    assert order == sorted(order)


@settings(max_examples=200, deadline=None)
@given(cluster_sets())
def test_select_meets_minimums_property(clusters: list[Cluster]) -> None:
    """With generous quotas, every minimum is met whenever enough fresh supply exists."""
    quotas = dict.fromkeys(QUOTAS, 99)
    out = select(clusters, quotas, CFG, NOW)
    fresh = [c for c in clusters if (NOW - c.primary.published_at).total_seconds() <= 20 * 3600]
    for category, minimum in CFG.category_min.items():
        supply = sum(1 for c in fresh if c.primary.category == category)
        got = sum(1 for s in out if s.category == category)
        assert got >= min(minimum, supply)


@settings(max_examples=200, deadline=None)
@given(cluster_sets())
def test_select_exact_total(clusters: list[Cluster]) -> None:
    """Exactly total_stories whenever the eligible pool (under quotas) is large enough."""
    quotas = dict.fromkeys(QUOTAS, 99)
    out = select(clusters, quotas, CFG, NOW)
    fresh = [c for c in clusters if (NOW - c.primary.published_at).total_seconds() <= 20 * 3600]
    fresh_categories = {c.primary.category for c in fresh}
    rescued = {
        c.primary.category
        for c in clusters
        if c not in fresh and c.primary.category not in fresh_categories
    }
    assert len(out) == min(CFG.total_stories, len(fresh) + len(rescued))
