---
description: Phase 4 - push the approved branch and open the PR (pusher, cheap model)
argument-hint: <task-id>
---
Task: $1

Precondition: the last reviewer verdict in this conversation for $1 is APPROVE. If it is not, stop and say so.

Use the `subagent` tool with `agent: "pusher"`, `agentScope: "both"` and this task:
"Task $1. Reviewer verdict: <paste the full APPROVE verdict text including MINOR findings>. Implementer summary: <paste the implementer's final summary>. Follow your procedure: verify, run checks, push, open the PR with gh."

Show me the PR URL and the CI status. Never merge.
