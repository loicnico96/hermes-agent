# Kanban Task Approvals — Autonomous Agent Approval Runtime Spec

Status: Draft
Depends on:
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/01-db-and-migration.md`
- `docs/specs/kanban-task-approvals/02-runtime-and-dispatch.md`
- `docs/specs/kanban-task-approvals/03-cli-and-manual-approval-workflows.md`
- Phase 3 manual approval workflows already landed on the same branch/PR lane

Scope: concrete Phase 4 runtime slice for dispatcher-managed autonomous agent approvals, including claim/run lifecycle, approval-worker spawn contract, retries, reclaim/timeout handling, and escalation into human review. Dashboard UX remains out of scope.

---

## 1) Goal

Make agent approvals actually execute.

After this phase, an attached agent approval row on a task in `approving` must be able to:

1. become runnable through the dispatcher,
2. be claimed atomically,
3. spawn one approval worker for the configured approver profile,
4. return exactly one structured decision,
5. have that decision applied by the kernel,
6. retry on runtime/contract failure up to the fixed failure limit,
7. escalate to human review when the agent opts out or exhausts its retry budget.

Phase 4 must preserve the Phase 2/3 principle that approval-row state is kernel-owned. Approval workers may inspect task context and return a decision payload, but they must never mutate approval rows directly.

---

## 2) Explicit scope for this phase

This phase must deliver:

1. a dispatcher approval pass after normal task-worker scheduling,
2. a separate approval-worker concurrency cap,
3. atomic approval-row claim-before-spawn behavior,
4. `task_approval_runs` creation and lifecycle updates for live agent attempts,
5. approval-worker spawn/runtime integration,
6. a strict structured final decision contract,
7. approval failure/retry bookkeeping,
8. stale/crash/timeout/reclaim handling for approval runs,
9. automatic human-gate creation or reset on escalation/failure,
10. focused runtime/dispatcher/kernel tests for the new behavior.

This phase must **not** deliver:

- dashboard UI,
- gateway buttons or interactive approval UX,
- task-creation flags that inline approval setup into `hermes kanban create`,
- direct approval-row mutation tools for workers,
- general-purpose multi-step approval workflows,
- per-approval override knobs for retry budget or concurrency cap,
- a broad policy prompt that hardcodes how the approver should reason beyond the structured contract.

---

## 3) Authority and layering rules

### 3.1 Approval rows remain the business authority

The authoritative state remains:
- `tasks.status` for task execution lifecycle,
- `task_approvals.status` for approval lifecycle,
- `task_approval_runs` for attempt bookkeeping.

Phase 4 adds autonomous execution only. It must not move business authority out of the kernel.

### 3.2 Scheduling unit: approval row

The dispatcher schedules approval rows, not tasks.

Implications:
- multiple agent approvals on the same task may run independently,
- retries are tracked per approval row,
- escalation/failure state is per approval row,
- task state changes still flow only through the shared aggregate resolver.

### 3.3 Approval workers are decision producers only

Approval workers may:
- inspect the parent task,
- inspect task comments,
- inspect task events/history,
- read the task result summary and other existing task-centric context,
- return one structured decision with optional comment text.

Approval workers must **not**:
- receive tools that mutate approval rows directly,
- decide aggregate task-state transitions themselves,
- depend on direct visibility into the live approval-row set as part of their reasoning contract.

Task context is sufficient in this slice. If a previous approval matters, that information must be visible through existing task comments/events rather than through dedicated approval-row context injection.

---

## 4) Dispatcher integration

### 4.1 Tick order

Extend the existing dispatcher with an approval-specific pass after normal task-worker scheduling.

The Phase 4 dispatcher tick order must be:

1. reclaim stale task claims,
2. detect stale/crashed/timed-out task workers,
3. recompute task ready state,
4. spawn normal task workers,
5. reclaim stale approval claims,
6. detect stale/crashed/timed-out approval workers,
7. scan runnable approval rows,
8. spawn approval workers.

This preserves one centralized scheduler while keeping approval execution visibly separate from task execution.

### 4.2 Separate approval concurrency budget

Add a new config key:
- `kanban.max_approval_spawn`

Rules:
- this cap applies only to concurrently running approval workers,
- it is fully independent from `kanban.max_spawn`,
- the default value in config must be `2`,
- Phase 4 must **not** fall back to `kanban.max_spawn` when the approval cap is unset,
- tests must assert the isolated default explicitly.

Rationale:
- approval work and task work are different queues with different operator expectations,
- a shared budget would make approval throughput depend on unrelated task-worker load,
- a small default approval cap keeps parallel agent review bounded without silently starving task workers.

### 4.3 Runnable approval definition

An approval row is runnable iff all of the following are true:

1. `approver_type = 'agent'`,
2. `status = 'requested'`,
3. `approver_profile IS NOT NULL`,
4. the parent task exists and `tasks.status = 'approving'`,
5. `claim_lock IS NULL`,
6. the parent task is not archived.

Rows in `running`, `approved`, `rejected`, `escalated`, or `failed` are not runnable.
Human approval rows are never runnable.

Dispatcher code must use this predicate directly rather than deriving it indirectly from older task-worker logic.

---

## 5) Claim and run lifecycle

### 5.1 Atomic claim-before-spawn rule

Before spawning an approval worker, the dispatcher must atomically claim the approval row.

The claim operation must:
- verify the row is still runnable,
- consume `status = 'requested'` into `status = 'running'`,
- set `claim_lock`,
- set `claim_expires`,
- initialize approval heartbeat bookkeeping,
- create exactly one `task_approval_runs` row with `status = 'running'`,
- set `current_run_id` on the approval row to that run id,
- emit an approval-claimed event.

Only after the claim succeeds may the dispatcher spawn the subprocess.

This is the duplication guard for approval workers. Phase 4 must not allow spawn-then-claim races.

### 5.2 Approval-run row creation

Every successful claim must create one `task_approval_runs` row with at least:
- `approval_id`,
- `task_id`,
- `profile`,
- `status = 'running'`,
- claim fields,
- `started_at`.

Human approval rows never create run rows.

### 5.3 Worker pid recording

After a successful subprocess spawn, the runtime must record the worker pid on:
- `task_approvals.worker_pid`, and
- the live `task_approval_runs.worker_pid` row.

This mirrors the task-worker model and is required for crash inspection and stale reclaim behavior.

---

## 6) Approval-worker runtime contract

### 6.1 Base prompt contract stays narrow

The base runtime/system-prompt layer for approval workers must state only the execution contract:
- the worker is acting as a task approver,
- it must return exactly one structured final decision,
- accepted decision values are `approved`, `rejected`, or `escalated`,
- `comment` is optional,
- direct approval-row mutation is forbidden,
- malformed, extra, or contradictory output is treated as failure.

The base prompt contract must **not** hardcode a specific approval policy or style of reasoning beyond this contract.

Rationale:
- users must be able to control approval behavior through skills or explicit approver configuration,
- the runtime should enforce structure and authority boundaries, not encode one canonical approval personality.

### 6.2 Default `kanban-approver` skill

Phase 4 should add a default `kanban-approver` skill for approval workers.

Rules:
- when an agent approval row does **not** specify an explicit auto-loaded skill, the runtime auto-loads `kanban-approver`,
- when the approval row does specify an explicit skill, that explicit skill wins and the default is not auto-added on top,
- the default skill should provide sane approval guidance, but the runtime contract must remain valid even without relying on skill-specific wording.

This gives users a useful default while preserving full control for custom approver behavior.

### 6.3 Approval worker context surface

Approval workers must receive enough context to inspect the parent task effectively, including:
- task metadata,
- task body/result summary,
- task comments,
- task events/history relevant to prior execution and review.

Approval workers should **not** receive dedicated injected context describing:
- the full live approval-row set,
- other approvers' raw row state,
- approval-run bookkeeping rows.

If previous approvers matter, they should be discoverable through ordinary task-centric history surfaces such as comments and events.

### 6.4 Structured final output

The runtime must require exactly one final structured payload with:

- `decision`: `approved` | `rejected` | `escalated`
- `comment`: optional string

Any other shape is invalid.

Invalid output includes:
- non-parseable JSON,
- missing `decision`,
- unsupported decision values,
- multiple conflicting decisions,
- extra contradictory structure that makes the intent ambiguous.

`failed` is not a valid worker decision. It remains a kernel-only state used after repeated runtime/contract failure.

---

## 7) Applying valid approval results

### 7.1 Common success path

When a valid decision payload is returned, the kernel must apply all mutations inside one write transaction.

Common steps:
1. verify the live run still owns the approval row,
2. optionally append the returned comment as a task comment,
3. capture the new `comment_id` when a comment was written,
4. mark the approval run terminal,
5. clear approval-row live claim fields,
6. clear `current_run_id`,
7. reset `consecutive_failures` to `0`,
8. clear `last_failure_error`,
9. consume the approval row from `running` into the terminal approval-row state,
10. emit an approval-decision event,
11. recompute task state through the shared aggregate resolver.

### 7.2 Approved

For `decision = approved`:
- set approval-row status to `approved`,
- preserve any optional comment via `comment_id`,
- recompute task state.

### 7.3 Rejected

For `decision = rejected`:
- set approval-row status to `rejected`,
- preserve any optional comment via `comment_id`,
- recompute task state,
- rely on the existing aggregate resolver to apply the rejection-cycle reset when no live `running` row outranks the rejection.

Phase 4 must preserve the existing rule that a live `running` row outranks an already-recorded `rejected` row until no approval row remains `running`.

### 7.4 Escalated

For `decision = escalated`:
- set the agent approval row to `escalated`,
- ensure at least one human approval row exists for the same task,
- if no human row exists, create one,
- if a human row already exists, reset that human row back to `requested`,
- recompute task state.

The reset-on-reuse rule is required in this phase.

Rationale:
- a previously approved human row may predate the new escalated concern,
- escalation means the human must reconsider the task with new information,
- silently reusing an already-approved human row would allow stale approval state to satisfy the new gate.

When resetting the existing human row for escalation reuse, the implementation must apply the normal approval reset operation:
- set `status = requested`,
- clear `comment_id`,
- clear live claim fields,
- clear `current_run_id`,
- reset `consecutive_failures = 0`,
- clear `last_failure_error`.

---

## 8) Failure handling

### 8.1 Failure classes

The runtime must treat all of the following as approval-run failures:
- subprocess spawn failure,
- subprocess crash,
- max-runtime timeout,
- stale claim reclaimed after the worker is no longer healthy,
- invalid structured output.

All of these are retryable until the fixed failure limit is reached.

### 8.2 Failure limit

Use a fixed limit in this phase:
- 3 consecutive failures per approval row.

Do not add per-approval override knobs in Phase 4.

### 8.3 Failure under limit

When a failure occurs and the row remains under the limit:

1. increment `consecutive_failures`,
2. persist `last_failure_error`,
3. mark the approval run terminal with the correct failure outcome,
4. clear approval-row live claim fields,
5. clear `current_run_id`,
6. consume `status = 'running'` back to `status = 'requested'`,
7. emit an approval-failure event.

The dispatcher retries the row on a later pass once it becomes runnable again.

### 8.4 Failure at limit

When a failure causes `consecutive_failures` to reach `3`:

1. persist the final failure count,
2. mark the approval row `failed`,
3. persist `last_failure_error`,
4. mark the approval run terminal,
5. clear live claim fields,
6. ensure at least one human approval row exists for the same task,
7. if a human row already exists, reset it to `requested`,
8. emit an approval-failed event,
9. recompute task state.

The row must not be requeued automatically while it remains `failed`.

The reset-on-existing-human rule for failure escalation is intentionally the same as explicit `escalated`.

---

## 9) Heartbeat, stale, crash, and timeout handling

### 9.1 Heartbeats

Approval workers may heartbeat to extend claim freshness.

Heartbeat updates must touch:
- `task_approvals.last_heartbeat_at`,
- the live `task_approval_runs.last_heartbeat_at`,
- `claim_expires`.

### 9.2 TTL extension for live pid

If a claim TTL expires but the worker pid is still alive, the dispatcher extends the lease rather than reclaiming immediately.

This mirrors the existing task-worker behavior and avoids punishing slow but healthy approvers.

### 9.3 Reclaim behavior

If an approval run is stale, crashed, or otherwise unrecoverable, the dispatcher must reclaim it.

Reclaim steps:
1. end the approval run with the appropriate terminal outcome,
2. clear live claim fields on the approval row,
3. clear `current_run_id`,
4. increment `consecutive_failures`,
5. consume `status = 'running'` back to `requested`, or to `failed` if the failure limit is reached,
6. emit an approval reclaim/failure event,
7. recompute task state only if the row becomes `failed` or triggers human-gate reset/create behavior.

Phase 4 does not add a dedicated human CLI reclaim command for approval runs.

---

## 10) Stale-result and state-race rules

Late worker output must never outrank current authoritative row state.

Phase 4 must preserve and extend the existing row/run-ownership guard so that a returned result is discarded when any of the following are true:
- the approval row no longer exists,
- the approval row is no longer `running`,
- `current_run_id` no longer matches the returning run,
- the task has left the compatible task-state domain for that result path,
- the row was reset, reclaimed, or removed before the result arrived.

Required implications:
- resetting a `running` approval row does not require eagerly killing the process,
- removing a human gate and re-requesting agent review does not make old results valid again,
- a rejection-cycle reset must prevent stale in-flight approvals from re-advancing the task,
- an approval row reused after escalation/failure must only accept results from its new live run.

The kernel must prefer current row ownership and current task state over late subprocess output.

---

## 11) Events and audit requirements

At minimum, Phase 4 must emit events for:
- approval claimed,
- approval spawned,
- approval heartbeat,
- approval decided,
- approval escalated,
- approval failed,
- approval reset for escalation/failure human-gate reuse,
- approval reclaim,
- approval timeout,
- approval crash,
- approval invalid-output failure.

The exact event names should stay consistent with existing task/approval event naming style. The purpose of this section is contract coverage, not wire-name bikeshedding.

Comments remain the human-readable explanation surface. Run rows remain attempt bookkeeping.

---

## 12) Testing requirements

Phase 4 is complete only if tests cover all of the following:

1. runnable approval-row predicate,
2. atomic claim-before-spawn,
3. exactly one run row per successful claim,
4. isolated approval concurrency cap with default `2`,
5. valid `approved` application,
6. valid `rejected` application,
7. valid `escalated` application,
8. escalation reset of an already-existing human row,
9. failure under limit requeues to `requested`,
10. failure at limit marks the row `failed` and resets/creates the human gate,
11. crash/timeout/stale reclaim paths,
12. late/stale result discard when ownership changed,
13. approval workers not depending on injected approval-row context,
14. default `kanban-approver` skill autoload only when no explicit skill is specified.

Focused test placement is expected in the existing approval DB/dispatcher/runtime test files rather than one giant new end-to-end suite.

---

## 13) Acceptance criteria

Phase 4 is complete only if all of the following hold:

1. a task in `approving` with a runnable agent approval row is picked up automatically by the dispatcher,
2. approval rows are claimed atomically before process spawn,
3. approval workers return exactly one structured decision payload or fail,
4. valid decisions mutate approval state only through kernel-owned helpers,
5. invalid output is treated as approval-run failure,
6. approval failure retry budget is enforced per approval row,
7. `kanban.max_approval_spawn` is independent and defaults to `2`,
8. explicit `escalated` and retry-exhausted `failed` both create or reset a human gate,
9. an existing human gate is reset to `requested` on escalation/failure reuse,
10. stale late results are safely discarded,
11. no worker gains direct approval-row mutation power.

---

## 14) Cross-references

- Core business rules remain in `00-master-spec.md`.
- DB schema/migration rules remain in `01-db-and-migration.md`.
- Broad runtime/dispatcher lifecycle rules remain in `02-runtime-and-dispatch.md`.
- Manual CLI/operator workflows remain in `03-cli-and-manual-approval-workflows.md`.
- This file is the concrete Phase 4 execution contract for autonomous approval runtime behavior.
