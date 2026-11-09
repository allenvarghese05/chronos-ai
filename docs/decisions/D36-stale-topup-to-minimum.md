# D36: Stale stories top a category up to its minimum

- Date: 2026-11-08, Status: accepted, Task: T3.3
- Owner chose this after the T3.3 live smoke run on a Sunday selected only 1 AI story.

| ID | Decision | Choice | Why | Revisit if |
|---|---|---|---|---|
| D36 | Stale-item rescue (refines "ignored unless a category would be empty" in docs/06 §1) | A category with a `category_min` takes its fresh stories first, then tops up with its best stale stories (by score, quotas apply) until the minimum is met. A category **without** a minimum still gets at most one stale story, and only when it has no fresh story at all. Stale stories never fill spare slots otherwise. | HF daily papers and arXiv do not publish at weekends, so on Saturday, Sunday and Monday mornings every AI item is older than `freshness_hours` (20 h). The literal rule rescued only one AI story although `category_min.ai = 2`. | Weekend AI picks turn out too old to be worth hearing; then add a per-category freshness override instead. |

Covered by `tests/content/test_rank.py::test_weekend_minimum_topped_up_from_stale` and the
`test_select_meets_minimums_property` / `test_select_exact_total` properties.
