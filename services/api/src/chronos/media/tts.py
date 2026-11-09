"""TTS stage (T4.2, docs/07 §2): voice per scene, concatenation, loudness, timings.

- **Kokoro-82M** via ``kokoro-onnx`` (voice ``af_heart``, speed 1.15 ~ 163 wpm), 24 kHz mono.
- One WAV per scene so per-scene timings are exact; 350 ms of silence between scenes.
- ``ffmpeg loudnorm=I=-16:TP=-1.5:LRA=11`` gives ``voice.wav`` (normalised, used by
  captions and render) and ``voice.mp3`` (app preview). Single-pass loudnorm keeps the length,
  so timings computed from sample counts hold for the normalised file.
- **edge-tts** fallback (network, Microsoft) per scene when Kokoro raises or returns under
  1 s of audio; recorded as ``licence = edge_tts``.
- ``tts.json`` = ``[{scene_index, start_sec, end_sec, words}]`` (coarse timing per scene, the
  captions fallback).
"""

from __future__ import annotations

import io
import json
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import structlog
from numpy.typing import NDArray

from chronos.media.subprocess import run

log = structlog.get_logger(__name__)

SAMPLE_RATE = 24_000
GAP_SEC = 0.35
MIN_SCENE_SEC = 1.0
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
FFMPEG_TIMEOUT_SEC = 120.0

Audio = NDArray[np.float32]


class TtsError(RuntimeError):
    """A TTS engine could not produce usable audio."""


class TtsEngine(Protocol):
    name: str  # recorded as the asset licence: "kokoro" | "edge_tts"

    def synthesize(self, text: str) -> Audio: ...


@dataclass
class KokoroEngine:
    model_path: Path
    voices_path: Path
    voice: str = "af_heart"
    speed: float = 1.15
    name: str = "kokoro"
    _model: object | None = field(default=None, repr=False)

    def synthesize(self, text: str) -> Audio:
        from kokoro_onnx import Kokoro

        if self._model is None:
            if not self.model_path.exists() or not self.voices_path.exists():
                raise TtsError("Kokoro model files missing (run `just models`)")
            self._model = Kokoro(str(self.model_path), str(self.voices_path))
        assert isinstance(self._model, Kokoro)
        samples, rate = self._model.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        audio = np.asarray(samples, dtype=np.float32)
        return audio if rate == SAMPLE_RATE else resample(audio, rate, SAMPLE_RATE)


@dataclass
class EdgeEngine:
    """Online fallback: edge-tts MP3 decoded to 24 kHz mono PCM with ffmpeg."""

    ffmpeg: str
    work_dir: Path
    data_root: Path
    voice: str = "en-US-AriaNeural"
    name: str = "edge_tts"

    def synthesize(self, text: str) -> Audio:
        import asyncio

        import edge_tts

        mp3 = self.work_dir / "edge.mp3"
        try:
            asyncio.run(edge_tts.Communicate(text, self.voice).save(str(mp3)))
        except Exception as exc:  # network / service failure
            raise TtsError(f"edge-tts failed: {type(exc).__name__}") from exc
        wav = self.work_dir / "edge.wav"
        ffmpeg(self.ffmpeg, ["-i", str(mp3), "-ac", "1", "-ar", str(SAMPLE_RATE), str(wav)],
               self.work_dir, self.data_root)  # fmt: skip
        return read_wav(wav)


def resample(audio: Audio, src: int, dst: int) -> Audio:
    """Linear resampling (only used if an engine returns a different rate)."""
    if src == dst or audio.size == 0:
        return audio
    n = round(audio.size * dst / src)
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def write_wav(path: Path, audio: Audio, rate: int = SAMPLE_RATE) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


def read_wav(path: Path) -> Audio:
    with wave.open(str(path), "rb") as src:
        if src.getnchannels() != 1 or src.getsampwidth() != 2:
            raise TtsError(f"unexpected WAV format in {path.name}")
        frames = src.readframes(src.getnframes())
        rate = src.getframerate()
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
    return resample(audio, rate, SAMPLE_RATE)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as src:
        return src.getnframes() / src.getframerate()


def ffmpeg(binary: str, args: Sequence[str], cwd: Path, data_root: Path) -> None:
    result = run(
        [binary, "-hide_banner", "-loglevel", "error", "-y", *args],
        timeout=FFMPEG_TIMEOUT_SEC,
        cwd=cwd,
        data_root=data_root,
    )
    if result.timed_out or result.returncode != 0:
        # raw stderr is never surfaced (media.subprocess contract); exit code only
        raise TtsError(f"ffmpeg failed (exit {result.returncode}, timed_out={result.timed_out})")


@dataclass(frozen=True, slots=True)
class SceneTiming:
    scene_index: int
    start_sec: float
    end_sec: float
    words: int
    engine: str


def concatenate(clips: Sequence[Audio], gap_sec: float = GAP_SEC) -> tuple[Audio, list[float]]:
    """Join clips with silence between them; returns audio and each clip's start time."""
    gap = np.zeros(round(gap_sec * SAMPLE_RATE), dtype=np.float32)
    parts: list[Audio] = []
    starts: list[float] = []
    cursor = 0
    for i, clip in enumerate(clips):
        if i:
            parts.append(gap)
            cursor += gap.size
        starts.append(cursor / SAMPLE_RATE)
        parts.append(clip)
        cursor += clip.size
    joined = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return joined.astype(np.float32), starts


@dataclass(frozen=True, slots=True)
class TtsResult:
    voice_wav: Path
    voice_mp3: Path
    tts_json: Path
    duration_sec: float
    timings: list[SceneTiming]
    engines: set[str]


def synthesize_scenes(
    texts: Sequence[tuple[int, str]],
    out_dir: Path,
    data_root: Path,
    engines: Sequence[TtsEngine],
    ffmpeg_bin: str,
) -> TtsResult:
    """Voice every scene, concatenate, loudness-normalise, write ``tts.json``.

    Raises:
        TtsError: if any scene fails on every engine, or ffmpeg fails.
    """
    voice_dir = out_dir / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Audio] = []
    used: list[str] = []
    for index, text in texts:
        audio: Audio | None = None
        errors: list[str] = []
        for engine in engines:
            try:
                candidate = engine.synthesize(text)
            except Exception as exc:  # engines are external code (ONNX, network)
                errors.append(f"{engine.name}: {type(exc).__name__}: {exc}"[:200])
                continue
            if candidate.size < MIN_SCENE_SEC * SAMPLE_RATE:
                errors.append(f"{engine.name}: under {MIN_SCENE_SEC}s of audio")
                continue
            audio = candidate
            used.append(engine.name)
            break
        if audio is None:
            raise TtsError(f"scene {index}: no engine produced audio ({'; '.join(errors)})")
        if errors:
            log.warning("tts_scene_fallback", scene=index, errors=errors)
        write_wav(voice_dir / f"scene-{index:02d}.wav", audio)
        clips.append(audio)

    joined, starts = concatenate(clips)
    raw = out_dir / "voice_raw.wav"
    write_wav(raw, joined)
    voice_wav = out_dir / "voice.wav"
    voice_mp3 = out_dir / "voice.mp3"
    ffmpeg(ffmpeg_bin, ["-i", str(raw), "-af", LOUDNORM, "-ar", str(SAMPLE_RATE), "-ac", "1",
                        "-c:a", "pcm_s16le", str(voice_wav)], out_dir, data_root)  # fmt: skip
    ffmpeg(ffmpeg_bin, ["-i", str(voice_wav), "-c:a", "libmp3lame", "-b:a", "128k",
                        str(voice_mp3)], out_dir, data_root)  # fmt: skip
    raw.unlink(missing_ok=True)

    timings = [
        SceneTiming(index, round(start, 3), round(start + clip.size / SAMPLE_RATE, 3),
                    len(text.split()), engine)
        for (index, text), start, clip, engine in zip(texts, starts, clips, used, strict=True)
    ]  # fmt: skip
    tts_json = out_dir / "tts.json"
    tts_json.write_text(
        json.dumps(
            [{"scene_index": t.scene_index, "start_sec": t.start_sec, "end_sec": t.end_sec,
              "words": t.words} for t in timings],
            indent=1,
        ),
        encoding="utf-8",
    )  # fmt: skip
    return TtsResult(voice_wav, voice_mp3, tts_json, wav_duration(voice_wav), timings, set(used))


def silence_like(text: str, seconds_per_word: float = 0.37) -> Audio:
    """Deterministic stand-in audio (tests / fake engine): a quiet tone of plausible length."""
    n = max(1, len(text.split()))
    samples = round(n * seconds_per_word * SAMPLE_RATE)
    t = np.arange(samples, dtype=np.float32) / SAMPLE_RATE
    return (0.1 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


@dataclass
class FakeEngine:
    name: str = "kokoro"
    fail: bool = False
    calls: list[str] = field(default_factory=list[str])

    def synthesize(self, text: str) -> Audio:
        self.calls.append(text)
        if self.fail:
            raise TtsError("fake failure")
        return silence_like(text)


def wav_bytes(audio: Audio) -> bytes:  # small helper for tests
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()
