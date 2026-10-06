---
description: Phase 3 - independent review with a bounded fix loop (reviewer strongest model, implementer fixes)
argument-hint: <task-id>
---
Task: $1

Use the `subagent` tool with `agent: "reviewer"`, `agentScope: "both"` and this task:
"Review the current branch for task $1 against docs/plans/$1-brief.md and docs/plans/$1-plan.md. Return APPROVE or CHANGES NEEDED with BLOCKING/MINOR findings."

If the verdict is CHANGES NEEDED:
- If any finding is marked `(plan issue)`, stop and show me those findings. Do not send them to the implementer.
- Otherwise send only the BLOCKING findings to the `implementer` subagent (agentScope "both") with the task:
  "Fix these BLOCKING review findings on the current branch for task $1, with tests, committing per fix. Do not change anything else: <findings>".
- Then run the reviewer again. Do this at most twice. If still CHANGES NEEDED, stop and show me everything.

When the verdict is APPROVE, show me the verdict, the MINOR findings and recommended follow-ups,
and stop. I will run /push (or say "push" and I will run it for you).
