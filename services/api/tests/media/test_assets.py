"""T4.1 assets: sanitising images, og:image fallback, guards, and the assets stage."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.http import GuardedHttpClient
from chronos.media.assets import (
    IMAGE_MAX_BYTES,
    ImageRejected,
    SceneImageRequest,
    fetch_scene_image,
    og_image_from_html,
    process_image,
)
from chronos.media.reels import BriefNotReady, assets_stage
from chronos.models.media import Reel, ReelAsset

GPS_TAG = 0x8825


def jpeg_with_gps(size: tuple[int, int] = (1600, 900)) -> bytes:
    img = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SpyCam"  # Make
    exif[GPS_TAG] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def png_rgba() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (500, 800), (0, 0, 255, 128)).save(buf, format="PNG")
    return buf.getvalue()


# --- process_image -----------------------------------------------------------------------------


def test_exif_gps_is_stripped_and_output_is_cover_jpeg(tmp_path: Path) -> None:
    src = jpeg_with_gps()
    with Image.open(io.BytesIO(src)) as check:
        assert GPS_TAG in check.getexif()  # precondition: the input really has GPS
    out = process_image(src, tmp_path / "scene-01.jpg")
    with Image.open(out.path) as img:
        assert img.format == "JPEG" and img.size == (1080, 1350) and img.mode == "RGB"
        assert len(img.getexif()) == 0
        assert "exif" not in img.info and "icc_profile" not in img.info
    assert b"SpyCam" not in out.path.read_bytes()
    assert out.size_bytes == out.path.stat().st_size and len(out.sha256) == 64


def test_png_with_alpha_converted(tmp_path: Path) -> None:
    out = process_image(png_rgba(), tmp_path / "x.jpg")
    assert (out.width, out.height) == (1080, 1350)


@pytest.mark.parametrize(
    "data",
    [
        b"not an image",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"<html><body>hi</body></html>",
        jpeg_with_gps()[:200],  # truncated
    ],
)
def test_non_images_rejected(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(ImageRejected):
        process_image(data, tmp_path / "x.jpg")
    assert not (tmp_path / "x.jpg").exists()


def test_decompression_bomb_rejected(tmp_path: Path) -> None:
    buf = io.BytesIO()
    Image.new("1", (7000, 7000)).save(buf, format="PNG")  # 49 MP, tiny file
    with pytest.raises(ImageRejected, match="too many pixels"):
        process_image(buf.getvalue(), tmp_path / "x.jpg")


# --- og:image ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<meta property="og:image" content="/img/a.jpg">', "https://news.example.com/img/a.jpg"),
        ('<meta name="twitter:image" content="https://cdn.example.com/t.png">',
         "https://cdn.example.com/t.png"),
        ('<meta property="og:image" content="javascript:alert(1)">', None),
        ("<p>no meta</p>", None),
    ],
)  # fmt: skip
def test_og_image_from_html(html: str, expected: str | None) -> None:
    assert og_image_from_html(html.encode(), "https://news.example.com/story/1") == expected


# --- fetch_scene_image (guarded clients over MockTransport) ------------------------------------


def _clients(
    handler: Callable[[httpx.Request], httpx.Response], resolve: str = "93.184.216.34"
) -> tuple[GuardedHttpClient, GuardedHttpClient]:
    async def resolver(host: str) -> list[str]:
        return ["10.0.0.5"] if host == "internal.example" else [resolve]

    def make(max_bytes: int) -> GuardedHttpClient:
        return GuardedHttpClient("test", resolver=resolver, max_bytes=max_bytes,
                                 transport=httpx.MockTransport(handler))  # fmt: skip

    return make(2_000_000), make(IMAGE_MAX_BYTES)


def _run(handler: Callable[[httpx.Request], httpx.Response], req: SceneImageRequest, out: Path):
    html, images = _clients(handler)
    return asyncio.run(fetch_scene_image(html, images, req, out))


JPEG = jpeg_with_gps()


def req(index: int, feed: str) -> SceneImageRequest:
    return SceneImageRequest(index, feed, "https://news.example.com/s")


def test_feed_image_used_first(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "img.example.com"
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    res = _run(handler, req(1, "https://img.example.com/a.jpg"), tmp_path / "s.jpg")
    assert res.image is not None and res.source_url == "https://img.example.com/a.jpg"


def test_falls_back_to_og_image_when_feed_image_is_svg(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/bad.svg":
            return httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})
        if req.url.host == "news.example.com":
            page = b'<meta property="og:image" content="https://img.example.com/og.jpg">'
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    res = _run(handler, req(2, "https://img.example.com/bad.svg"), tmp_path / "s.jpg")
    assert res.source_url == "https://img.example.com/og.jpg" and res.image is not None


def test_oversized_image_aborted_and_no_file(tmp_path: Path) -> None:
    big = b"\xff\xd8" + b"0" * (IMAGE_MAX_BYTES + 10)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "news.example.com":
            return httpx.Response(200, content=b"<p></p>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})

    res = _run(handler, req(3, "https://img.example.com/huge.jpg"), tmp_path / "s.jpg")
    assert res.image is None and "TooLarge" in (res.error or "")
    assert not (tmp_path / "s.jpg").exists()


def test_redirect_to_private_address_blocked(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "img.example.com":
            return httpx.Response(302, headers={"location": "http://internal.example/x.jpg"})
        return httpx.Response(200, content=b"<p></p>", headers={"content-type": "text/html"})

    res = _run(handler, req(4, "https://img.example.com/r.jpg"), tmp_path / "s.jpg")
    assert res.image is None and "SsrfBlocked" in (res.error or "")


# --- the assets stage ---------------------------------------------------------------------------


def test_assets_stage_end_to_end_and_idempotent(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    from chronos.content.script.job import ScriptJobPayload, run_script_job
    from tests.content.script.test_job import SOURCES, seed_day

    engine = create_db_engine(tmp_db_url)
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if "image" in req.url.path or req.url.path.endswith((".jpg", ".png")):
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        page = b'<meta property="og:image" content="/image/og.jpg">'
        return httpx.Response(200, content=page, headers={"content-type": "text/html"})

    with Session(engine) as session:
        now, device_id = seed_day(session)
        payload = ScriptJobPayload.model_validate(
            {"local_date": "2026-10-06", "device_id": device_id}
        )
        run_script_job(session, payload, SOURCES, now, [])  # template brief
        html, images = _clients(handler)
        report = assets_stage(
            session, local_date="2026-10-06", device_id=device_id, data_root=tmp_path,
            user_agent="test", now=now, html_client=html, image_client=images,
        )  # fmt: skip
        assert report.images == 6 and report.gradients == 0  # >= 1 asset per story
        reel = session.exec(select(Reel)).one()
        assert reel.status == "assets" and reel.expires_at == "2026-10-07T06:00:00.000000Z"
        rows = session.exec(select(ReelAsset)).all()
        assert sorted(r.scene_index or 0 for r in rows) == [1, 2, 3, 4, 5, 6]
        for row in rows:
            assert (tmp_path / "media" / row.path).is_file()
            assert row.path.startswith(f"2026-10-06/{reel.public_id}/assets/scene-")

        n_calls = len(calls)
        html2, images2 = _clients(handler)
        again = assets_stage(
            session, local_date="2026-10-06", device_id=device_id, data_root=tmp_path,
            user_agent="test", now=now, html_client=html2, image_client=images2,
        )  # fmt: skip
        assert again.skipped_existing == 6 and len(calls) == n_calls  # nothing re-downloaded
        assert len(session.exec(select(Reel)).all()) == 1


def test_assets_stage_needs_ready_brief(test_settings: Settings, tmp_db_url: str,
                                        tmp_path: Path) -> None:  # fmt: skip
    with Session(create_db_engine(tmp_db_url)) as session, pytest.raises(BriefNotReady):
        assets_stage(session, local_date="2026-10-06", device_id=1, data_root=tmp_path,
                     user_agent="t", now="2026-10-06T06:00:00.000000Z")  # fmt: skip


def test_broll_scene_without_pexels_uses_image_path(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    """AC: the Pexels path is skipped without a key (scene still gets an image)."""
    from chronos.content.script.job import ScriptJobPayload, run_script_job
    from chronos.models.content import Brief
    from tests.content.script.test_job import SOURCES, seed_day

    def handler(request: httpx.Request) -> httpx.Response:
        assert "pexels" not in request.url.host
        if request.url.path.startswith("/image/"):
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        page = b'<meta property="og:image" content="/image/og.jpg">'
        return httpx.Response(200, content=page, headers={"content-type": "text/html"})

    with Session(create_db_engine(tmp_db_url)) as session:
        now, device_id = seed_day(session)
        payload = ScriptJobPayload.model_validate(
            {"local_date": "2026-10-06", "device_id": device_id}
        )
        brief = run_script_job(session, payload, SOURCES, now, [])
        script = json.loads(brief.script_json)
        script["scenes"][1]["visual"] = "broll"
        script["scenes"][1]["broll_query"] = "city night"
        row = session.get(Brief, brief.id)
        assert row is not None
        row.script_json = json.dumps(script)
        session.add(row)
        session.commit()
        html, images = _clients(handler)
        report = assets_stage(
            session, local_date="2026-10-06", device_id=device_id, data_root=tmp_path,
            user_agent="test", now=now, html_client=html, image_client=images,
            pexels_available=False,
        )  # fmt: skip
        assert report.images == 6


def test_small_feed_thumbnail_upgraded_to_larger_og_image(tmp_path: Path) -> None:
    small_buf = io.BytesIO()
    Image.new("RGB", (240, 135), (0, 255, 0)).save(small_buf, format="JPEG")
    small = small_buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/thumb.jpg":
            return httpx.Response(200, content=small, headers={"content-type": "image/jpeg"})
        if request.url.host == "news.example.com":
            page = b'<meta property="og:image" content="https://img.example.com/big.jpg">'
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    res = _run(handler, req(5, "https://img.example.com/thumb.jpg"), tmp_path / "s.jpg")
    assert res.source_url == "https://img.example.com/big.jpg"


def test_large_feed_image_does_not_fetch_article(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host != "news.example.com", "article fetched needlessly"
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    res = _run(handler, req(6, "https://img.example.com/a.jpg"), tmp_path / "s.jpg")
    assert res.image is not None
