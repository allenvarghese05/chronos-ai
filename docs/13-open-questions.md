# Open questions and assumptions

These are the decisions I made to complete the plan where your answers did not cover a point. Each
is in effect until you say otherwise; changing one is a one-line ADR, not a redesign. Answer by
number.

| # | Assumption in the plan | Alternative | Where it matters |
|---|---|---|---|
| 1 | **Weekend fallback at 14:00** (end of the weekend window), mirroring the weekday 11:00 rule. | No weekend fallback (skip the day if never confirmed). | D07, T1.7 |
| 2 | **Eager render** (D09): the reel is built at window start − 30 min so the push arrives seconds after confirmation. Content is therefore ~30–60 min "old" when you wake. | `on_trigger`: render after confirmation (3–6 min delay, freshest content). Kept as a config toggle. | D09, T3.5 |
| 3 | **Score weights v1** (steps 30, walking 20, unlocked 15+10, charger 15, window steps 5, foreground 40; confirm 70 ×2 ticks, strong 85). | Any other numbers — they are YAML. | `03-wake-engine.md` §4 |
| 4 | **Tick interval 5 min** inside the window (≈ 60 silent pushes on a weekday). iOS may throttle; the design tolerates it. | 3 min (more aggressive, more throttling risk) or 10 min (slower confirm). | D04 |
| 5 | **Ollama model `qwen2.5:7b-instruct`** (q4, ~4.7 GB) for JSON structured output on 16 GB. | `llama3.1:8b`, `gemma2:9b`, or a 3B model if memory pressure shows. | D11 |
| 6 | **Pexels b-roll is opt-in** (needs a free API key); without it, images + animated gradients. | Skip b-roll entirely; or another free provider (Pixabay). | D27, T4.1 |
| 7 | **mkcert HTTPS on the LAN** with the root CA installed on the phone (one-time 2-minute step). | Plain HTTP on LAN with an ATS exception (simpler, token visible to anyone sniffing your Wi-Fi) — not recommended. | D16 |
| 8 | **Apple Developer Program ($99/yr)** is assumed for a 1-year provisioning profile and stable APNs. A free account works but the app expires every 7 days and must be re-installed from Xcode. | Free account + weekly reinstall. | T0.4, T2.4 |
| 9 | **Signed commits NOT yet required** on `main`: commit signing is not configured on the laptop (2026-10-06). Once you run `git config --global gpg.format ssh && git config --global user.signingkey ~/.ssh/<key>.pub && git config --global commit.gpgsign true` and add the key to GitHub as a signing key, add `{ "type": "required_signatures" }` back to `scripts/github-ruleset.json` and update the ruleset. | Keep unsigned. | D20, T0.2 |
| 10 | **Private GitHub repo `chronos-ai`** (owner decision 2026-10-06). Rulesets/required checks are **not available** on a private repo on the free plan, so `main` has no server-side protection; the agents' never-push-to-main rule and CI on PRs are the controls. `scripts/github-ruleset.json` is kept for when the repo goes public or the plan changes. | Public repo (enables the ruleset). | T0.2, D20 |
| 11 | **Laptop stays open/awake-capable at night** and on power; `pmset repeat` wakes it at 05:25/07:25. If the lid is closed without an external display, scheduled wake does not run and the morning falls back to "pipeline runs when the laptop wakes". | Leave the laptop on always (`caffeinate -s` service) or use a Mac mini later. | T0.6 |
| 12 | **Expo SDK / Remotion / Kokoro versions** are pinned at the time each scaffold task runs (latest stable), recorded in the task plan and README. | Pin now (would be stale by implementation time). | T0.4, T0.5, T4.2 |
| 13 | **Captions language English only** (`ggml-small.en`). | Multilingual model if India sources add Hindi terms that matter. | D13 |
| 14 | **Reel structure**: cover + 6 stories (2 AI, 1 tech, 2 world, 1 India) + outro ≈ 60 s. | Different mix or 4 longer stories. | `06-content-pipeline.md` selection |
| 15 | **Themes**: `dawn` on weekdays, `ink` on weekends, switchable in settings. | One fixed theme. | `07-media-pipeline.md` §6 |
| 16 | **Subagent models**: understander `claude-haiku-4-5`, implementer `claude-sonnet-4-6`, reviewer `claude-opus-4-6`, pusher `claude-haiku-4-5`. | Any model in `pi --list-models`; edit the `model:` line in `.pi/agents/*.md`. | `.pi/agents/` |
| 17 | **Debug "actual wake time" annotation** is manual (you tap a time). | Infer from first `foreground` event of the day. | T1.8, T2.5 |
| 18 | **Data retention**: telemetry 30 d, items 14 d, days/briefs 90 d, unsaved reels 24 h. | Longer for tuning history. | D24 |
