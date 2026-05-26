# Kanban Task Approvals — Master Spec

Status: Draft (phased rollout; Phase 2 kernel semantics and Phase 3 CLI/manual workflows landed, Phase 4 autonomous approval runtime landed on the branch/PR lane, and Phase 5 semantic/runtime cleanup is specified in `05-approval-event-and-worker-surface-hardening.md`)
Owner: Hermes Kanban
Scope: Kanban DB, dispatcher/runtime, worker prompt contract, and CLI surface. Dashboard UI is out of scope.

---

## 1) Goal

Add first-class task approvals to Hermes Kanban so a task can complete execution, enter an explicit `approval` state, and be gated on one or more approval rows without overloading `blocked` or the existing ad-hoc `review-required` convention.

---

## 2) In-scope behavior

This slice must implement all of the following:

1. A separate approvals table, task-scoped.
2. A separate approval-runs table for agent approval attempts.
3. A new task status: `approval`.
4. Human and agent approvers.
5. Dispatcher-managed spawning of agent approvers.
6. Claim / heartbeat / stale / reclaim handling for approval runs, modelled after task workers.
7. A strict approver output contract; the kernel, not the agent, mutates approval state.
8. Retries for failed agent approval attempts.
9. After 3 consecutive approval-run failures, mark the approval row `failed` and escalate to human review.
10. CLI commands to create, inspect, remove, and decide approvals.
11. DB migration for existing boards.

Out of scope for this slice:

- Dashboard UI.
- Cross-platform gateway buttons / approval UX.
- General-purpose multi-step approval workflows.
- Approval-round versioning.
- Per-approval retention policies beyond parent-task archival/removal cleanup.
- Worker-side direct approval mutation tools.

---

## 3) Core model

### 3.1 Task lifecycle vs approval lifecycle

Task execution state and approval state are distinct.

- `tasks.status` tracks execution lifecycle.
- `task_approvals.status` tracks approval lifecycle.
- `task_approval_runs` tracks spawned agent approval attempts.

A task can have zero, one, or many approval rows.

### 3.1.1 Approval invariants

The kernel must enforce these invariants:

- at most one human approver per task
- at most one agent approver per `(approver_profile, approver_skill)` combination on a task
- approval rows are reset whenever a task moves to a status other than `approval`, `done`, or `archived`

Implications:
- a task cannot accumulate duplicate generic human-review rows
- a task cannot accumulate duplicate agent approval gates for the same profile/skill pair
- approval rows persist across task-state transitions; when a task moves into a status other than `approval`, `done`, or `archived`, those rows are reset to `requested` instead of being deleted

### 3.2 Task statuses

This slice adds `approval` to the task status set.

Meaning:
- `running`: implementation work is still in progress.
- `approval`: implementation work finished successfully, but the task is not yet complete because approval rows exist and are not fully resolved.
- `done`: implementation work finished and all approval gates are satisfied.
- `todo`: implementation work must resume because an approval rejected the result.

`blocked` remains for genuine inability to proceed and is not reused for approvals in this design.

### 3.3 Approval statuses

Approval rows use exactly these statuses:

- `requested`
- `running`
- `approved`
- `rejected`
- `escalated`
- `failed`

Definitions:
- `requested`: approval is pending and not currently claimed by an agent approval run.
- `running`: an agent approval row has been claimed and is currently owned by a live approval run.
- `approved`: this approver approved the task result.
- `rejected`: this approver rejected the task result; the task must return to `todo`.
- `escalated`: this approver intentionally declined to decide and handed the decision to human review.
- `failed`: the approver agent could not produce a valid decision after the retry budget was exhausted; this has the same aggregate effect as `escalated` (human review required), but preserves the distinction between intentional escalation and repeated agent failure.

---

## 4) Aggregate task-state rules

These rules are authoritative. They must live in one kernel helper and be reused by all codepaths that mutate approval state.

### 4.1 Entering `approval`

When a task worker finishes normally and the task has one or more approval rows, the task must transition to `approval` instead of `done`.

The worker does not decide whether approvals are needed at completion time. The kernel checks for attached approval rows.

When this handoff happens, the kernel emits `awaiting_approval` rather than `completed`.

Payload contract:
- `task_status = approval`
- `run_id = <task-worker run id that just completed>`

### 4.2 While any approval is unresolved

A task remains `approval` while any approval row is in `requested` or `running`, with one priority caveat: a live `running` row outranks an already-recorded `rejected` row, while an idle `requested` row does not.

Clarifications:
- `requested` and `running` are the live blocking approval-row states.
- aggregate priority is: any `running` -> `approval`; else any `rejected` -> `todo`; else any `requested` -> `approval`; else `done`.
- `escalated` and `failed` are agent opt-out states that remain present for audit/history but do not independently block `done`.
- when an agent approval escalates or fails, the blocking gate is the human approval row that remains `requested`, not the `escalated` or `failed` row itself.
- approval-run liveness is execution bookkeeping and must not outrank authoritative approval-row state.

### 4.3 Rejection rule

If any approval row becomes `rejected`:

1. Record the rejection and emit approval decision events.
2. Transition the task from `approval` to `todo` as soon as that rejection is authoritatively recorded.
3. In the same transaction as that transition, reset all approval-row statuses back to `requested`.
4. In the same reset transaction, clear approval-row `comment_id` values.
5. In the same reset transaction, clear approval-row consecutive failure counts and last failure errors.
6. Preserve audit history through events and task comments; do not preserve old decision state in the live rows.

Clarifications:
- approval-row state is the business authority.
- a recorded rejection does not move the task back to `todo` until no approval row remains `running`; this lets already-running approvers finish and contribute stacking feedback/comments.
- once no approval row remains `running`, any surviving `rejected` row outranks `requested` rows and immediately drives the rejection cycle.
- stale late worker results are discarded by row/run ownership checks when the row is no longer owned by that run.

This is the only rule in this slice that returns a task from approval back into execution.

### 4.4 Done rule

A task moves from `approval` to `done` only when:
- there is no approval row in `requested`,
- there is no approval row in `running`,
- there is no approval row in `rejected`, and
- every remaining approval row is in `approved`, `escalated`, or `failed`.

Implications:
- if the last approval row is removed while the task is `approval`, the task moves to `done`
- `escalated` and `failed` agent rows do not block `done` by themselves; those rows have opted out of the approval decision
- the blocking gate is the human approval row they assign on top when one exists
- when human approval approves the task, the escalated/failed agent rows remain `escalated` / `failed`
- the aggregate resolver is driven by approval-row state, not by generic approval-run liveness

When the task actually moves to `done`, the kernel emits `completed`.

`completed` is reserved for real movement to `done`, including `approval -> done`. It is not used for the earlier worker handoff into approval.

### 4.5 Approval/task event contract

The canonical task/approval lifecycle events are:

- `awaiting_approval`
- `approval_requested`
- `approval_removed`
- `approval_decided`
- `completed`

Their exact payloads and emission rules are specified in `05-approval-event-and-worker-surface-hardening.md`.

Important rule:
- escalation emits `approval_decided` with `decision = escalated`,
- and also emits `approval_requested` only when the human gate is newly created or reset from a non-`requested` state.

---

## 5) Human escalation behavior

### 5.1 Intentional escalation

One or more agent approvers can return `escalated`. For each such decision, the kernel must:

1. Mark that approval row `escalated`.
2. Ensure at least one human approval row exists for the same task.
3. Reuse an existing human approval row when one already exists; do not create duplicate generic human review rows just because multiple agents escalated.
4. Keep the task in `approval`.

Meaning:
- `escalated` is an opt-out from the approval decision by that agent row
- the row remains present for audit/history and to explain why the human gate was assigned
- this slice supports only human escalation targets, even though the general concept could later be extended to another agent type

### 5.2 Failed agent approver

If an agent approver reaches 3 consecutive failures, the kernel must:

1. Mark the approval row `failed`.
2. Ensure at least one human approval row exists for the same task.
3. Reuse an existing human approval row when one already exists.
4. Keep the task in `approval`.

Meaning:
- `failed` is also an opt-out from the approval decision by that agent row
- the row remains present for audit/history and failure diagnosis
- the human approval row it assigns becomes the blocking gate

### 5.3 Removing a human escalation target

If a human approval row that is currently satisfying one or more `escalated` or `failed` agent approvals is removed while the task is still `approval`, the kernel must:

1. Find every `escalated` or `failed` agent approval row on that task that is currently blocked only by that human gate.
2. Reset all of those agent approval rows from `escalated` or `failed` back to `requested` in the same transaction.
3. Clear each reset row’s `comment_id`.
4. Clear each reset row’s consecutive failure count and last failure error.
5. Requeue all reset rows for dispatcher-managed execution.

This prevents a task from silently losing its last required gate.

### 5.4 Approval reset operation

Approval reset is a first-class kernel operation used by multiple flows in this slice.

Resetting an approval row means:

1. set `status = requested`
2. clear `comment_id`
3. clear live claim fields
4. clear `current_run_id`
5. set `consecutive_failures = 0`
6. clear `last_failure_error`

If the row was previously `running`, reset does not need to terminate the already-spawned agent process eagerly; any later result from that old run becomes stale/discarded because the row no longer remains in the owned `running` state for that run.

The reset operation is used by:
- rejection-cycle reset when a task returns to `todo`
- removing the human gate that is currently blocking one or more `escalated` / `failed` agent approvals
- explicit CLI reset

---

## 6) Agent approver contract

Agent approvers are not allowed to mutate approval rows directly.

By default, autonomous approval workers should reason from task-centric context only:
- task metadata/body/result summary,
- task comments,
- task events/history.

They do not need direct injected visibility into the live approval-row set. Previous approval activity should be discoverable through ordinary task comments/events.

The runtime must expect one strict structured final output with:

- `decision`: one of `approved`, `rejected`, `escalated`
- `comment`: optional feedback text to append as a task comment

Notes:
- `failed` is not a valid agent decision. It is a kernel state used after repeated runtime/contract failures.
- If the runtime cannot parse or validate the agent output, that approval attempt is a failure.
- If the agent emits contradictory or multiple decisions, that approval attempt is a failure.

The approval executor must treat malformed output exactly like other retryable failures.

---

## 7) CLI surface

This slice adds a dedicated approval namespace under `hermes kanban` rather than introducing many new top-level verbs.

### 7.1 New command group

Add a nested command group:

```bash
hermes kanban approval ...
```

This group is the only new CLI namespace for approvals in this slice.

### 7.2 Commands

#### `hermes kanban approval request`

Create one approval row.

Required forms:

```bash
hermes kanban approval request <task_id> --human
hermes kanban approval request <task_id> --agent <profile>
```

Optional flags:
- `--skill <name>` — allowed only with `--agent`
- `--json`

Rules:
- exactly one of `--human` or `--agent` is required
- `--skill` appears at most once in this slice
- adding an approval to a task in `done` or `archived` is forbidden

#### `hermes kanban approval list`

List approval rows.

Supported filters in this slice:

```bash
hermes kanban approval list
hermes kanban approval list --task <task_id>
hermes kanban approval list --status <requested|running|approved|rejected|escalated|failed>
hermes kanban approval list --type <human|agent>
hermes kanban approval list --json
```

#### `hermes kanban approval remove`

Remove one approval row.

```bash
hermes kanban approval remove <approval_id>
```

Rules:
- if the removed row is a human gate currently satisfying an `escalated` or `failed` agent approval, apply the rerun rule from section 5.3
- otherwise, simply remove the row and recompute task state

#### `hermes kanban approval approve`

Human approval action.

```bash
hermes kanban approval approve <task_id> [--comment "..."]
```

Rules:
- valid only when the task has exactly one human approval row
- writes a task comment when `--comment` is present and stores its `comment_id`
- recomputes task state after mutation

#### `hermes kanban approval reject`

Human rejection action.

```bash
hermes kanban approval reject <task_id> [--comment "..."]
```

Rules:
- valid only when the task has exactly one human approval row
- writes a task comment when `--comment` is present and stores its `comment_id`
- applies the rejection rule from section 4.3

#### `hermes kanban approval reset`

Reset one approval row back to `requested`.

```bash
hermes kanban approval reset <approval_id>
```

Rules:
- applies the approval reset operation from section 5.4
- does not recompute task status; with the task still in `approval` and the row reset to `requested`, the aggregate necessarily remains `approval`
- is valid for rows in `running`, `approved`, `rejected`, `escalated`, or `failed`
- if the row was `running`, reset does not require eagerly killing the already-spawned agent process; any later result is stale/discarded because the row no longer remains in the owned `running` state for that run

### 7.3 Existing command changes

#### `hermes kanban show <task_id>`

Extend output to include:
- attached approval rows
- approval status
- approver type
- approver profile / skill when present
- approval comment reference if present

When `--json` is used, include approvals in the structured payload.

#### `hermes kanban create`

No approval flags are added to `create` in this slice.
Approval attachment happens via `hermes kanban approval request`.

Rationale:
- keeps the first CLI slice small and explicit
- avoids adding mixed task/approval creation semantics before approval behavior is proven

---

## 8) Worker prompt / runtime integration

The implementation must update the approval-agent runtime path so the base runtime/system prompt states only the execution contract:
- accepted decision values,
- optional comment field,
- no direct approval-row mutation,
- malformed, extra, or contradictory tool/result handoff is treated as failure.

The base prompt contract must not hardcode one approval policy or style of reasoning beyond the structure/authority boundary.

Phase 4 should also add a default `kanban-approver` skill that auto-loads only when the approval row does not specify an explicit approver skill. An explicit approver skill must override the default rather than stack on top of it automatically.

Phase 5 hardens the runtime handoff further: approval workers keep task scope through `HERMES_KANBAN_TASK`, branch onto the approver surface through `HERMES_KANBAN_APPROVAL_ID`, and hand decisions back through a structured `kanban_approval` tool rather than raw log parsing.

This prompt/runtime contract belongs in runtime code / prompt builder, not only in tests. Concrete autonomous runtime details are specified in `04-autonomous-agent-approval-runtime.md`.

---

## 9) Events and audit rules

This slice relies on events for audit/history rather than immutable approval rows.

At minimum, the implementation must emit events for:
- approval requested
- approval removed
- approval claimed
- approval decided
- approval failed
- approval escalated
- approval reset after rejection
- approval reclaimed / timed out / crashed / invalid-output failure

The exact event names are specified in the DB/runtime specs.

---

## 10) Migration and rollout constraints

This feature must be introduced with additive migrations only.

Requirements:
- existing boards must open successfully before any approval rows exist
- no existing task or run rows may be rewritten in place except for the additive task-status expansion to allow `approval`
- boards with no approval usage must keep existing behavior
- archived-task removal must also remove approval rows and approval runs

DB migration details are specified in `01-db-and-migration.md`.
Runtime/dispatcher details are specified in `02-runtime-and-dispatch.md`.
Concrete Phase 3 CLI/manual workflow details are specified in `03-cli-and-manual-approval-workflows.md`.
Concrete Phase 4 autonomous approval runtime details are specified in `04-autonomous-agent-approval-runtime.md`.
