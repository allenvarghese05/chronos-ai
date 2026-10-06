---
description: Phase 2 - plan and implement an approved brief (implementer, strong model)
argument-hint: <task-id>
---
Task: $1

Preconditions you must check yourself before delegating:
- docs/plans/$1-brief.md exists and has an "## Owner answers" section or the word "approved" from me in this conversation.
- A branch for the task exists and is checked out (`git branch --show-current` starts with the task id in kebab form, e.g. `t1-5-scoring-engine`). If not, create it from an up-to-date `main`: `git fetch origin && git switch -c <branch> origin/main`.

Then use the `subagent` tool with `agent: "implementer"`, `agentScope: "both"` and this task:
"Task $1. Write docs/plans/$1-plan.md from docs/plans/$1-brief.md (including Owner answers), then implement it step by step on the current branch, committing after each step. Follow AGENTS.md."

When it returns, show me: the plan path, steps completed, files changed, tests added, dependencies
added, the `just check` / `just security` summaries, the device checklist if any, and anything it
reported instead of doing. Then stop. I will run /review.
