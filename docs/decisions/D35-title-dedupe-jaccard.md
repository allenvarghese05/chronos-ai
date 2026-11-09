# D35: Near-duplicate headlines by word-set Jaccard, not simhash distance

- Date: 2026-11-08, Status: accepted, Task: T3.3
- Owner chose this during T3.3 implementation after a calibration test showed the docs/06 rule
  failing.

| ID | Decision | Choice | Why | Revisit if |
|---|---|---|---|---|
| D35 | Title near-duplicate rule (supersedes "simhash Hamming ≤ 6" in docs/06 §2.3) | Two items are title duplicates when both titles have ≥ 4 content tokens (NFKC-casefolded `\w+` minus a small English stopword list) **and** the Jaccard similarity of those token sets is ≥ 0.6, **and** they were published within 36 h of each other. A shared URL dedupe hash still merges unconditionally. `items.title_simhash` is still computed and stored. | On 6–10-word headlines, 64-bit word simhash is too unstable. Rewordings of one story scored 6–20 bits apart, and different stories scored as close as 9, so no threshold separates them. The Jaccard rule separated the whole calibration set: same story ≥ 0.64, different stories ≤ 0.56. | Live mornings show missed merges or wrong merges; tune `TITLE_JACCARD_MIN`, or add the calibration pair to the tests first. |

The calibration pairs live in `services/api/tests/content/test_normalise.py`
(`_SAME_STORY`, `_DIFFERENT`). Any change to the stopword list or the threshold must keep them
passing, and new real-world misses should be added there.
