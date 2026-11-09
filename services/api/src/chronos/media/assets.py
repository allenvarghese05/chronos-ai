"""Assets stage (T4.1, docs/07 §1): one processed image per story scene.

For each story scene, in order of preference:

1. the item's feed image (``items.image_url``);
2. the article page's ``og:image`` / ``twitter:image`` (HTML fetched behind the SSRF guard,
   at most 2 MB);
3. nothing: the renderer draws its themed gradient (no file, no asset row).

Images are downloaded through :class:`~chronos.http.GuardedHttpClient` (SSRF guard, at most 3
redirects, content-type allowlist without SVG, at most 10 MB), then processed with Pillow:
decompression-bomb guard, decode check, orientation applied, longest side <= 4096 px, sRGB
JPEG quality 85, cover-cropped to 1080x1350. The image is rebuilt from raw pixels, so EXIF
(including GPS), ICC and every other metadata field is gone. Optional Pexels b-roll lives in
:mod:`chronos.media.broll` and is skipped without an API key.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import structlog
from PIL import Image, ImageOps
from selectolax.lexbor import LexborHTMLParser

from chronos.content.normalise import canonical_url
from chronos.http import FetchError, GuardedHttpClient

log = structlog.get_logger(__name__)

IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
IMAGE_MAX_BYTES = 10_000_000
HTML_MAX_BYTES = 2_000_000
MAX_SOURCE_PIXELS = 40_000_000  # decompression-bomb guard (~6300x6300)
MAX_SIDE = 4096
OUT_SIZE = (1080, 1350)
JPEG_QUALITY = 85
_META_KEYS = (("property", "og:image"), ("property", "og:image:url"),
              ("name", "twitter:image"), ("property", "twitter:image"))  # fmt: skip


class ImageRejected(ValueError):
    """The bytes are not a usable image (bad decode, too many pixels, wrong format)."""


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    path: Path
    width: int
    height: int
    size_bytes: int
    sha256: str


def og_image_from_html(html: bytes, page_url: str) -> str | None:
    """First ``og:image``/``twitter:image`` URL in *html*, made absolute; http(s) only."""
    try:
        tree = LexborHTMLParser(html.decode("utf-8", errors="replace"))
    except Exception:  # untrusted HTML: any parser failure means "no image"
        return None
    for attr, value in _META_KEYS:
        node = tree.css_first(f'meta[{attr}="{value}"]')
        content = node.attributes.get("content") if node is not None else None
        if content and content.strip():
            absolute = urljoin(page_url, content.strip())
            if canonical_url(absolute) is not None:
                return absolute
    return None


def process_image(data: bytes, out: Path) -> ProcessedImage:
    """Decode, sanitise and write a 1080x1350 JPEG with no metadata.

    Raises:
        ImageRejected: undecodable, too large, or not a raster image Pillow can read.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if probe.width * probe.height > MAX_SOURCE_PIXELS:
                    raise ImageRejected(f"too many pixels ({probe.width}x{probe.height})")
                probe.verify()
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                oriented = ImageOps.exif_transpose(img)
                rgb = oriented.convert("RGB")
        except ImageRejected:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageRejected("decompression bomb") from exc
        except Exception as exc:  # untrusted bytes: Pillow raises many types
            raise ImageRejected(f"cannot decode image: {type(exc).__name__}") from exc
    rgb.thumbnail((MAX_SIDE, MAX_SIDE))
    fitted = ImageOps.fit(rgb, OUT_SIZE, method=Image.Resampling.LANCZOS)
    clean = Image.frombytes("RGB", fitted.size, fitted.tobytes())  # drops every info field
    buffer = io.BytesIO()
    clean.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    payload = buffer.getvalue()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    return ProcessedImage(
        path=out,
        width=clean.width,
        height=clean.height,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class SceneImageRequest:
    scene_index: int
    feed_image_url: str | None
    article_url: str


@dataclass(frozen=True, slots=True)
class SceneImageResult:
    scene_index: int
    image: ProcessedImage | None
    source_url: str | None
    error: str | None = None


SMALL_SOURCE_SIDE = 800  # feed thumbnails below this get an og:image upgrade attempt


def source_pixels(data: bytes) -> tuple[int, int] | None:
    """Image dimensions from the header only (no full decode); ``None`` if unreadable."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            return probe.size
    except Exception:  # untrusted bytes
        return None


async def _raw(image_client: GuardedHttpClient, url: str) -> bytes:
    result = await image_client.fetch(url, allowed_content_types=set(IMAGE_CONTENT_TYPES))
    if result.status != 200:
        raise ImageRejected(f"HTTP {result.status}")
    return result.body


async def fetch_scene_image(
    html_client: GuardedHttpClient,
    image_client: GuardedHttpClient,
    request: SceneImageRequest,
    out: Path,
) -> SceneImageResult:
    """Best-effort image for one scene; never raises (the scene falls back to a gradient).

    The feed image is preferred; the article's og:image is fetched when there is no feed image,
    it fails, or it is a small thumbnail (longest side < 800 px) - the larger one wins.
    """
    errors: list[str] = []
    options: list[tuple[str, bytes, int]] = []  # (url, bytes, pixel count)
    feed = request.feed_image_url
    if feed and canonical_url(feed):
        try:
            data = await _raw(image_client, feed)
            size = source_pixels(data)
            if size is None:
                raise ImageRejected("cannot read image header")
            options.append((feed, data, size[0] * size[1]))
        except (FetchError, ImageRejected) as exc:
            errors.append(f"feed image: {type(exc).__name__}: {exc}"[:200])
    small = not options or max(_dims_side(o[1]) for o in options) < SMALL_SOURCE_SIDE
    if small:
        try:
            page = await html_client.fetch(
                request.article_url, allowed_content_types=set(HTML_CONTENT_TYPES)
            )
            og = og_image_from_html(page.body, page.final_url) if page.status == 200 else None
        except FetchError as exc:
            errors.append(f"article: {type(exc).__name__}")
            og = None
        if og and og != feed:
            try:
                data = await _raw(image_client, og)
                size = source_pixels(data)
                if size is not None:
                    options.append((og, data, size[0] * size[1]))
            except (FetchError, ImageRejected) as exc:
                errors.append(f"og:image: {type(exc).__name__}: {exc}"[:200])
    for url, data, _ in sorted(options, key=lambda o: o[2], reverse=True):
        try:
            return SceneImageResult(request.scene_index, process_image(data, out), url)
        except ImageRejected as exc:
            errors.append(f"{url}: {exc}"[:200])
    if errors:
        log.info("asset_image_missing", scene=request.scene_index, errors=errors)
    return SceneImageResult(request.scene_index, None, None, "; ".join(errors) or "no image")


def _dims_side(data: bytes) -> int:
    size = source_pixels(data)
    return max(size) if size else 0


async def fetch_scene_images(
    html_client: GuardedHttpClient,
    image_client: GuardedHttpClient,
    requests: Sequence[SceneImageRequest],
    out_dir: Path,
) -> list[SceneImageResult]:
    """Images for every story scene, sequentially (politeness; ~6 small downloads)."""
    results: list[SceneImageResult] = []
    for req in requests:
        out = out_dir / f"scene-{req.scene_index:02d}.jpg"
        results.append(await fetch_scene_image(html_client, image_client, req, out))
    return results
