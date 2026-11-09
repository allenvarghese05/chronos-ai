"""chronos.content.normalise: canonical URLs, dedupe keys, simhash, normalise()."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chronos.content.fetchers import ParsedItem
from chronos.content.normalise import (
    SUMMARY_MAX_CHARS,
    canonical_url,
    content_tokens,
    dedupe_key,
    hamming,
    normalise,
    simhash64,
    title_similarity,
    title_tokens,
    titles_near_duplicate,
    url_hash,
)

NOW = "2026-10-06T06:00:00.000000Z"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # brief edge case 1: tracking params, case, fragment, trailing slash
        (
            "https://BBC.co.uk/news/x/?utm_source=rss&fbclid=1&ref=hp#top",
            "https://bbc.co.uk/news/x",
        ),
        # edge case 2: meaningful query kept and sorted
        ("https://a.com/p?id=3&utm_medium=x&b=2", "https://a.com/p?b=2&id=3"),
        # edge case 4: default port dropped; www kept in the stored form
        ("https://www.a.com:443/x", "https://www.a.com/x"),
        ("http://a.com:80/x", "http://a.com/x"),
        ("https://a.com:8443/x", "https://a.com:8443/x"),
        ("https://a.com/", "https://a.com"),
        ("https://a.com", "https://a.com"),
        ("https://bbc.co.uk/n?at_medium=RSS&at_campaign=KARANGA", "https://bbc.co.uk/n"),
        ("  https://a.com/x  ", "https://a.com/x"),
        ("https://a.com/p?q=", "https://a.com/p?q="),
    ],
)
def test_canonical_url(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ftp://a.com/x",
        "javascript:alert(1)",
        "mailto:x@a.com",
        "https://",
        "/relative/x",
        "https://a.com:99999/x",
    ],
)
def test_canonical_url_rejects_non_http(raw: str) -> None:
    assert canonical_url(raw) is None


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # edge case 3: AMP variants fold to the same dedupe key
        (
            "https://www.ndtv.com/india-news/story-123/amp/1",
            "https://www.ndtv.com/india-news/story-123",
        ),
        ("https://a.com/x/amp", "https://a.com/x"),
        ("https://a.com/x?amp=1", "https://a.com/x"),
        ("https://a.com/x?outputType=amp", "https://a.com/x"),
        ("https://amp.example.com/x", "https://example.com/x"),
        ("https://www.a.com/x", "https://a.com/x"),
    ],
)
def test_dedupe_key_folds_variants(a: str, b: str) -> None:
    ca, cb = canonical_url(a), canonical_url(b)
    assert ca is not None and cb is not None
    assert dedupe_key(ca) == dedupe_key(cb)
    assert url_hash(ca) == url_hash(cb)


def test_dedupe_key_keeps_distinct_paths() -> None:
    assert dedupe_key("https://a.com/amplifier") == "https://a.com/amplifier"
    assert url_hash("https://a.com/x") != url_hash("https://a.com/y")
    assert len(url_hash("https://a.com/x")) == 64


def test_title_tokens_unicode() -> None:
    # edge case 20: NFKC + casefold, curly quotes and accents do not crash
    fullwidth_ai = "\uff21\uff29"
    assert title_tokens(f"\u201cCaf\u00e9\u201d {fullwidth_ai} Stra\u00dfe") == [
        "café",
        "ai",
        "strasse",
    ]
    assert title_tokens("भारत ने चंद्रयान लॉन्च किया")  # non-empty, no exception


def test_simhash_order_independent_and_stable() -> None:
    a = simhash64("Global Climate Summit Opens With Pledge on Methane Cuts")
    b = simhash64("Climate Summit Opens With Global Pledge on Methane Cuts")
    assert a == b
    assert 0 <= a < 2**64
    assert simhash64("") == 0
    assert simhash64("   !!! ") == 0


_SAME_STORY = [
    (
        "India launches lunar probe Chandrayaan-4 from Sriharikota",
        "India launches lunar probe Chandrayaan-4 from Sriharikota today",
    ),
    (
        "Earthquake strikes off coast of Japan, tsunami warning issued",
        "Earthquake Strikes Off Japan Coast; Tsunami Warning Issued",
    ),
    (
        "Fed holds interest rates steady, signals two cuts this year",
        "Federal Reserve holds rates steady and signals two cuts this year",
    ),
    ("OpenAI releases new reasoning model o5", "OpenAI releases o5, its new reasoning model"),
    (
        "Global Climate Summit Opens With Pledge on Methane Cuts",
        "Climate summit opens with global pledge to cut methane",
    ),
]
_DIFFERENT = [
    (
        "India launches lunar probe Chandrayaan-4 from Sriharikota",
        "European Central Bank holds interest rates steady again",
    ),
    (
        "Fed holds interest rates steady, signals two cuts this year",
        "Bank of England holds interest rates steady",
    ),
    (
        "Earthquake strikes off coast of Japan, tsunami warning issued",
        "Earthquake strikes off coast of Chile, no tsunami warning",
    ),
    ("Live updates: Israel-Gaza war", "Live updates: Ukraine war"),
    (
        "Apple unveils new iPhone with satellite messaging",
        "Google unveils new Pixel with satellite messaging",
    ),
]


@pytest.mark.parametrize(("a", "b"), _SAME_STORY)
def test_same_story_titles_are_near_duplicates(a: str, b: str) -> None:
    """ADR D35 calibration set: rewordings of one story merge."""
    assert titles_near_duplicate(content_tokens(a), content_tokens(b))


@pytest.mark.parametrize(("a", "b"), _DIFFERENT)
def test_different_story_titles_are_not_merged(a: str, b: str) -> None:
    """ADR D35 calibration set: similar-looking but different stories stay apart."""
    assert not titles_near_duplicate(content_tokens(a), content_tokens(b))


def test_short_titles_never_merge_on_title() -> None:
    # brief Q6: < 4 content tokens never merge on title alone
    a, b = content_tokens("Live updates"), content_tokens("Live updates")
    assert a == b
    assert not titles_near_duplicate(a, b)


def test_title_similarity_bounds() -> None:
    assert title_similarity(frozenset(), frozenset({"x"})) == 0.0
    assert title_similarity(frozenset({"x"}), frozenset({"x"})) == 1.0


def test_simhash_word_order_insensitive_and_hamming() -> None:
    assert hamming(simhash64("a b c d"), simhash64("d c b a")) == 0
    assert hamming(0, 2**64 - 1) == 64


@given(st.text(max_size=80))
def test_simhash_range_property(title: str) -> None:
    assert 0 <= simhash64(title) < 2**64


def _parsed(**kw: str | None) -> ParsedItem:
    base: dict[str, str | None] = {
        "external_id": "g1",
        "url": "https://www.a.com/x?utm_source=rss",
        "title": "  A   headline\nhere ",
        "summary": None,
        "author": None,
        "published_at": None,
        "image_url": None,
        "extra_json": None,
    }
    base.update(kw)
    return ParsedItem(**base)  # type: ignore[arg-type]


def test_normalise_basic_and_date_fallback() -> None:
    n = normalise(_parsed(), NOW)
    assert n is not None
    assert n.url == "https://www.a.com/x"
    assert n.url_hash == url_hash("https://a.com/x")
    assert n.title == "A headline here"
    assert n.published_at is None  # edge case 9: raw kept; fallback applied by the property
    assert n.effective_published_at == NOW
    assert n.fetched_at == NOW
    assert n.title_simhash == simhash64("A headline here")


def test_normalise_summary_capped() -> None:
    n = normalise(_parsed(summary="x" * 5000), NOW)
    assert n is not None and n.summary is not None
    assert len(n.summary) == SUMMARY_MAX_CHARS


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
def test_normalise_skips_empty_title(title: str) -> None:
    assert normalise(_parsed(title=title), NOW) is None  # edge case 15


@pytest.mark.parametrize("url", ["", "ftp://a.com/x", "not a url"])
def test_normalise_skips_bad_url(url: str) -> None:
    assert normalise(_parsed(url=url), NOW) is None  # edge case 16


def test_normalise_external_id_falls_back_to_url() -> None:
    n = normalise(_parsed(external_id="  "), NOW)
    assert n is not None
    assert n.external_id == "https://www.a.com/x"


def test_normalise_image_url_verbatim_or_dropped() -> None:
    signed = "https://cdn.example.com/i.jpg?s=abc&w=800&utm_source=x"
    n = normalise(_parsed(image_url=signed), NOW)
    assert n is not None and n.image_url == signed
    n2 = normalise(_parsed(image_url="data:image/png;base64,AAAA"), NOW)
    assert n2 is not None and n2.image_url is None
