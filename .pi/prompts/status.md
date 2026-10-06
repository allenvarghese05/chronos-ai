---
description: Show project status - branch, open PRs, next tasks from the implementation plan
---
Report, briefly:
1. `git status --short`, current branch, `git log --oneline -5`.
2. `gh pr list --state open` (if gh is available).
3. From docs/11-implementation-plan.md and docs/plans/*, list tasks that have a brief, a plan, or neither, and suggest the next 3 tasks by the critical-path order at the end of the plan.
Do not change any file.
