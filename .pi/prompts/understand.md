---
description: Phase 1 - understand a task fully, no code (understander, cheap model)
argument-hint: <task-id, e.g. T1.5>
---
Task: $1

Use the `subagent` tool with `agent: "understander"`, `agentScope: "both"` and this task:
"Write docs/plans/$1-brief.md for task $1 following your instructions. Task row is in docs/11-implementation-plan.md."

Do not design, plan or write code in this phase yourself.

When the understander returns, show me:
1. A summary of the brief in at most 10 lines.
2. The proposed approach, verbatim.
3. Every open question, numbered, exactly as written.

Then stop and wait. I will answer the questions, change the approach, or say "approved".
When I answer, append my answers to the end of the brief under "## Owner answers (<today's date>)",
numbered to match, then stop again.
