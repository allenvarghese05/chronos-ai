# Media Pipeline — assets, TTS, captions, Remotion render

All stages are jobs (`assets → tts → captions → render`) run serially by the worker for one
`local_date`. Every stage writes into `data/media/<local_date>/<reel_public_id>/`, records
`reel_assets` rows, and is idempotent (skips if its output exists with a matching sha256 of inputs).

## 1. Assets (`media/assets.py`)

For each `story` scene, in order of preference:

1. `items.image_url` from the feed, else resolve `og:image` / `twitter:image` by fetching the
   article HTML (≤ 2 MB, SSRF guard, 10 s).
   If the feed image is a small thumbnail (longest side < 800 px) the og:image is fetched too
   and the larger one wins (T4.1; BBC feed thumbnails are 240 px wide).
2. If `visual == "broll"` and `CHRONOS_PEXELS_API_KEY` is set: Pexels video search with
   `broll_query`, portrait orientation, ≤ 15 s, pick `hd` file ≤ 25 MB; store `licence = pexels`
   and attribution in `reel.json`.
3. Else `visual = "gradient"`: the renderer draws a themed animated background (no file).

Image processing with Pillow: verify it decodes, max 4 096 px, convert to sRGB JPEG q85, resize to
cover 1080×1350, strip all metadata, write `assets/scene-NN.jpg`. Videos: `ffprobe` to verify
codec/duration/size, re-encode to H.264 1080×1920 crop-to-fill 30 fps with `-an` (no audio) to
`assets/scene-NN.mp4` (bounded 60 s timeout).

Cover/outro use a generated thumbnail from the first story image blurred + date typography (done
inside Remotion; a `thumb.jpg` is rendered as a still for the app feed).

## 2. TTS (`media/tts.py`)

- **Kokoro-82M** through `kokoro-onnx` (ONNX Runtime, CoreML EP when available, CPU fallback).
  Files under `data/models/kokoro/` fetched by `just models` with pinned URLs + sha256.
  Voice: `af_heart` (default, configurable `CHRONOS_TTS_VOICE`), **speed 1.15** (`CHRONOS_TTS_SPEED`),
  24 kHz. Measured 2026-11-09: `af_heart` speaks ~142 wpm at 1.0, so 1.15 (~163 wpm) keeps the
  word budget near 60 s; the 6-story template reel (169 words) renders 59.6 s.
- One WAV per scene (`voice/scene-NN.wav`) so per-scene durations are known exactly; 350 ms of
  silence between scenes; concatenated with `pydub`/numpy to `voice.wav`.
- Loudness: `ffmpeg -af loudnorm=I=-16:TP=-1.5:LRA=11` → `voice.mp3` (AAC in the final mux, mp3
  kept for the app's audio-only preview). Timings are taken from the normalised file.
- Fallback **edge-tts** (`en-US-AriaNeural`) when Kokoro raises or the output is < 1 s; recorded in
  `reel_assets.licence = edge_tts`.
- Output: `tts.json` = `[ { scene_index, start_sec, end_sec, words } ]` (coarse timing per scene,
  used if captions fail).

## 3. Captions (`media/captions.py`)

- `whisper-cli` (whisper.cpp, Homebrew `whisper-cpp`, version pinned in `justfile`) on
  `voice.wav` (16 kHz mono resampled copy): `whisper-cli -m data/models/whisper/ggml-small.en.bin
  -f voice16k.wav -oj -ojf -ml 1 -sow -of captions_raw` → JSON with per-token/word timestamps.
- Alignment: words from Whisper are matched to the script's voiceover words per scene using the
  per-scene time ranges from `tts.json` as anchors plus a Needleman–Wunsch alignment on normalised
  tokens, so the on-screen caption text is always the **script's** words (correct spelling,
  numbers, names) with Whisper's **timings**. Unmatched script words get interpolated times.
- Output `captions.json`:
  ```json
  { "scenes": [ { "index": 1, "words": [ { "w": "Qwen3-VL", "s": 4.31, "e": 4.72 }, ... ] } ] }
  ```
- Fallback if whisper fails: proportional timing (character-weighted) inside each scene's TTS range.

## 4. Renderer (`packages/renderer`, Remotion)

### Composition
- `MorningReel`: 1080×1920, 30 fps, `durationInFrames = ceil(audioDuration × 30) + 15`.
- Props = `ReelProps` (zod schema generated from the API):
  ```ts
  { localDate, greeting, tone, audio: { src, durationSec }, scenes: SceneProps[], captions: CaptionWord[][], theme: "dawn" | "mono" | "ink", attribution: string[] }
  ```
- Scene components receive `startFrame/endFrame` and render inside `<Sequence>`:
  - `CoverScene`: date, greeting, animated gradient, subtle grain, "Morning Reel" wordmark.
  - `StoryScene`: kicker chip, headline (fitted type, max 3 lines), bullets staggering in, media
    layer (`Img` with slow Ken Burns, or `OffthreadVideo` b-roll with cover-fit, or gradient),
    source badge at bottom.
  - `OutroScene`: "Saved to your feed · tap to read sources", list of source names.
- `WordCaptions`: karaoke captions, 3–5 words per line, active word highlighted, positioned in
  the lower third above the safe area; uses `captions.json` words; respects `tone` for motion.
- Transitions: `@remotion/transitions` slide/fade, 12 frames.
- Design tokens in `design/tokens.ts` (type scale 1.25, palette per theme, 24 px grid, spring
  configs). Fonts self-hosted (`Inter Tight`, `JetBrains Mono` for the kicker) via `@remotion/fonts`.
- Audio: `<Audio src={staticFile(audio.src)} />`; assets are copied into `public/` for the render
  by the Python runner (Remotion's `staticFile` needs them under the project `public/` dir → the
  runner creates a per-render temp dir `public/renders/<reel_id>/` and deletes it afterwards).

### Render command (from Python, `media/render.py`)
```
npx remotion render src/index.ts MorningReel <out>/reel.mp4 \
  --props=<out>/reel.json --codec=h264 --crf=20 --audio-codec=aac --audio-bitrate=160k \
  --concurrency=2 --timeout=120000 --log=verbose --disable-web-security=false
```
- `cwd = packages/renderer`, timeout 900 s, `caffeinate -i` wrapper, env allowlist
  (`PATH`, `HOME`, `NODE_OPTIONS=--max-old-space-size=2048`, `REMOTION_*`).
- Thumbnail: `npx remotion still ... --frame=45 thumb.jpg`.
- Verification with `ffprobe -print_format json -show_streams -show_format`: h264 1080×1920,
  30 fps, aac, duration within ±0.5 s of audio; sha256 and size stored.
- Chrome: Remotion downloads a pinned headless shell on first run into the project; `just models`
  triggers `npx remotion browser ensure` so the first morning is not slow.

### CI smoke
`pnpm exec remotion still src/index.ts MorningReel out/smoke.png --props=src/fixtures/sample-reel.json --frame=60`
on `ubuntu-latest` (headless shell is downloaded by Remotion; cached). Fails the `renderer` job if
the still cannot be produced. Vitest covers scene timing maths and caption line-breaking.

## 5. Timing budget on M1 Pro (targets, measured in T4.5)

| Stage | Target | Notes |
|---|---|---|
| ingest | < 20 s | parallel fetch, 8 sources |
| script (Ollama 7B) | 40–90 s | ~700 output tokens; model load 5–10 s |
| assets | < 30 s | ≤ 6 images, ≤ 2 b-roll clips |
| tts (Kokoro) | 10–20 s | ~170 words |
| captions (whisper small.en, Metal) | 5–10 s | 60 s audio |
| render (Remotion, concurrency 2) | 90–180 s | 1 850 frames with video layers |
| **total** | **≈ 3–6 min** | well inside the 30-min prewarm lead |

## 6. Design direction for the reel

- Vertical, generous margins (96 px side, 220 px top/bottom safe area for the iOS player chrome).
- One idea per screen: kicker → headline → at most three bullets; captions are the voiceover, not
  the bullets, so the two never fight.
- Three themes selectable per day in config (`dawn` warm gradient, `mono` near-black with
  one accent, `ink` paper-white); default `dawn` on weekdays, `ink` on weekends.
- Motion: slow (≥ 8 s) Ken Burns on stills, spring-in for text, no bounces; captions pop by
  weight (700 vs 500), not by scale.
- Legibility rules enforced in code: headline ≥ 64 px, bullets ≥ 40 px, captions ≥ 52 px,
  contrast ≥ 4.5:1 against a scrim over media.
