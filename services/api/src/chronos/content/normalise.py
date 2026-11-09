"""Item normalisation: canonical URLs, dedupe hashes and title simhash (T3.3).

Pure functions only; nothing here touches the network or the database. Canonicalisation
never follows redirects or fetches anything (feed URLs are untrusted).

Two URL forms exist on purpose (T3.3 owner decision 4):

- :func:`canonical_url` is what is **stored** in ``items.url`` and what T3.4's validator
  compares ``source_url`` against. It keeps the original host so the link still opens.
- :func:`dedupe_key` additionally folds ``www.``/``amp.`` hosts and AMP path/query
  variants; :func:`url_hash` hashes it so the same story from an AMP or www link clusters.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from chronos.content.fetchers import ParsedItem

SUMMARY_MAX_CHARS = 1000

_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "ref",
        "ref_src",
        "mc_cid",
        "mc_eid",
        "at_medium",
        "at_campaign",
        "cmpid",
    }
)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_AMP_PATH_RE = re.compile(r"/amp(?:/\d+)?$")
_TOKEN_RE = re.compile(r"\w+")
_WS_RE = re.compile(r"\s+")

#: Near-duplicate titles: stopword-filtered word-set Jaccard (ADR D35). 64-bit simhash is
#: still computed and stored (docs/04) but is too unstable on 6-10 word headlines to decide.
TITLE_JACCARD_MIN = 0.6
MIN_CONTENT_TOKENS = 4
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "on", "in", "to", "for", "with", "and", "or", "but", "its",
        "it", "is", "are", "was", "be", "at", "by", "from", "as", "this", "that", "new",
        "off", "over", "after", "into", "up", "out", "says", "say", "s",
    }
)  # fmt: skip


def canonical_url(url: str) -> str | None:
    """Return the stored canonical form of *url*, or ``None`` if it is not http(s).

    Lowercases scheme and host, drops default ports, the fragment, tracking parameters
    (``utm_*`` and a fixed deny-list) and a trailing ``/``; sorts the remaining query.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def dedupe_key(canonical: str) -> str:
    """Fold host and AMP variants of an already-canonical URL for duplicate detection."""
    parts = urlsplit(canonical)
    host = parts.netloc
    for prefix in ("www.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    path = _AMP_PATH_RE.sub("", parts.path)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "amp" and not (k == "outputType" and v.lower() == "amp")
    ]
    return urlunsplit((parts.scheme, host, path, urlencode(query), ""))


def url_hash(canonical: str) -> str:
    """sha256 hex of :func:`dedupe_key` of *canonical*."""
    return hashlib.sha256(dedupe_key(canonical).encode("utf-8")).hexdigest()


def title_tokens(title: str) -> list[str]:
    """Word tokens of an NFKC-normalised, casefolded title."""
    return _TOKEN_RE.findall(unicodedata.normalize("NFKC", title).casefold())


def content_tokens(title: str) -> frozenset[str]:
    """Distinct title tokens without stopwords, used for near-duplicate detection."""
    return frozenset(t for t in title_tokens(title) if t not in _STOPWORDS)


def title_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two content-token sets (0.0 when either is empty)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def titles_near_duplicate(a: frozenset[str], b: frozenset[str]) -> bool:
    """ADR D35: both titles have >= 4 content tokens and Jaccard >= 0.6."""
    return (
        len(a) >= MIN_CONTENT_TOKENS
        and len(b) >= MIN_CONTENT_TOKENS
        and title_similarity(a, b) >= TITLE_JACCARD_MIN
    )


def simhash64(title: str) -> int:
    """Unsigned 64-bit simhash over the title's tokens (0 when there are none)."""
    weights = [0] * 64
    tokens = title_tokens(title)
    if not tokens:
        return 0
    for token in tokens:
        h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if (h >> bit) & 1 else -1
    return sum(1 << bit for bit in range(64) if weights[bit] > 0)


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two unsigned 64-bit hashes."""
    return (a ^ b).bit_count()


@dataclass(frozen=True, slots=True)
class NormalisedItem:
    """A parsed item ready for the ``items`` table.

    ``published_at`` is the feed's date or ``None``; :func:`effective_published_at` applies
    the ``fetched_at`` fallback so the store can keep an earlier real date on re-fetch.
    """

    external_id: str
    url: str
    url_hash: str
    title: str
    summary: str | None
    author: str | None
    published_at: str | None
    fetched_at: str
    image_url: str | None
    extra_json: str | None
    title_simhash: int

    @property
    def effective_published_at(self) -> str:
        return self.published_at or self.fetched_at


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = _WS_RE.sub(" ", text).strip()
    return cleaned or None


def normalise(item: ParsedItem, now: str) -> NormalisedItem | None:
    """Normalise one parsed item; ``None`` when it has no usable title or URL."""
    title = _clean(item.title)
    url = canonical_url(item.url) if item.url else None
    if title is None or url is None:
        return None
    summary = _clean(item.summary)
    # Image URLs are kept verbatim (signed CDN URLs depend on exact query order);
    # only the scheme is checked here. Fetching them is T4.1 behind the SSRF guard.
    image = item.image_url.strip() if item.image_url else None
    if image is not None and canonical_url(image) is None:
        image = None
    return NormalisedItem(
        external_id=item.external_id.strip() or url,
        url=url,
        url_hash=url_hash(url),
        title=title,
        summary=summary[:SUMMARY_MAX_CHARS] if summary else None,
        author=_clean(item.author),
        published_at=item.published_at,
        fetched_at=now,
        image_url=image,
        extra_json=item.extra_json,
        title_simhash=simhash64(title),
    )
