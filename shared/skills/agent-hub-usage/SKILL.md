---
name: agent-hub-usage
description: How to use the shared agent-hub memory, task queue and event log. Read this first when connected to the agent-hub MCP server.
---

# Using agent-hub

1. **Start of work:** call `project_context` and `memory_search` with the topic before planning. Do not re-ask the user for decisions already stored as verified memories.
2. **Taking work:** call `task_claim` (your roles decide which tasks you see). Call `task_heartbeat` at least every 10 minutes on long tasks; leases expire after 15 minutes and the task is handed to someone else.
3. **While working:** use the worktree/branch returned by the claim. Never edit the main checkout.
4. **Finishing:** `task_update` with status `review` (developers) or `done`/`failed` (reviewers) and a concrete `result` (what changed, how it was verified).
5. **Learning:** `memory_write` durable facts, decisions and lessons. They land as `draft`; only a reviewer or human promotes them. Write one fact per memory, with the reason.
6. **Never** write secrets to memory. Never mark a task `done` without running its checks.
