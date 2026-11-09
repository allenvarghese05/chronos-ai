# D37: One LLM call per story; code assembles the script

- Date: 2026-11-09, Status: accepted, Task: T3.4
- The owner chose this after real-model tests of `qwen2.5:7b-instruct` (2026-11-08/09).

| ID | Decision | Choice | Why | Revisit if |
|---|---|---|---|---|
| D37 | How the LLM produces the reel script (supersedes "the LLM returns the whole `ReelScript`" in docs/06 §4–5) | One call per story returns only `{title, bullets, voiceover}`; one call returns `{greeting, cover_voiceover, outro_voiceover}`. Code assembles the `ReelScript`: scene order, cover/outro, `item_id`, `source_url`, `source_name` and the kicker (from the category). Each piece is retried with its own problems; a piece that fails on every provider is filled from the template, so the rest of the reel keeps the model's writing. Story voiceover is 8 to `min(45, (185 − 40)/n)` words (24 for 6 stories); cover/outro 6–20 words; total budget `8n + 12` to 185 words. The prompt says the source is where a story was *reported*, not who did it. | Asked for the whole script, the 7B model failed structural rules on every attempt (dropped ids/URLs, then put an id on the cover and misplaced scenes). Per story it returned the right shape 100 % of the time. The old 25-word story minimum forced padding on headline-only stories (HN, live blogs); the model correctly refused to add facts and failed the check instead. | Pieces are patched from the template on more than ~1 story a day in normal use; then a bigger local model (14B) or the opt-in cloud fallback. |

## Reel length (spoken at Kokoro's ~165 wpm, plus 0.35 s between scenes)

| Day | Words | Duration |
|---|---|---|
| Old rule (25–45 words per story) | 130–185 | ~50–70 s |
| Every story headline-only (worst case) | ~60–85 | ~25–33 s |
| Typical (summaries on most stories) | ~120–150 | ~46–57 s |
| Maximum | 185 | ~70 s |

## Evidence

- Whole-script attempts: 3/3 invalid (structure), each 35–50 s.
- Per story on live feeds (12 stories): format valid 12/12 at ~4 s per call; failures were only the old length minimum.
- Per story, fixture day (headline-only), after this decision: 7/7 pieces valid first try, 24 s total, correct attribution.

The validator (`validate_script`) still checks the assembled script: candidate set, exact URLs, order, budget and deny list.
