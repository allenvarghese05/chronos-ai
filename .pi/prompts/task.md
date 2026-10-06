---
description: Run the whole pipeline for a task with gates - understand -> implement -> review -> push
argument-hint: <task-id>
---
Task: $1

Run the four phases in order, stopping at each gate exactly as the individual prompts describe:

1. Phase 1 as in /understand $1. Stop after showing the brief summary, proposed approach and open
   questions. Wait for my answers or "approved" and record them in the brief.
2. Phase 2 as in /implement $1 (create the branch from origin/main if needed). Stop after the
   summary.
3. Phase 3 as in /review $1 with the bounded fix loop. Stop on APPROVE or after two failed rounds.
4. Phase 4 as in /push $1 only after I say "push" (or "ship").

You are the orchestrator: delegate every phase with the `subagent` tool and `agentScope: "both"`;
do not do the subagents' work yourself; never skip phase 1; never merge.
