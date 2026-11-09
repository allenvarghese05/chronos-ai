"""Build the renderer's ``ReelProps`` from the day's artefacts (T4.5, docs/07 §4).

Inputs: the validated script, ``tts.json`` (scene timings), ``captions.json`` (word timings,
optional) and the scene images. Paths in the props are relative to the per-render public dir
the render stage stages (``voice.wav``, ``scene-NN.jpg``).

Scene ``startSec`` is the scene's first spoken sample; ``endSec`` is the next scene's start
(so the silence gap belongs to the outgoing scene) and the audio end for the last scene.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from chronos.content.script.schema import ReelScript
from chronos.media.reel_props import Audio, CaptionWord, Media, ReelProps, Scene, SceneSource

ThemeName = Literal["dawn", "mono", "ink"]


def theme_for(local_date: datetime.date, preference: str) -> ThemeName:
    """``auto``: ``dawn`` on weekdays, ``ink`` at weekends (docs/07 §6); else the preference."""
    if preference in ("dawn", "mono", "ink"):
        return preference
    return "ink" if local_date.weekday() >= 5 else "dawn"


def build_reel_props(
    script: ReelScript,
    tts: Sequence[Mapping[str, Any]],
    captions: Mapping[str, Any] | None,
    images: Mapping[int, str],
    *,
    audio_src: str,
    audio_duration: float,
    theme: ThemeName,
    voice_credit: str = "Voice: Kokoro-82M",
) -> ReelProps:
    """Assemble validated ``ReelProps`` (raises ``pydantic.ValidationError`` if inconsistent)."""
    starts = {int(t["scene_index"]): float(t["start_sec"]) for t in tts}
    words_by_scene: dict[int, list[CaptionWord]] = {}
    for scene in cast("list[dict[str, Any]]", (captions or {}).get("scenes", [])):
        words_by_scene[int(scene["index"])] = [
            CaptionWord(w=str(w["w"]), s=float(w["s"]), e=float(w["e"]))
            for w in cast("list[dict[str, Any]]", scene.get("words", []))
            if str(w.get("w", ""))
        ]
    ordered = sorted(s.index for s in script.scenes)
    scenes: list[Scene] = []
    for scene in script.scenes:
        start = starts.get(scene.index, 0.0)
        later = [starts[i] for i in ordered if i > scene.index and i in starts]
        end = later[0] if later else max(audio_duration, start)
        media: Media | None = None
        source: SceneSource | None = None
        if scene.type == "story":
            image = images.get(scene.index)
            media = Media(type="image", url=image) if image else Media(type="gradient")
            if scene.source_name and scene.source_url:
                source = SceneSource(name=scene.source_name, url=scene.source_url)
        scenes.append(
            Scene(
                index=scene.index,
                type=scene.type,
                title=scene.title,
                kicker=scene.kicker,
                body=scene.bullets or None,
                media=media,
                source=source,
                startSec=round(start, 3),
                endSec=round(end, 3),
            )
        )
    names: list[str] = []
    for scene in script.stories:
        if scene.source_name and scene.source_name not in names:
            names.append(scene.source_name)
    return ReelProps(
        localDate=script.local_date.isoformat(),
        greeting=script.greeting,
        tone=script.tone,
        audio=Audio(src=audio_src, durationSec=round(audio_duration, 3)),
        scenes=scenes,
        captions=[words_by_scene.get(s.index, []) for s in script.scenes],
        theme=theme,
        attribution=[*names, voice_credit, "Captions: whisper.cpp"],
    )
