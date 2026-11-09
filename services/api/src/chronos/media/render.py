"""Render stage (T4.5, docs/07 §4): ``reel.json`` -> Remotion -> verified ``reel.mp4``.

- A per-render public dir under the reel's folder (``public/``) holds ``voice.wav``, the scene
  images and a symlink to the renderer's fonts; Remotion runs with ``--public-dir`` so nothing
  is copied into the repo. It is removed after the render.
- Remotion runs through :func:`chronos.media.subprocess.run` (argv list, timeout, cwd under
  ``data/``, minimal environment with ``PATH`` = node's directory) - verified to work there.
- One retry at ``--concurrency=1`` (docs/02 §7). Then ``ffprobe`` verifies h264 1080x1920 @
  30 fps + aac and a duration within 0.5 s of the composition length; a still at frame 45 is
  the feed thumbnail; sha256, size, timings and ``renderer_version`` are recorded.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import structlog
from sqlmodel import Session

from chronos.content.script.schema import ReelScript
from chronos.enums import AssetLicence, ReelAssetKind, ReelStatus
from chronos.media.reel_json import ThemeName, build_reel_props, theme_for
from chronos.media.reels import (
    existing_asset,
    media_root,
    record_asset,
    reel_dir,
    reel_for_day,
)
from chronos.media.subprocess import ProcessResult, run
from chronos.models.media import ReelAsset

log = structlog.get_logger(__name__)

RENDER_TIMEOUT_SEC = 840.0  # job timeout is 900 s
STILL_TIMEOUT_SEC = 120.0
PROBE_TIMEOUT_SEC = 30.0
FPS = 30
TAIL_FRAMES = 15
DURATION_TOLERANCE_SEC = 0.5

Runner = Callable[..., ProcessResult]


class RenderError(RuntimeError):
    """Rendering or verification failed."""


@dataclass(frozen=True, slots=True)
class RenderTools:
    node_bin: str
    renderer_dir: Path
    ffprobe_bin: str

    @property
    def remotion(self) -> str:
        return str(self.renderer_dir / "node_modules" / ".bin" / "remotion")

    @property
    def entry(self) -> str:
        return str(self.renderer_dir / "src" / "index.ts")

    @property
    def env(self) -> dict[str, str]:
        return {"PATH": f"{Path(self.node_bin).parent}:/usr/bin:/bin"}


def renderer_version(renderer_dir: Path) -> str:
    try:
        data = json.loads((renderer_dir / "package.json").read_text(encoding="utf-8"))
        return f"{data.get('version', '0.0.0')}+MorningReel"
    except (OSError, ValueError):
        return "unknown+MorningReel"


def expected_duration(audio_sec: float) -> float:
    return (math.ceil(audio_sec * FPS) + TAIL_FRAMES) / FPS


def verify_mp4(probe: dict[str, Any], audio_sec: float) -> float:
    """Check codec, size, fps and duration from ``ffprobe -show_streams -show_format`` JSON."""
    streams = cast("list[dict[str, Any]]", probe.get("streams", []))
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise RenderError("missing video or audio stream")
    if video.get("codec_name") != "h264" or (video.get("width"), video.get("height")) != (
        1080,
        1920,
    ):
        raise RenderError(f"unexpected video stream {video.get('codec_name')} "
                          f"{video.get('width')}x{video.get('height')}")  # fmt: skip
    if video.get("r_frame_rate") not in ("30/1", "30000/1000"):
        raise RenderError(f"unexpected frame rate {video.get('r_frame_rate')}")
    if audio.get("codec_name") != "aac":
        raise RenderError(f"unexpected audio codec {audio.get('codec_name')}")
    duration = float(cast("dict[str, Any]", probe.get("format", {})).get("duration", 0.0))
    if abs(duration - expected_duration(audio_sec)) > DURATION_TOLERANCE_SEC:
        raise RenderError(
            f"duration {duration:.2f}s, expected ~{expected_duration(audio_sec):.2f}s"
        )
    return duration


def _ok(result: ProcessResult, what: str) -> None:
    if result.timed_out or result.returncode != 0:
        raise RenderError(f"{what} failed (exit {result.returncode}, timed_out={result.timed_out})")


def stage_public(
    out_dir: Path, renderer_dir: Path, voice_wav: Path, images: Sequence[tuple[int, Path]]
) -> tuple[Path, dict[int, str]]:
    public = out_dir / "public"
    if public.exists():
        shutil.rmtree(public)
    public.mkdir(parents=True)
    (public / "fonts").symlink_to(renderer_dir / "public" / "fonts", target_is_directory=True)
    shutil.copyfile(voice_wav, public / "voice.wav")
    names: dict[int, str] = {}
    for index, path in images:
        name = f"scene-{index:02d}.jpg"
        shutil.copyfile(path, public / name)
        names[index] = name
    return public, names


@dataclass(frozen=True, slots=True)
class RenderReport:
    reel_public_id: str
    file_path: str
    duration_sec: float
    render_ms: int
    skipped: bool


def render_stage(
    session: Session,
    *,
    local_date: str,
    device_id: int,
    data_root: Path,
    tools: RenderTools,
    theme_preference: str,
    now: str,
    runner: Runner = run,
) -> RenderReport:
    """Render and verify the day's reel; marks it ``ready``. Idempotent once ready."""
    brief, reel = reel_for_day(session, local_date, device_id)
    assert reel.id is not None
    out_dir = reel_dir(data_root, reel)
    mp4 = out_dir / "reel.mp4"
    if reel.status == ReelStatus.ready.value and mp4.exists():
        return RenderReport(reel.public_id, reel.file_path or "", reel.duration_sec or 0.0,
                            reel.render_ms or 0, True)  # fmt: skip

    script = ReelScript.model_validate_json(brief.script_json)
    tts = cast("list[dict[str, Any]]", json.loads((out_dir / "tts.json").read_text("utf-8")))
    captions_file = out_dir / "captions.json"
    captions = json.loads(captions_file.read_text("utf-8")) if captions_file.exists() else None
    voice = out_dir / "voice.wav"
    voice_asset = existing_asset(session, reel.id, ReelAssetKind.voice_wav, None)
    audio_sec = (voice_asset.duration_sec if voice_asset else None) or tts[-1]["end_sec"]
    image_rows = [
        a for a in _image_assets(session, reel.id) if (media_root(data_root) / a.path).exists()
    ]
    public, names = stage_public(
        out_dir, tools.renderer_dir, voice,
        [(a.scene_index or 0, media_root(data_root) / a.path) for a in image_rows],
    )  # fmt: skip
    theme: ThemeName = theme_for(script.local_date, theme_preference)
    credit = (
        "Voice: edge-tts"
        if voice_asset and voice_asset.licence == "edge_tts"
        else ("Voice: Kokoro-82M")
    )
    props = build_reel_props(
        script, tts, captions, names, audio_src="voice.wav", audio_duration=audio_sec,
        theme=theme, voice_credit=credit,
    )  # fmt: skip
    reel_json = out_dir / "reel.json"
    reel_json.write_text(props.to_json(), encoding="utf-8")
    record_asset(session, reel.id, data_root, ReelAssetKind.reel_json, reel_json,
                 licence=AssetLicence.generated)  # fmt: skip
    reel.status = ReelStatus.rendering.value
    session.add(reel)
    session.commit()

    started = time.perf_counter()
    try:
        last: RenderError | None = None
        for concurrency in (2, 1):
            argv = [tools.remotion, "render", tools.entry, "MorningReel", str(mp4),
                    f"--props={reel_json}", f"--public-dir={public}", "--codec=h264", "--crf=20",
                    "--audio-codec=aac", "--audio-bitrate=160k", f"--concurrency={concurrency}",
                    "--timeout=120000", "--log=error"]  # fmt: skip
            try:
                _ok(runner(argv, timeout=RENDER_TIMEOUT_SEC, cwd=out_dir, data_root=data_root,
                           env=tools.env), "remotion render")  # fmt: skip
                last = None
                break
            except RenderError as exc:
                last = exc
                log.warning("render_attempt_failed", reel=reel.public_id, concurrency=concurrency,
                            error=str(exc))  # fmt: skip
        if last is not None:
            raise last
        render_ms = int((time.perf_counter() - started) * 1000)
        probe = runner([tools.ffprobe_bin, "-v", "error", "-print_format", "json", "-show_streams",
                        "-show_format", str(mp4)], timeout=PROBE_TIMEOUT_SEC, cwd=out_dir,
                       data_root=data_root)  # fmt: skip
        _ok(probe, "ffprobe")
        duration = verify_mp4(json.loads(probe.stdout.decode("utf-8")), audio_sec)
        thumb = out_dir / "thumb.jpg"
        still = runner([tools.remotion, "still", tools.entry, "MorningReel", str(thumb),
                        f"--props={reel_json}", f"--public-dir={public}", "--frame=45",
                        "--image-format=jpeg", "--log=error"], timeout=STILL_TIMEOUT_SEC,
                       cwd=out_dir, data_root=data_root, env=tools.env)  # fmt: skip
        _ok(still, "remotion still")
    except (RenderError, OSError, ValueError) as exc:
        reel.status = ReelStatus.failed.value
        reel.error = str(exc)[:500]
        reel.updated_at = now
        session.add(reel)
        session.commit()
        raise RenderError(str(exc)) from exc
    finally:
        shutil.rmtree(public, ignore_errors=True)

    data = mp4.read_bytes()
    record_asset(session, reel.id, data_root, ReelAssetKind.thumbnail, thumb,
                 licence=AssetLicence.generated)  # fmt: skip
    reel.file_path = str(mp4.resolve().relative_to(media_root(data_root).resolve()))
    reel.sha256 = hashlib.sha256(data).hexdigest()
    reel.size_bytes = len(data)
    reel.duration_sec = round(duration, 3)
    reel.render_ms = render_ms
    reel.renderer_version = renderer_version(tools.renderer_dir)
    reel.status = ReelStatus.ready.value
    reel.error = None
    reel.updated_at = now
    session.add(reel)
    session.commit()
    log.info("render_done", reel=reel.public_id, seconds=round(duration, 1), render_ms=render_ms,
             size=len(data))  # fmt: skip
    return RenderReport(reel.public_id, reel.file_path, reel.duration_sec, render_ms, False)


def _image_assets(session: Session, reel_id: int) -> list[ReelAsset]:
    from sqlmodel import select

    return list(
        session.exec(
            select(ReelAsset).where(
                ReelAsset.reel_id == reel_id, ReelAsset.kind == ReelAssetKind.image.value
            )
        ).all()
    )
