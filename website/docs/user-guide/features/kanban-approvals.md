---
sidebar_position: 13
title: "Kanban task approvals"
description: "Human and agent approval gates for Hermes Kanban tasks"
---

# Kanban task approvals

Task approvals let you attach one or more durable review gates to a Kanban task.
A gate can be owned by a **human** or by another **agent profile**. Unlike
transient chat prompts, approvals live in the Kanban DB, survive restarts, and
remain visible in task history.

If you have not read the [Kanban overview](./kanban) yet, start there first —
this page assumes you already know what a task, assignee, dispatcher, comment,
and run are.

## What approvals are for

Approvals are the mechanism Hermes uses when "the work is implemented" is not
the same thing as "the task is done." Typical cases:

- a human must sign off before a task can close
- a reviewer profile should inspect code, copy, or deployment state
- a team wants a durable review loop instead of an ad-hoc comment convention
- an operator wants an explicit reopen / retry cycle after rejection

Approvals are attached to an existing task. They do **not** create a separate
peer task or replace the task's assignee. The assignee still does the work;
approvers decide whether the task may finally land in `done`.

## When tasks enter `approval`

Requesting an approval does **not** immediately move a task into the `approval`
state. Approvals are a **completion gate**:

1. a task is created and worked like any other Kanban task
2. the assignee reaches the point where it would normally complete the task
3. Hermes checks the attached approval rows
4. if a decision is still outstanding, the task moves to `approval` instead of
   `done`

This keeps planning/execution statuses (`todo`, `ready`, `running`, `blocked`)
separate from review-time state.

## Approval types

### Human approval

A human approval is resolved manually by an operator:

- `hermes kanban approval approve <task_id>`
- `hermes kanban approval reject <task_id>`

Use this when the final decision must be person-owned — for example, product
sign-off, security sign-off, or "do we actually want to ship this?"

### Agent approval

An agent approval is owned by another Hermes profile. It behaves like a durable
reviewer lane rather than a top-level task reassignment.

Use this when you want review roles such as:

- `reviewer`
- `security-reviewer`
- `release-reviewer`
- `editor`

Agent approvals may also carry an optional skill name so the reviewer lane can
be more specialized.

## Aggregate task-state rules

When a task is in `approval`, Hermes derives the task's next state from the
attached approval rows in this order:

1. any human `rejected` → task returns to `todo`
2. any human `approved` → task moves to `done`
3. any human approval still pending/present → task stays in `approval`
4. any agent `running` → task stays in `approval`
5. any agent `rejected` → task returns to `todo`
6. any agent `requested` → task stays in `approval`
7. otherwise → task moves to `done`

The important consequence is that **human decisions are authoritative over agent
reviews**. A human rejection reopens the task even if another agent already
approved it; a human approval can finish the task even if an agent reviewer is
still attached.

## Requesting approvals

Approvals are attached through the `hermes kanban approval` namespace.

```bash
# Human gate
hermes kanban approval request t_abc --human

# Agent gate
hermes kanban approval request t_abc --agent reviewer

# Agent gate with an explicit skill
hermes kanban approval request t_abc --agent security-reviewer --skill security-review
```

You can inspect the attached rows with:

```bash
hermes kanban approval list --task t_abc
hermes kanban show t_abc
```

The same surface is also available through chat via `/kanban approval ...`.

## Manual human decisions

Human approvals are addressed by **task id**, not by requiring the operator to
look up an approval row id first.

```bash
hermes kanban approval approve t_abc --comment "LGTM"
hermes kanban approval reject t_abc --comment "Needs stronger tests"
```

A comment is optional, but strongly recommended — it becomes part of the task
thread and the next run can read it in `kanban_show()`.

## Reset, reclaim, and archive semantics

These transitions are easy to confuse, so it is worth being explicit.

### Reset / rejection / reopen

When a task leaves `approval` and goes back to active work (for example after a
rejection), Hermes:

1. transitions the task back to `todo`
2. reclaims any currently-running agent approval first
3. resets attached approval rows to `requested`

This means the next completion attempt starts a fresh approval cycle rather than
reusing stale decisions.

### Reclaim

An operator can explicitly reclaim a currently-running **agent** approval:

```bash
hermes kanban approval reclaim <approval_id>
```

Reclaiming ends the live review attempt and immediately recomputes the task's
aggregate approval state. Depending on what other approval rows exist, the task
may remain in `approval`, move to `done`, or return to `todo`.

### Archive

Archiving is preservation-oriented rather than reset-oriented.

When a task is archived:

- running approval workers are reclaimed
- running approval rows become `cancelled`
- already-decided rows are preserved as part of the archived record

So archive keeps the historical review record instead of reopening the loop.

## CLI workflow

```bash
# Request approvals on a task
hermes kanban approval request t_abc --human
hermes kanban approval request t_abc --agent reviewer
hermes kanban approval request t_abc --agent security-reviewer --skill security-review

# Inspect approval state
hermes kanban approval list --task t_abc
hermes kanban show t_abc

# Record a human decision
hermes kanban approval approve t_abc --comment "LGTM"
hermes kanban approval reject t_abc --comment "Needs tests"

# Operator controls
hermes kanban approval reset <approval_id>
hermes kanban approval reclaim <approval_id>
hermes kanban approval remove <approval_id>
```

## Example: human review loop

A typical approval loop looks like this:

1. a worker completes implementation work on `t_abc`
2. the task has a human gate and an agent reviewer attached, so Hermes moves it
   to `approval` instead of `done`
3. the human reviewer rejects the task
4. Hermes returns the task to `todo` and resets the attached approvals to
   `requested`
5. the assignee fixes the issue and completes the task again
6. the human reviewer approves it
7. Hermes moves the task to `done`

## Example: reclaiming an in-flight agent review

A second common flow is operator reclaim of a running reviewer:

1. a task is in `approval`
2. an agent approval is currently `running`
3. the operator decides that review run should stop and calls
   `hermes kanban approval reclaim <approval_id>`
4. Hermes marks the live run reclaimed and recomputes the approval aggregate
5. if that was the last outstanding gate, the task may move to `done`; if other
   rejecting or pending rows remain, the task stays open or returns to `todo`

## Related docs

- [Kanban overview](./kanban) — the main board reference
- [Kanban tutorial](./kanban-tutorial) — workflow walkthrough with screenshots
- [Kanban worker lanes](./kanban-worker-lanes) — worker/reviewer lane contracts
- [CLI Commands Reference](/docs/reference/cli-commands#hermes-kanban) — shell command surface
- [Slash Commands Reference](/docs/reference/slash-commands) — `/kanban ...` in chat
