"""T4.5 render: props builder, ffprobe verification, render stage (fake + real Remotion)."""

from __future__ import annotations

import datetime
import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.content.script.fallback import template_script
from chronos.db import create_db_engine
from chronos.media.reel_json import build_reel_props, theme_for
from chronos.media.render import (
    RenderError,
    RenderTools,
    expected_duration,
    render_stage,
    verify_mp4,
)
from chronos.media.subprocess import ProcessResult
from chronos.models.media import Reel, ReelAsset
from tests.content.script.helpers import DAY, candidates

REPO = Path(__file__).parents[4]


@pytest.mark.parametrize(
    ("day", "pref", "expected"),
    [
        ((2026, 10, 6), "auto", "dawn"),
        ((2026, 10, 10), "auto", "ink"),
        ((2026, 10, 10), "mono", "mono"),
    ],
)
def test_theme_for(day: tuple[int, int, int], pref: str, expected: str) -> None:
    assert theme_for(datetime.date(*day), pref) == expected


def _tts(n_scenes: int, scene_sec: float = 5.0, gap: float = 0.35) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = 0.0
    for i in range(n_scenes):
        out.append({"scene_index": i, "start_sec": round(t, 3), "end_sec": round(t + scene_sec, 3),
                    "words": 10})  # fmt: skip
        t += scene_sec + gap
    return out


def test_build_reel_props_contiguous_scenes_media_and_attribution() -> None:
    script = template_script(candidates(3), DAY)
    tts = _tts(5)
    captions = {"scenes": [{"index": 1, "words": [{"w": "From", "s": 5.4, "e": 5.6}]}]}
    props = build_reel_props(script, tts, captions, {1: "scene-01.jpg"}, audio_src="voice.wav",
                             audio_duration=26.4, theme="dawn")  # fmt: skip
    assert [s.type for s in props.scenes] == ["cover", "story", "story", "story", "outro"]
    for a, b in zip(props.scenes, props.scenes[1:], strict=False):
        assert a.endSec == b.startSec  # the silence gap belongs to the outgoing scene
    assert props.scenes[-1].endSec == 26.4
    assert props.scenes[1].media is not None and props.scenes[1].media.url == "scene-01.jpg"
    assert props.scenes[2].media is not None and props.scenes[2].media.type == "gradient"
    assert props.scenes[0].media is None and props.scenes[1].source is not None
    assert len(props.captions) == 5 and props.captions[1][0].w == "From"
    assert props.attribution[-2:] == ["Voice: Kokoro-82M", "Captions: whisper.cpp"]
    assert props.attribution[:3] == ["Source 0", "Source 1", "Source 2"]
    assert "null" not in props.to_json()


def _probe(**over: Any) -> dict[str, Any]:
    video = {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
             "r_frame_rate": "30/1"}  # fmt: skip
    audio = {"codec_type": "audio", "codec_name": "aac"}
    video.update(over.pop("video", {}))
    audio.update(over.pop("audio", {}))
    return {"streams": [video, audio], "format": {"duration": over.pop("duration", "60.5")}}


def test_verify_mp4_accepts_good_render() -> None:
    assert verify_mp4(_probe(), 60.0) == pytest.approx(60.5)
    assert expected_duration(60.0) == pytest.approx(60.5)


@pytest.mark.parametrize(
    "probe",
    [
        _probe(video={"codec_name": "hevc"}),
        _probe(video={"width": 720}),
        _probe(video={"r_frame_rate": "25/1"}),
        _probe(audio={"codec_name": "mp3"}),
        _probe(duration="58.0"),
        {"streams": [], "format": {}},
    ],
)
def test_verify_mp4_rejects(probe: dict[str, Any]) -> None:
    with pytest.raises(RenderError):
        verify_mp4(probe, 60.0)


# --- render_stage with a fake runner -------------------------------------------------------------


def _ready_for_render(session: Session, data_root: Path) -> tuple[str, int]:
    from chronos.media.captions import CaptionsError
    from chronos.media.reels import captions_stage, tts_stage
    from chronos.media.tts import FakeEngine
    from tests.media.test_tts import _seed_reel  # pyright: ignore[reportPrivateUsage]

    now, device_id = _seed_reel(session, data_root)
    tts_stage(session, local_date="2026-10-06", device_id=device_id, data_root=data_root,
              engines=[FakeEngine()], ffmpeg_bin=shutil.which("ffmpeg") or "", now=now)  # fmt: skip

    def no_whisper(voice: Path, work: Path) -> list[Any]:
        raise CaptionsError("not in this test")

    captions_stage(session, local_date="2026-10-06", device_id=device_id, data_root=data_root,
                   transcribe=no_whisper, now=now)  # fmt: skip
    return now, device_id


class FakeRunner:
    def __init__(self, fail_first_render: bool = False, fail_all: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_first_render = fail_first_render
        self.fail_all = fail_all
        self.audio_sec = 0.0

    def __call__(self, argv: list[str], **kw: Any) -> ProcessResult:
        self.calls.append(argv)
        if argv[1] == "render":
            renders = sum(1 for c in self.calls if c[1] == "render")
            if self.fail_all or (self.fail_first_render and renders == 1):
                return ProcessResult(1, b"", b"boom", 10)
            props = json.loads(Path(argv[5].removeprefix("--props=")).read_text())
            self.audio_sec = props["audio"]["durationSec"]
            assert Path(argv[6].removeprefix("--public-dir=")).joinpath("voice.wav").is_file()
            Path(argv[4]).write_bytes(b"fake-mp4")
            return ProcessResult(0, b"", b"", 10)
        if argv[1] == "still":
            Path(argv[4]).write_bytes(b"fake-jpg")
            return ProcessResult(0, b"", b"", 10)
        probe = _probe(duration=str(expected_duration(self.audio_sec)))
        return ProcessResult(0, json.dumps(probe).encode(), b"", 10)


TOOLS = RenderTools("/usr/local/bin/node", REPO / "packages" / "renderer", "ffprobe")
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
def test_render_stage_marks_reel_ready(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    with Session(create_db_engine(tmp_db_url)) as session:
        now, device_id = _ready_for_render(session, tmp_path)
        runner = FakeRunner(fail_first_render=True)
        report = render_stage(session, local_date="2026-10-06", device_id=device_id,
                              data_root=tmp_path, tools=TOOLS, theme_preference="auto", now=now,
                              runner=runner)  # fmt: skip
        assert not report.skipped
        renders = [c for c in runner.calls if c[1] == "render"]
        assert [c[-3] for c in renders] == ["--concurrency=2", "--concurrency=1"]  # retry
        reel = session.exec(select(Reel)).one()
        assert reel.status == "ready" and reel.size_bytes == len(b"fake-mp4")
        assert reel.file_path == f"2026-10-06/{reel.public_id}/reel.mp4"
        assert reel.renderer_version.endswith("+MorningReel") and reel.sha256
        kinds = {a.kind for a in session.exec(select(ReelAsset)).all()}
        assert {"reel_json", "thumbnail", "voice_wav", "captions_json"} <= kinds
        assert not (tmp_path / "media" / "2026-10-06" / reel.public_id / "public").exists()
        again = render_stage(session, local_date="2026-10-06", device_id=device_id,
                             data_root=tmp_path, tools=TOOLS, theme_preference="auto", now=now,
                             runner=runner)  # fmt: skip
        assert again.skipped


@needs_ffmpeg
def test_render_failure_marks_reel_failed(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    with Session(create_db_engine(tmp_db_url)) as session:
        now, device_id = _ready_for_render(session, tmp_path)
        with pytest.raises(RenderError):
            render_stage(session, local_date="2026-10-06", device_id=device_id,
                         data_root=tmp_path, tools=TOOLS, theme_preference="auto", now=now,
                         runner=FakeRunner(fail_all=True))  # fmt: skip
        reel = session.exec(select(Reel)).one()
        assert reel.status == "failed" and reel.error


# --- real Remotion end to end (local only) --------------------------------------------------------


@pytest.mark.remotion
@needs_ffmpeg
def test_real_render_end_to_end_produces_valid_mp4(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    """AC: end-to-end on the fixture produces a valid MP4 (ffprobe-verified); durations logged."""
    node, renderer = shutil.which("node"), REPO / "packages" / "renderer"
    if node is None or not (renderer / "node_modules" / ".bin" / "remotion").exists():
        pytest.skip("node / renderer dependencies not installed")
    with Session(create_db_engine(tmp_db_url)) as session:
        t0 = time.perf_counter()
        now, device_id = _ready_for_render(session, tmp_path)
        t1 = time.perf_counter()
        report = render_stage(session, local_date="2026-10-06", device_id=device_id,
                              data_root=tmp_path, tools=RenderTools(node, renderer,
                              shutil.which("ffprobe") or "ffprobe"), theme_preference="auto",
                              now=now)  # fmt: skip
        t2 = time.perf_counter()
        print(f"\nstages to captions {t1 - t0:.1f}s, render {t2 - t1:.1f}s, "
              f"reel {report.duration_sec:.1f}s, render_ms {report.render_ms}")  # fmt: skip
        reel = session.exec(select(Reel)).one()
        assert reel.status == "ready"
        assert (tmp_path / "media" / (reel.file_path or "")).stat().st_size > 100_000
