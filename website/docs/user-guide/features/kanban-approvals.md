---
sidebar_position: 13
title: "Kanban task approvals"
description: "Formal human and agent approval gates for Hermes Kanban tasks"
---

# Kanban task approvals

Task approvals let you attach one or more durable review gates to a Kanban task.
A gate can be owned by a **human** or by another **agent profile**. Approvals
provide an alternative to using the `blocked` column for human reviews, by
consolidating human and agent reviews/approvals into a single status with clear
transition rules.

If you have not read the [Kanban overview](./kanban) yet, start there first —
this page assumes you already know what a task, assignee, dispatcher, comment,
and run are.

## Core principles

- **Approvals are a task-level contract and are enforced by the orchestrator, not by the worker.**
  - **The task worker CANNOT request approvals.**
  - Contrary to the `kanban_block` pattern, requested approvals are explicitly stored for each task.
  - Approvals gate the task automatically on worker _completion_, parking it in a dedicated `approval` status.
  - From the perspective of the worker, the task was completed.
  - This provides:
    - Clear separation of skills and context between orchestrator, worker, approver.
    - Clear visibility over which approval gates are currently active and in what status.
    - Full control over adding/removing/reassigning approval gates, even after the task has started.
- **Approvals are structured decisions.**
  - Approvers MUST explicitly choose one of 3 defined decisions:
    - `approved`: Task is moved to `done` automatically, unblocking children.
    - `rejected`: Task is moved back to `todo` automatically for iteration.
    - `escalated`: Task remains parked, human approval is requested (this decision is available to agents only).
  - **Additionally, each approval can be linked to one task comment.**
  - All decisions are auditable through task events and approval run history.
- **Agent approval runs are fully independent from workers.**
  - **There may be any number of approvers for a same task.**
  - **Approver agents can be assigned a profile and skill different from the worker's.**
  - Approver agents are spawned separately from task workers, in their own concurrency pool (default max of 2).
  - Approval runs are given a dedicated system prompt and restricted tool access.
  - Approval runs are stored in a separate table from task runs and emit separate events.
  - Approval runs are bound to a specific task run, reviewing that run's output only.
- **Human and agent approvers co-exist under the same `approval` status.**
  - Arbitrarily mixing approver types is the intended workflow.
  - **Human decision always bypasses agents.**
  - Approver agents are encouraged to escalate to human review if uncertain.
  - Failing agent approvers automatically escalated to human review.
- **Approval policy lives in approver skills and orchestrator instructions.**
  - The infrastructure is not opinionated towards a particular workflow. It equally covers:
    - Human-only approvals: explicitly request it
    - Agent-only approvals: explicitly discourage/forbid escalation in approver skill
    - Specialized approvals: request multiple approvals each with a dedicated skill
    - No approvals: don't request them, task will move to `done` normally
  - The built-in approver system prompt only includes the handoff contract.
  - Behavior (e.g. how strict to enforce nits, how often to escalate) is in the approver skill.
  - **The generic `kanban-approver` skill is fully replaced if a custom skill is assigned.**

## Approval types

### Human approval

Human approvals are resolved manually by an operator:

- `hermes kanban approval approve <approval_id> [--comment <comment>]`
- `hermes kanban approval reject <approval_id> [--comment <comment>]`

Request a human approval when you want to guarantee the opportunity to manually inspect a task's result before it's moved to `done`.

Currently there can only be one human approval row per task.

### Agent approval

Agent approvers are spawned automatically when the task transitions to `approval`.
Each approver is assigned one Hermes profile and up to one skill.
If no skill is specified, a generic `kanban-approver` skill is loaded, allowing the feature to be useful without much setup.
This allows specialized approvers (security-reviewer, proofreader, etc.) to be created either at separate profiles, or as skills within a same profile, based on user's preferences.

- Approvers are spawned by the dispatcher, right after workers.
- Approvers have a separate concurrency pool (default 2), so they can't starve workers.
- Approvers run in the same workspace as each other and as the worker. _(however note that `scratch` gets cleared on worker completion)_
- Failing approvers are automatically retried. After 2 retries, the approval is marked as `failed` and human review is automatically requested.
- Approver processes are reclaimed eagerly once obsolete (e.g. task is archived). Such approvals are marked as `cancelled`.

## Walkthrough

### When tasks enter `approval`

Requesting an approval does **NOT** immediately move a task into the `approval` state.
Approvals are an automatic completion gate:

1. a task is created and worked like any other Kanban task
2. the assignee completes the task (i.e. a "completed" run)
3. Hermes checks the attached approval rows
4. if any approval row exists, the task moves to `approval` instead of `done`, emitting `awaiting_approval` task event
5. all approvals are reset to `requested`, so a fresh set of approvals is always required for each task run
6. approver agents are spawned automatically in parallel
7. once approved, the task finally moves to `done`, emitting the same event `completed` it would do on normal completion
8. otherwise, it's moved back to `todo` and the loop continues

This keeps planning/execution statuses (`todo`, `ready`, `running`, `blocked`) separate from review-time state.
In particular, `blocked` still solely represents "a problem appeared while working on the task".

### When tasks leave `approval`

When a task is in `approval`, the task's next state is determined from the attached approval rows in this order:

1. any human `rejected` → task returns to `todo`
2. any human `approved` → task moves to `done`
3. any human `requested` → task stays in `approval`
4. any agent `running` → task stays in `approval`
5. any agent `rejected` → task returns to `todo`
6. any agent `requested` → task stays in `approval`
7. otherwise → task moves to `done`

Important properties:
- As long as any human approval row exists, it has complete authority over any agent.
- Upon human approval or rejection, still-running agents are immediately cancelled.
- Parallel agents are all allowed to complete before automatically transitioning, in order to gather all feedback.
- Rejection has priority over approval.

## CLI commands

The same commands are also available through chat via `/kanban approval ...`.

### Requesting approvals

Approvals are attached through the `hermes kanban approval` namespace.

```bash
# Human gate
hermes kanban approval request t_abc --human

# Agent gate
hermes kanban approval request t_abc --agent reviewer

# Agent gate with an explicit skill
hermes kanban approval request t_abc --agent reviewer --skill security-review
```

- The command prevents creating duplicate approvals (human, or same agent profile/skill pair).
- If that approver already made a decision, it will **NOT** be reset - see 'Resetting approvals' command below.

### Inspecting approvals

```bash
# Default: grouped by task, hiding done/archived parent tasks
hermes kanban approval ls

# `list` remains an alias
hermes kanban approval list

# Flat one-row-per-approval view
hermes kanban approval ls --flat

# Inspect one task (positional task id; implies `--flat` and includes done/archived parents)
hermes kanban approval ls t_abc

# Inspect run history for one approval row
hermes kanban approval runs 42

# Restrict by approver kind or approval status
hermes kanban approval ls --human
hermes kanban approval ls --agent
hermes kanban approval ls --status approved

# Include done/archived parents, or only tasks currently awaiting approval
hermes kanban approval ls --all
hermes kanban approval ls --active
```

- Default output is grouped by parent task.
- Passing a task id positionally (for example `approval ls t_abc`) implies `--flat` and errors if the task does not exist.
- `approval runs <approval_id>` shows the execution history for a single approval row.
- `--all` and `--active` are mutually exclusive.
- The approval ID is an integer auto-increment. It is required for follow-up commands such as `runs`, `remove`, `reset`, and `reclaim`.

### Completing human review

The comment is optional.
If specified, it is added to the task thread and linked to the approval for bookkeeping.

`approve` and `reject` accept either an approval id or a task id.
When given a task id, Hermes resolves that task's human approval row, creating it first if the task is already in `approval` and no human row exists yet.

```bash
hermes kanban approval approve 6 --comment "LGTM"
hermes kanban approval reject 6 --comment "Needs stronger tests"

# Equivalent task-id forms
hermes kanban approval approve t_abc --comment "LGTM"
hermes kanban approval reject t_abc --comment "Needs stronger tests"
```

- **If any agents are currently running, they are immediately cancelled.**
  - If you wish agents to still produce comments (without impacting the decision), wait for them to finish first.
  - If you wish agents to make the decision, unassign human review - see 'Unassigning approvals' below.
- **It is NOT possible to forcefully set an agent's decision.**
  - To bypass an agent's decision, unassign it, or issue a human decision.

### Resetting approvals

This resets the approval row back to `requested` from any other state.

```bash
hermes kanban approval reset 6
```

- If the agent is currently running, that process is killed.
- **If an agent approval is reset while the task is in `approval` status, it becomes immediately available for dispatch again.**
  - This is the primary way to re-run `failed` or `cancelled` approvers.
  - If you wish the agent not to run again, unassign it instead.

### Unassigning approvals

This removes the approval row completely, as if it hadn't been requested (existing task events/logs remain).

```bash
hermes kanban approval remove 6
```

- If the agent is currently running, that process is killed.
- **If a human unassigns themselves after escalation, all `escalated` approvers are automatically re-requested.**
  - To allow the task to move to `done` after escalation, explicitly approve it instead.

### Cancelling running approvers

This cancels a running agent without fully removing the row (e.g. if the current run is poisoned but you would like to retry it later). This marks the approval row as `cancelled`, so it will not participate in decision aggregation, and will not re-run again. To re-run the approver later, reset it - see 'Resetting approvals' command above.

```bash
hermes kanban approval reclaim 6
```

## Events

- `awaiting_approval`: A task was moved to `approval` status.
- `approval_requested`: A new approval row was added or reset back to requested.
  - This event IS NOT emitted if the row was already in requested status.
  - This event IS emitted when agent approval escalates into human review.
- `approval_claimed`: An agent approval run was claimed.
- `approval_spawned`: An agent approval run process was spawned.
- `approval_failed`: An agent approval run failed.
- `approval_cancelled`: An agent approval run was reclaimed.
- `approval_decided`: An approver made a decision (`approved` | `rejected` | `escalated`).
- `completed`: A task was moved to `done`.
  - This event is emitted either immediately upon worker completion (if no approval rows exist) or once approved.
  - Thus each task will still emit this event once, regardless of whether it goes through approval or not.
  - The payload is the same in either case (e.g. including output artifacts).

**Note that the worker run is still marked as `outcome=completed` even if the task transitions to `approval` instead of `done`.**
From the perspective of the worker, the run was completed successfully.

## Related docs

- [Kanban overview](./kanban) — the main board reference
- [Kanban tutorial](./kanban-tutorial) — workflow walkthrough with screenshots
- [Kanban worker lanes](./kanban-worker-lanes) — worker/reviewer lane contracts
- [CLI Commands Reference](/docs/reference/cli-commands#hermes-kanban) — shell command surface
- [Slash Commands Reference](/docs/reference/slash-commands) — `/kanban ...` in chat
