"""T4.3 captions: alignment, interpolation, fallback, the stage, real whisper (local)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlmodel import Session, select

from chronos.config import Settings
from chronos.db import create_db_engine
from chronos.media.captions import (
    CaptionsError,
    SceneRange,
    TimedWord,
    align,
    build_captions,
    norm,
    parse_whisper_json,
    proportional,
    run_whisper,
    time_scene,
)

REPO = Path(__file__).parents[4]


def heard(*items: tuple[str, float, float]) -> list[TimedWord]:
    return [TimedWord(w, s, e) for w, s, e in items]


# --- alignment on hand-made token sequences -------------------------------------------------------


def test_norm() -> None:
    assert norm("Qwen3-VL,") == "qwen3vl" and norm("5.4%") == "54" and norm("“BBC”") == "bbc"


def test_align_exact_and_spelling_variants() -> None:
    script = ["Qwen3-VL", "scores", "5.4", "percent", "higher."]
    spoken = ["QN3VL", "scores", "5.4%", "higher."]
    mapping = align(script, spoken)
    assert mapping[0] == 0  # partial match QN3VL ~ qwen3vl
    assert mapping[1] == 1 and mapping[4] == 3
    assert mapping[2] == 2  # "5.4" ~ "5.4%"
    assert mapping[3] is None  # "percent" has no spoken token of its own


def test_align_insertion_and_deletion() -> None:
    assert align(["a", "quick", "brown", "fox"], ["a", "brown", "fox"]) == [0, None, 1, 2]
    assert align(["brown", "fox"], ["the", "brown", "um", "fox"]) == [1, 3]


def test_align_empty_sides() -> None:
    assert align([], ["x"]) == [] and align(["x"], []) == [None]


# --- timing ---------------------------------------------------------------------------------------


def test_proportional_is_character_weighted_and_spans_range() -> None:
    out = proportional(["a", "longer"], 1.0, 3.0)
    assert out[0].s == 1.0 and out[-1].e == pytest.approx(3.0, abs=0.002)
    assert (out[1].e - out[1].s) > (out[0].e - out[0].s)


def test_time_scene_uses_whisper_times_and_interpolates_gaps() -> None:
    scene = SceneRange(1, 2.0, 5.0)
    spoken = heard(("Rivers", 2.1, 2.5), ("burst", 2.5, 2.9), ("banks", 3.6, 4.0))
    timed, matched = time_scene(["Rivers", "burst", "their", "banks."], spoken, scene)
    assert matched == 3
    assert [t.w for t in timed] == ["Rivers", "burst", "their", "banks."]  # script spelling
    assert timed[0].s == pytest.approx(2.1)
    assert timed[1].e <= timed[2].s < timed[2].e <= timed[3].s  # "their" interpolated between
    assert timed[3].s == pytest.approx(3.6)


def test_time_scene_ignores_words_from_other_scenes() -> None:
    scene = SceneRange(2, 10.0, 12.0)
    timed, matched = time_scene(["hello", "world"], heard(("hello", 1.0, 1.3)), scene)
    assert matched == 0 and timed[0].s == 10.0  # proportional fallback inside the scene


def _assert_monotone(words: list[TimedWord], scene: SceneRange) -> None:
    prev = scene.start
    for t in words:
        assert scene.start - 1e-6 <= t.s < t.e <= scene.end + 1e-6
        assert t.s >= prev - 1e-6
        prev = t.e


@settings(max_examples=200, deadline=None)
@given(
    st.lists(st.sampled_from(["the", "BBC", "5.4", "Qwen3-VL", "rivers", "x"]), min_size=1,
             max_size=12),
    st.lists(st.tuples(st.sampled_from(["the", "bbc", "5.4%", "qn3vl", "rivers", "um"]),
                       st.floats(0, 12), st.floats(0.01, 1.5)), max_size=15),
)  # fmt: skip
def test_timings_always_monotone_and_inside_scene(
    words: list[str], raw: list[tuple[str, float, float]]
) -> None:
    scene = SceneRange(1, 3.0, 9.0)
    spoken = sorted((TimedWord(w, s, s + d) for w, s, d in raw), key=lambda t: t.s)
    timed, _ = time_scene(words, spoken, scene)
    assert [t.w for t in timed] == words
    _assert_monotone(timed, scene)


def test_build_captions_whisper_failed_uses_proportional() -> None:
    res = build_captions([(0, ["Good", "morning"])], [SceneRange(0, 0.0, 1.0)], None)
    assert not res.used_whisper and res.coverage == 0.0
    doc = json.loads(res.to_json())
    assert doc["scenes"][0]["index"] == 0 and doc["scenes"][0]["words"][0]["w"] == "Good"


def test_parse_whisper_json_skips_empty_and_bad_segments() -> None:
    data = {"transcription": [
        {"text": "", "offsets": {"from": 0, "to": 100}},
        {"text": " Hello", "offsets": {"from": 100, "to": 400}},
        {"text": " bad", "offsets": {"from": "x", "to": 1}},
    ]}  # fmt: skip
    assert parse_whisper_json(data) == [TimedWord("Hello", 0.1, 0.4)]


def test_run_whisper_missing_model(tmp_path: Path) -> None:
    with pytest.raises(CaptionsError, match="model missing"):
        run_whisper("whisper-cli", "ffmpeg", tmp_path / "nope.bin", tmp_path / "v.wav",
                    tmp_path, tmp_path)  # fmt: skip


# --- stage ----------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_captions_stage_end_to_end_with_fake_transcriber(
    test_settings: Settings, tmp_db_url: str, tmp_path: Path
) -> None:
    from chronos.media.reels import captions_stage, tts_stage
    from chronos.media.tts import FakeEngine
    from chronos.models.media import Reel, ReelAsset
    from tests.media.test_tts import _seed_reel  # pyright: ignore[reportPrivateUsage]

    ffmpeg = shutil.which("ffmpeg") or ""
    with Session(create_db_engine(tmp_db_url)) as session:
        now, device_id = _seed_reel(session, tmp_path)
        tts_stage(session, local_date="2026-10-06", device_id=device_id, data_root=tmp_path,
                  engines=[FakeEngine()], ffmpeg_bin=ffmpeg, now=now)  # fmt: skip

        def fake_transcribe(voice: Path, work: Path) -> list[TimedWord]:
            assert voice.is_file()
            raise CaptionsError("whisper unavailable in this test")

        kw = {"local_date": "2026-10-06", "device_id": device_id, "data_root": tmp_path,
              "transcribe": fake_transcribe, "now": now}  # fmt: skip
        report = captions_stage(session, **kw)  # type: ignore[arg-type]
        assert not report.used_whisper and not report.skipped
        reel = session.exec(select(Reel)).one()
        assert reel.status == "captions"
        asset = session.exec(select(ReelAsset).where(ReelAsset.kind == "captions_json")).one()
        doc = json.loads((tmp_path / "media" / asset.path).read_text())
        assert len(doc["scenes"]) == 8
        again = captions_stage(session, **kw)  # type: ignore[arg-type]
        assert again.skipped


# --- real whisper (local only) --------------------------------------------------------------------


@pytest.mark.whisper
def test_real_whisper_covers_95_percent_with_monotone_timings(tmp_path: Path) -> None:
    """AC: on a fixture WAV, >= 95 % of script words matched; timings monotone, no overlap."""
    whisper, ffmpeg = shutil.which("whisper-cli"), shutil.which("ffmpeg")
    model = REPO / "data" / "models" / "whisper" / "ggml-small.en.bin"
    kokoro = REPO / "data" / "models" / "kokoro"
    if not (whisper and ffmpeg and model.exists() and (kokoro / "kokoro-v1.0.onnx").exists()):
        pytest.skip("whisper-cli / ffmpeg / models not available")
    from chronos.content.script.fallback import template_script
    from chronos.media.tts import KokoroEngine, synthesize_scenes
    from tests.content.script.helpers import DAY, candidates

    script = template_script(candidates(6), DAY)
    texts = [(s.index, s.voiceover) for s in script.scenes]
    engine = KokoroEngine(kokoro / "kokoro-v1.0.onnx", kokoro / "voices-v1.0.bin")
    tts = synthesize_scenes(texts, tmp_path / "reel", tmp_path, [engine], ffmpeg)
    spoken = run_whisper(whisper, ffmpeg, model, tts.voice_wav, tmp_path / "reel", tmp_path)
    ranges = [SceneRange(t.scene_index, t.start_sec, t.end_sec) for t in tts.timings]
    res = build_captions([(i, t.split()) for i, t in texts], ranges, spoken)
    print(f"\ncoverage={res.coverage:.3f} ({res.matched}/{res.total})")
    assert res.coverage >= 0.95
    for (index, words), rng in zip(res.scenes, ranges, strict=True):
        assert index == rng.index
        _assert_monotone(words, rng)
