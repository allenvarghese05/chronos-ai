"""Captions stage (T4.3, docs/07 §3): the script's words with whisper.cpp's timings.

1. ``whisper-cli`` (``ggml-small.en``) on a 16 kHz mono copy of ``voice.wav`` with
   ``-oj -ojf -ml 1 -sow`` gives one segment per spoken word with millisecond offsets.
2. Per scene, the whisper words whose midpoint falls inside the scene's ``tts.json`` range
   are aligned to the scene's script words with Needleman-Wunsch on normalised tokens. The
   on-screen text is always the **script's** words (spelling, names, numbers); only the
   timings come from whisper (it writes "Qwen3-VL" as "QN3VL", "5.4 percent" as "5.4%").
3. Unmatched script words get times interpolated between their matched neighbours
   (character-weighted); a scene with no usable whisper words, or a failed whisper run,
   uses proportional character-weighted timing inside the scene's TTS range.
4. Timings are clamped monotone and non-overlapping inside each scene.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, cast

import structlog

from chronos.media.subprocess import run

log = structlog.get_logger(__name__)

WHISPER_TIMEOUT_SEC = 180.0
SCENE_SLACK_SEC = 0.25
MIN_WORD_SEC = 0.04
MATCH, PARTIAL, MISMATCH, GAP = 2.0, 1.0, -1.0, -1.0
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


class CaptionsError(RuntimeError):
    """whisper-cli could not produce usable output (the stage then uses proportional timing)."""


@dataclass(frozen=True, slots=True)
class TimedWord:
    w: str
    s: float
    e: float


@dataclass(frozen=True, slots=True)
class SceneRange:
    index: int
    start: float
    end: float


def norm(token: str) -> str:
    """Comparable form: NFKC, casefolded, letters and digits only."""
    return _NON_ALNUM.sub("", unicodedata.normalize("NFKC", token).casefold())


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return MISMATCH
    if a == b:
        return MATCH
    if SequenceMatcher(None, a, b).ratio() >= 0.6 or a.startswith(b) or b.startswith(a):
        return PARTIAL
    return MISMATCH


def align(script: Sequence[str], heard: Sequence[str]) -> list[int | None]:
    """Needleman-Wunsch: for each script word, the index of the heard word it matches."""
    a = [norm(w) for w in script]
    b = [norm(w) for w in heard]
    n, m = len(a), len(b)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = i * GAP
    for j in range(1, m + 1):
        score[0][j] = j * GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            score[i][j] = max(
                score[i - 1][j - 1] + similarity(a[i - 1], b[j - 1]),
                score[i - 1][j] + GAP,
                score[i][j - 1] + GAP,
            )
    mapping: list[int | None] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        sim = similarity(a[i - 1], b[j - 1])
        if score[i][j] == score[i - 1][j - 1] + sim:
            if sim > 0:
                mapping[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif score[i][j] == score[i - 1][j] + GAP:
            i -= 1
        else:
            j -= 1
    return mapping


def proportional(words: Sequence[str], start: float, end: float) -> list[TimedWord]:
    """Character-weighted timing of *words* across [start, end]."""
    if not words:
        return []
    weights = [max(1, len(w)) + 1 for w in words]
    total = sum(weights)
    span = max(end - start, MIN_WORD_SEC * len(words))
    out: list[TimedWord] = []
    cursor = start
    for word, weight in zip(words, weights, strict=True):
        dur = span * weight / total
        out.append(TimedWord(word, round(cursor, 3), round(cursor + dur, 3)))
        cursor += dur
    return out


def _clamp(words: list[TimedWord], start: float, end: float) -> list[TimedWord]:
    """Monotone, non-overlapping, inside [start, end], each at least MIN_WORD_SEC long."""
    out: list[TimedWord] = []
    prev_end = start
    for k, tw in enumerate(words):
        remaining = len(words) - k - 1
        latest_start = max(start, end - (remaining + 1) * MIN_WORD_SEC)
        s = min(max(tw.s, prev_end), latest_start)
        e = min(max(tw.e, s + MIN_WORD_SEC), end - remaining * MIN_WORD_SEC)
        e = max(e, s + MIN_WORD_SEC)
        out.append(TimedWord(tw.w, round(s, 3), round(e, 3)))
        prev_end = e
    return out


def time_scene(
    script_words: Sequence[str], heard: Sequence[TimedWord], scene: SceneRange
) -> tuple[list[TimedWord], int]:
    """Timed script words for one scene and how many were matched to whisper words."""
    lo, hi = scene.start - SCENE_SLACK_SEC, scene.end + SCENE_SLACK_SEC
    local = [h for h in heard if lo <= (h.s + h.e) / 2 <= hi]
    if not script_words:
        return [], 0
    if not local:
        return proportional(script_words, scene.start, scene.end), 0
    mapping = align(script_words, [h.w for h in local])
    times: list[tuple[float, float] | None] = [
        (local[j].s, local[j].e) if j is not None else None for j in mapping
    ]
    matched = sum(t is not None for t in times)
    result: list[TimedWord] = []
    k = 0
    n = len(script_words)
    while k < n:
        t = times[k]
        if t is not None:
            result.append(TimedWord(script_words[k], t[0], t[1]))
            k += 1
            continue
        run_end = k
        while run_end < n and times[run_end] is None:
            run_end += 1
        gap_start = result[-1].e if result else scene.start
        nxt = times[run_end] if run_end < n else None
        gap_end = nxt[0] if nxt is not None else scene.end
        result.extend(proportional(script_words[k:run_end], gap_start, max(gap_end, gap_start)))
        k = run_end
    return _clamp(result, scene.start, scene.end), matched


def parse_whisper_json(data: dict[str, Any]) -> list[TimedWord]:
    words: list[TimedWord] = []
    for seg in cast("list[dict[str, Any]]", data.get("transcription", [])):
        text = str(seg.get("text", "")).strip()
        offsets = cast("dict[str, Any]", seg.get("offsets", {}))
        start, end = offsets.get("from"), offsets.get("to")
        if not text or not isinstance(start, int) or not isinstance(end, int):
            continue
        words.append(TimedWord(text, start / 1000.0, end / 1000.0))
    return words


def run_whisper(
    whisper_bin: str, ffmpeg_bin: str, model: Path, voice_wav: Path, work: Path, data_root: Path
) -> list[TimedWord]:
    """Transcribe *voice_wav* to word timings. Raises :class:`CaptionsError`."""
    if not model.exists():
        raise CaptionsError("whisper model missing (run `just models`)")
    wav16 = work / "voice16k.wav"
    for argv, what in (
        ([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-i", str(voice_wav),
          "-ar", "16000", "-ac", "1", str(wav16)], "ffmpeg"),
        ([whisper_bin, "-m", str(model), "-f", str(wav16), "-oj", "-ojf", "-ml", "1", "-sow",
          "-np", "-of", str(work / "captions_raw")], "whisper-cli"),
    ):  # fmt: skip
        try:
            result = run(argv, timeout=WHISPER_TIMEOUT_SEC, cwd=work, data_root=data_root)
        except (OSError, ValueError) as exc:
            raise CaptionsError(f"{what} could not start: {type(exc).__name__}") from exc
        if result.timed_out or result.returncode != 0:
            raise CaptionsError(f"{what} failed (exit {result.returncode})")
    try:
        data = json.loads((work / "captions_raw.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaptionsError("whisper JSON unreadable") from exc
    words = parse_whisper_json(cast("dict[str, Any]", data))
    if not words:
        raise CaptionsError("whisper heard no words")
    return words


@dataclass(frozen=True, slots=True)
class CaptionsResult:
    scenes: list[tuple[int, list[TimedWord]]]
    matched: int
    total: int
    used_whisper: bool

    @property
    def coverage(self) -> float:
        return self.matched / self.total if self.total else 1.0

    def to_json(self) -> str:
        return json.dumps(
            {"scenes": [{"index": i, "words": [{"w": t.w, "s": t.s, "e": t.e} for t in ws]}
                        for i, ws in self.scenes]},
            ensure_ascii=False,
        )  # fmt: skip


def build_captions(
    scene_words: Sequence[tuple[int, list[str]]],
    ranges: Sequence[SceneRange],
    heard: Sequence[TimedWord] | None,
) -> CaptionsResult:
    """Captions for every scene; ``heard=None`` means whisper failed (proportional only)."""
    by_index = {r.index: r for r in ranges}
    scenes: list[tuple[int, list[TimedWord]]] = []
    matched = total = 0
    for index, words in scene_words:
        scene = by_index[index]
        if heard is None:
            timed, hit = proportional(words, scene.start, scene.end), 0
        else:
            timed, hit = time_scene(words, heard, scene)
        scenes.append((index, timed))
        matched += hit
        total += len(words)
    return CaptionsResult(scenes, matched, total, heard is not None)
