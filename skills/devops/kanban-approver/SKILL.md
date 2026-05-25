---
name: kanban-approver
description: "Default approval guidance for autonomous Kanban approval workers."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, approvals, review]
---

# Kanban Approver

Use this skill when you are asked to approve or reject a Kanban task result.

What to evaluate:
1. Did the worker satisfy the task's stated scope?
2. Is the handoff honest about what was and was not verified?
3. Do the cited tests or commands support the claims being made?
4. Are there blocking correctness, safety, or scope issues?

Decision guidance:
- `approved` when the task appears complete and the evidence matches the claim.
- `rejected` when there is a concrete blocking issue that should send the work back.
- `escalated` when you cannot responsibly decide from the available task-centric context and a human should review it.

When you include `comment`, make it short and concrete. Name the blocking issue or the reason for escalation; do not restate the whole task history.

Always follow the runtime contract from the system prompt: return exactly one JSON object with `decision` and optional `comment`.
