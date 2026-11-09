"""T4.2 TTS: timing maths, WAV I/O, engine fallback, loudnorm, the stage, real Kokoro."""

from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.media.tts import (
    GAP_SEC,
    SAMPLE_RATE,
    FakeEngine,
    KokoroEngine,
    TtsError,
    concatenate,
    read_wav,
    resample,
    silence_like,
    synthesize_scenes,
    wav_duration,
    write_wav,
)
from chronos.models.media import Reel, ReelAsset

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
REPO = Path(__file__).parents[4]


def tone(seconds: float) -> np.ndarray:
    return silence_like("x", seconds_per_word=seconds)


def test_concatenate_inserts_gaps_and_reports_starts() -> None:
    joined, starts = concatenate([tone(1.0), tone(2.0), tone(0.5)])
    assert starts == pytest.approx([0.0, 1.0 + GAP_SEC, 3.0 + 2 * GAP_SEC])
    assert joined.size == round((3.5 + 2 * GAP_SEC) * SAMPLE_RATE)
    gap = joined[SAMPLE_RATE : SAMPLE_RATE + round(GAP_SEC * SAMPLE_RATE)]
    assert np.all(gap == 0)


def test_concatenate_empty() -> None:
    joined, starts = concatenate([])
    assert joined.size == 0 and starts == []


def test_wav_round_trip_and_duration(tmp_path: Path) -> None:
    audio = tone(1.5)
    write_wav(tmp_path / "a.wav", audio)
    back = read_wav(tmp_path / "a.wav")
    assert back.size == audio.size
    assert np.max(np.abs(back - audio)) < 1e-3
    assert wav_duration(tmp_path / "a.wav") == pytest.approx(1.5, abs=1e-3)


def test_resample_length() -> None:
    assert resample(tone(1.0), SAMPLE_RATE, 16_000).size == 16_000
    same = tone(0.2)
    assert resample(same, SAMPLE_RATE, SAMPLE_RATE) is same


def test_kokoro_engine_missing_files_raises(tmp_path: Path) -> None:
    with pytest.raises(TtsError, match="missing"):
        KokoroEngine(tmp_path / "x.onnx", tmp_path / "v.bin").synthesize("hello there")


@needs_ffmpeg
def test_synthesize_scenes_writes_voice_timings_and_mp3(tmp_path: Path) -> None:
    texts = [(0, "Good morning. Here is your brief."), (1, " ".join(["word"] * 20)),
             (2, "That is all for today, sources are saved.")]  # fmt: skip
    res = synthesize_scenes(texts, tmp_path / "reel", tmp_path, [FakeEngine()], FFMPEG or "")
    assert res.voice_wav.is_file() and res.voice_mp3.is_file() and res.tts_json.is_file()
    timings = json.loads(res.tts_json.read_text())
    assert [t["scene_index"] for t in timings] == [0, 1, 2]
    for a, b in itertools.pairwise(timings):
        assert a["end_sec"] < b["start_sec"]  # monotone, gap between scenes
        assert b["start_sec"] - a["end_sec"] == pytest.approx(GAP_SEC, abs=0.002)
    assert res.duration_sec == pytest.approx(
        timings[-1]["end_sec"], abs=0.05
    )  # loudnorm keeps length
    assert (tmp_path / "reel" / "voice" / "scene-01.wav").is_file()
    assert not (tmp_path / "reel" / "voice_raw.wav").exists()


@needs_ffmpeg
def test_fallback_engine_used_when_primary_fails(tmp_path: Path) -> None:
    primary, fallback = FakeEngine(fail=True), FakeEngine(name="edge_tts")
    res = synthesize_scenes([(0, "one two three four five")], tmp_path / "r", tmp_path,
                            [primary, fallback], FFMPEG or "")  # fmt: skip
    assert res.engines == {"edge_tts"} and len(primary.calls) == 1


@needs_ffmpeg
def test_too_short_audio_triggers_fallback(tmp_path: Path) -> None:
    class Tiny(FakeEngine):
        def synthesize(self, text: str) -> np.ndarray:
            return tone(0.2)

    engines = [Tiny(), FakeEngine(name="edge_tts")]
    text = "a sentence that is long enough to speak"
    res = synthesize_scenes([(0, text)], tmp_path / "r", tmp_path, engines, FFMPEG or "")
    assert res.engines == {"edge_tts"}


def test_all_engines_failing_raises(tmp_path: Path) -> None:
    with pytest.raises(TtsError, match="scene 0"):
        synthesize_scenes([(0, "hello")], tmp_path / "r", tmp_path,
                          [FakeEngine(fail=True)], "ffmpeg")  # fmt: skip


def _seed_reel(session: Session, data_root: Path) -> tuple[str, int]:
    import httpx

    from chronos.content.script.job import ScriptJobPayload, run_script_job
    from chronos.http import GuardedHttpClient
    from chronos.media.reels import assets_stage
    from tests.content.script.test_job import SOURCES, seed_day

    now, device_id = seed_day(session)
    run_script_job(session, ScriptJobPayload.model_validate(
        {"local_date": "2026-10-06", "device_id": device_id}), SOURCES, now, [])  # fmt: skip
    transport = httpx.MockTransport(lambda r: httpx.Response(404))

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    def client() -> GuardedHttpClient:
        return GuardedHttpClient("t", resolver=resolver, transport=transport)

    assets_stage(session, local_date="2026-10-06", device_id=device_id, data_root=data_root,
                 user_agent="t", now=now, html_client=client(), image_client=client())  # fmt: skip
    return now, device_id


@needs_ffmpeg
def test_tts_stage_records_assets_and_is_idempotent(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    from chronos.media.reels import tts_stage

    with Session(create_db_engine(tmp_db_url)) as session:
        now, device_id = _seed_reel(session, tmp_path)
        engine = FakeEngine()
        report = tts_stage(session, local_date="2026-10-06", device_id=device_id,
                           data_root=tmp_path, engines=[engine], ffmpeg_bin=FFMPEG or "",
                           now=now)  # fmt: skip
        assert not report.skipped and report.duration_sec > 10
        reel = session.exec(select(Reel)).one()
        assert reel.status == "tts" and reel.tts_ms is not None
        kinds = {a.kind: a for a in session.exec(select(ReelAsset)).all()}
        assert kinds["voice_wav"].licence == "kokoro" and kinds["voice_mp3"].size_bytes
        assert (tmp_path / "media" / kinds["voice_wav"].path).is_file()
        n = len(engine.calls)
        again = tts_stage(session, local_date="2026-10-06", device_id=device_id,
                          data_root=tmp_path, engines=[engine], ffmpeg_bin=FFMPEG or "",
                          now=now)  # fmt: skip
        assert again.skipped and len(engine.calls) == n


@pytest.mark.kokoro
@needs_ffmpeg
def test_real_kokoro_fixture_script_is_50_to_70_seconds(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    """AC: Kokoro produces a 50-70 s WAV for the fixture script (local only)."""
    models = REPO / "data" / "models" / "kokoro"
    if not (models / "kokoro-v1.0.onnx").exists():
        pytest.skip("Kokoro model not downloaded (just models)")
    from chronos.content.script.fallback import template_script
    from tests.content.script.helpers import DAY, candidates

    script = template_script(candidates(6), DAY)
    texts = [(s.index, s.voiceover) for s in script.scenes]
    engine = KokoroEngine(models / "kokoro-v1.0.onnx", models / "voices-v1.0.bin")
    res = synthesize_scenes(texts, tmp_path / "reel", tmp_path, [engine], FFMPEG or "")
    print(f"\nwords={script.total_words} duration={res.duration_sec:.1f}s")
    assert 50.0 <= res.duration_sec <= 70.0
