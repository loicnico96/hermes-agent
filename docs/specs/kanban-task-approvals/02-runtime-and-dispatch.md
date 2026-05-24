# Kanban Task Approvals — Runtime and Dispatch Spec

Status: Draft (no code changes yet)
Depends on:
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/01-db-and-migration.md`

Scope: Dispatcher scheduling, claim/reclaim behavior, approval-agent execution contract, retries, and CLI/runtime integration.

---

## 1) Scheduling unit

The scheduling unit is the approval row, not the task.

Implications:
- multiple agent approvals on one task can run independently
- approval retries are tracked per approval row
- an approval failure or escalation affects task aggregate state through the kernel resolver, not through a task-scoped worker slot

This slice must never schedule “the task’s approvals” as one aggregate job.

---

## 2) Dispatcher integration

### 2.1 Separate approval queue pass

Extend the dispatcher with an approval-specific pass after normal task-worker scheduling.

The dispatcher tick order for this slice must be:

1. reclaim stale task claims
2. detect stale/crashed/timed-out task workers
3. recompute task ready state
4. spawn normal task workers
5. reclaim stale approval claims
6. detect stale/crashed/timed-out approval workers
7. scan runnable approval rows
8. spawn approval workers

This preserves one centralized scheduler and avoids inline approval spawning from task completion code.

### 2.2 Separate concurrency budget

Approval workers use a separate max-spawn budget from normal task workers.

Add a new config value under the kanban config section:
- `kanban.max_approval_spawn`

Meaning:
- maximum number of concurrently running approval workers across the board
- independent from the existing task-worker concurrency cap

Default for this slice:
- default config value is `2`
- do not fall back to `kanban.max_spawn`

This keeps approval throughput isolated and predictable.

---

## 3) Runnable approval definition

An approval row is runnable iff all of the following are true:

1. `approver_type = 'agent'`
2. `status = 'requested'`
3. `approver_profile IS NOT NULL`
4. the parent task exists and `tasks.status = 'approving'`
5. `claim_lock IS NULL`
6. the parent task is not archived

Rows in `running`, `approved`, `rejected`, `escalated`, or `failed` are not runnable.
Human approval rows are never runnable.
`escalated` and `failed` are opt-out states for agent approval rows in this slice; once a row enters either state, it no longer participates in approval decision-making unless it is explicitly reset.

Dispatcher scans must use this predicate directly.

---

## 4) Claim-before-spawn rule

### 4.1 Mandatory atomic claim

Before spawning an approval worker, the dispatcher must atomically claim the approval row.

The claim operation must:
- verify the row is still runnable
- consume `status = requested` and set `status = running`
- set `claim_lock`
- set `claim_expires`
- set `started` bookkeeping on the new approval run row
- set `current_run_id`
- emit an approval-claimed event

Only after the claim succeeds may the dispatcher spawn the subprocess.

This is the only anti-duplication guard. Spawn eligibility must be consumed before process creation.

### 4.2 Approval-run row creation

Every successful claim must create one `task_approval_runs` row with:
- `approval_id`
- `task_id`
- `profile`
- `status = 'running'`
- claim fields
- `started_at`

Human approvals never create run rows.

---

## 5) Approval worker execution model

Approval workers are one-shot decision jobs, not general mutable workers.

### 5.1 Runtime contract

The approval worker receives enough task-centric context to:
- inspect the task
- inspect relevant comments / task history / result summary
- return exactly one decision plus optional comment text

The approval worker does **not** receive approval-mutation tools.
It also does not require direct injected visibility into live approval-row state; prior approval context should be discoverable through ordinary task comments/events.

### 5.2 Expected structured output

The runtime must require a final structured payload with:

- `decision`: `approved` | `rejected` | `escalated`
- `comment`: optional string

Any other shape is invalid.

The base runtime/system prompt for this slice should state the contract only. Default approval behavior should come from a default `kanban_approver` skill when no explicit approver skill is specified.

### 5.3 Output validation

The approval executor must reject output when:
- JSON cannot be parsed
- required keys are missing
- `decision` is not one of the allowed values
- multiple conflicting decisions are present
- the payload is structurally malformed

Invalid output is a failed approval attempt.

---

## 6) Applying a valid approval result

When a valid structured result is returned, the kernel must perform all mutations inside one write transaction.

### 6.1 Common steps

For any valid decision:
1. optionally append the returned comment as a task comment
2. capture the new `comment_id` when a comment was written
3. mark the approval run terminal
4. clear approval-row live claim fields
5. clear `current_run_id`
6. reset `consecutive_failures` to 0
7. consume the owned approval row from `running` into the new terminal approval-row status
8. emit an approval-decision event
9. recompute parent task state using the aggregate resolver

### 6.2 Approved

`decision = approved`
- set approval-row status to `approved`
- recompute task state

### 6.3 Rejected

`decision = rejected`
- set approval-row status to `rejected`
- recompute task state
- the aggregate resolver immediately applies the rejection-cycle reset rule from the master spec in the same transaction that records the authoritative live rejection

### 6.4 Escalated

`decision = escalated`
- set approval-row status to `escalated`
- ensure at least one human approval row exists for the same task
- if a human approval row already exists, reset it to `requested`
- recompute task state

If an existing human approval row already exists on the task, reuse it by resetting it to `requested`; do not create duplicate generic human rows.
The `escalated` row itself has opted out of the approval decision and remains only as audit/history plus the reason the human gate was assigned.

---

## 7) Failure handling

### 7.1 Failure classes

The runtime must treat all of the following as approval-run failures:

- subprocess spawn failure
- subprocess crash
- max-runtime timeout
- stale claim reclaimed after the worker is no longer healthy
- invalid structured output

These are all retryable until the consecutive-failure limit is hit.

### 7.2 Consecutive-failure limit

Use a fixed limit for this slice:
- 3 consecutive failures

Do not add a per-approval override in this slice.

### 7.3 Failure under limit

When a failure occurs and the row remains under the limit:

1. increment `consecutive_failures`
2. set `last_failure_error`
3. mark the approval run terminal with the correct failure outcome
4. clear approval-row live claim fields
5. clear `current_run_id`
6. consume `status = running` back to `status = requested`
7. emit an approval-failure event

The dispatcher retries it on a later pass once the row is again `requested`.

### 7.4 Failure at limit

When a failure causes `consecutive_failures` to reach 3:

1. increment and persist the final failure count
2. mark the approval row `failed`
3. set `last_failure_error`
4. mark the approval run terminal
5. clear live claim fields
6. ensure at least one human approval row exists for the same task
7. if a human approval row already exists, reset it to `requested`
8. emit an approval-failed event
9. recompute task state

The row must not be requeued automatically while it remains `failed`.
The `failed` row itself has opted out of the approval decision and remains only as audit/history plus the reason the human gate was assigned.

---

## 8) Heartbeat / stale / reclaim model

Approval runs must use the same general lease model as task workers.

### 8.1 Heartbeats

Approval workers can heartbeat to extend claim freshness.

Rules:
- heartbeat updates `task_approvals.last_heartbeat_at`
- heartbeat updates the active `task_approval_runs.last_heartbeat_at`
- heartbeat extends `claim_expires`

### 8.2 TTL and live-PID extension

If a claim TTL expires but the worker PID is still alive, the dispatcher extends the lease rather than reclaiming immediately.

This mirrors the existing task-worker logic and avoids killing slow but healthy approval workers.

### 8.3 Reclaim

If an approval run is stale, crashed, or otherwise unrecoverable, the dispatcher must reclaim it.

Reclaim steps:
1. end the approval run with the appropriate outcome (`reclaimed`, `crashed`, `timed_out`, etc.)
2. clear live claim fields on the approval row
3. clear `current_run_id`
4. increment `consecutive_failures`
5. consume `status = running` back to `requested`, or to `failed` if the limit is reached
6. emit an approval reclaim/failure event
7. recompute task state only if the row becomes `failed`

### 8.4 Manual reclaim

This slice does not add a dedicated human CLI reclaim command for approval runs.
Manual intervention is out of scope unless it falls out naturally from internal/admin code.

Keep the initial CLI surface small.

---

## 9) Parent-task interactions

### 9.1 Task leaves `approving`

If a task leaves `approving` because one approval rejected and the aggregate resolver returned the task to `todo`, any other in-flight approval workers for that task are now stale relative to current task state.

Required behavior:
- a recorded rejection keeps the task in `approving` while any approval row is still `running`; once no row remains `running`, that rejection immediately drives the authoritative rejection-cycle path
- late valid results must not re-advance the task incorrectly
- result-application code must re-check task/approval row state before mutating
- if the approval row is no longer in a state compatible with the returned result, the result is discarded at the business-state layer even if the worker process only finished later

The kernel must prefer current authoritative task/approval state over late worker output.

### 9.2 Task archival/removal while approval run is active

If a task is archived or removed while an approval run is active:
- clear the approval row claim fields
- mark the approval run terminal as reclaimed during cleanup if the row still exists at that point
- on permanent task removal, delete the approval rows and runs along with the task-owned cleanup path

---

## 10) CLI/runtime integration requirements

### 10.1 `kanban show`

`hermes kanban show <task_id>` must display approval rows and approval-run history in a task-local way.

This slice does **not** add a standalone `kanban approval show-run` command.
Use task-centric inspection only.

### 10.2 Approval add/remove/decide commands

The `hermes kanban approval ...` commands defined in the master spec and concretized in `03-cli-and-manual-approval-workflows.md` must call kernel helpers that:
- mutate approval rows transactionally
- recompute task state transactionally where required
- use the first-class approval reset operation for both internal flows and `hermes kanban approval reset`
- do not perform direct process spawning inline

Spawning remains dispatcher-owned.

---

## 11) Event names

Use these exact event kinds in this slice.

### Approval-row lifecycle
- `approval_requested`
- `approval_removed`
- `approval_decided`
- `approval_reset`
- `approval_failed`

### Approval-run lifecycle
- `approval_claimed`
- `approval_claim_extended`
- `approval_spawn_failed`
- `approval_timed_out`
- `approval_crashed`
- `approval_reclaimed`
- `approval_invalid_output`

Payload requirements:
- always include `approval_id`
- include `approval_run_id` for run events
- include `decision` for `approval_decided`
- include `comment_id` when a comment was written
- include concise error text for failure events

---

## 12) Acceptance criteria

Runtime/dispatcher behavior is correct only if all of the following hold:

1. Two agent approvals on the same task can be claimed and run independently.
2. An approval row is never spawned twice concurrently.
3. Agent approval spawning is performed only by the dispatcher.
4. Claim consumes `requested` and marks the approval row `running` before subprocess spawn.
5. Approval workers do not mutate approval rows directly.
6. Invalid approver output is treated as a failed attempt.
7. After 3 consecutive failures, the approval row becomes `failed` and a human approval row exists.
8. `escalated` and `failed` do not block `done` by themselves; the live blocking gate is the human approval row they ensure when one exists.
9. A single authoritative rejection returns the task to `todo` and resets approval rows to `requested` in the same transaction once no approval row remains `running`.
10. Approval workers use independent concurrency accounting from task workers.
11. Stale/crashed/timed-out approval workers are reclaimed using task-like lease semantics.
12. Late approval results cannot incorrectly move a task back to `done` after the task already returned to `todo`.
