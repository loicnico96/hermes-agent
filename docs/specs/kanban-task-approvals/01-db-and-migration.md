# Kanban Task Approvals — DB and Migration Spec

Status: Draft (no code changes yet)
Depends on: `docs/specs/kanban-task-approvals/00-master-spec.md`
Scope: SQLite schema, migration strategy, row ownership, and cleanup rules.

---

## 1) Scope

This spec defines the minimum schema required for:
- task-scoped approval state
- agent-approval session management
- per-approval retry tracking
- task-owned cleanup

This slice does **not** introduce:
- approval-round tables
- approval-retention GC separate from task archival/removal
- foreign-key cascade rules

The current Kanban schema explicitly manages child-row deletion in application code. This feature must follow that pattern.

---

## 2) Schema changes

### 2.1 `tasks.status` expansion

Extend task status validation to allow `approving`.

New task status set for this slice:
- `triage`
- `todo`
- `scheduled`
- `ready`
- `running`
- `blocked`
- `review`
- `approving`
- `done`
- `archived`

No existing status semantics are changed except by the new aggregate rules in the master spec.

### 2.2 New table: `task_approvals`

Add one row per active approval gate.

Required columns:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `task_id TEXT NOT NULL`
- `approver_type TEXT NOT NULL`
  - allowed values: `human`, `agent`
- `approver_profile TEXT`
  - required for `approver_type='agent'`
  - NULL for `human`
- `approver_skill TEXT`
  - optional
  - allowed only for `agent`
- `status TEXT NOT NULL`
  - allowed values: `requested`, `approved`, `rejected`, `escalated`, `failed`
- `comment_id INTEGER`
  - nullable reference to the task comment carrying the latest decision text
- `claim_lock TEXT`
  - nullable lease token for an in-flight agent approval run
- `claim_expires INTEGER`
  - nullable Unix timestamp
- `worker_pid INTEGER`
  - nullable process id of the spawned approver worker
- `last_heartbeat_at INTEGER`
  - nullable Unix timestamp
- `current_run_id INTEGER`
  - nullable pointer to the active `task_approval_runs.id`
- `consecutive_failures INTEGER NOT NULL DEFAULT 0`
- `last_failure_error TEXT`
  - nullable short excerpt of the latest failure
- `created_at INTEGER NOT NULL`
- `updated_at INTEGER NOT NULL`

### 2.3 Column constraints (application-enforced)

This slice does not require SQL CHECK constraints, but the kernel must enforce:

- `approver_type='human'` => `approver_profile IS NULL`
- `approver_type='human'` => `approver_skill IS NULL`
- `approver_type='agent'` => `approver_profile IS NOT NULL`
- `status IN {requested, approved, rejected, escalated, failed}`
- `comment_id`, when present, must reference a comment on the same task
- at most one human approval row per task
- at most one agent approval row per `(task_id, approver_profile, approver_skill)` combination

Use the same application-side validation style as the existing Kanban schema.

### 2.4 New table: `task_approval_runs`

Add one row per spawned agent approval attempt.

Required columns:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `approval_id INTEGER NOT NULL`
- `task_id TEXT NOT NULL`
  - denormalized for cheap filtering and cleanup
- `profile TEXT`
- `status TEXT NOT NULL`
  - allowed values: `running`, `approved`, `rejected`, `escalated`, `failed`, `crashed`, `timed_out`, `reclaimed`, `spawn_failed`
- `claim_lock TEXT`
- `claim_expires INTEGER`
- `worker_pid INTEGER`
- `last_heartbeat_at INTEGER`
- `started_at INTEGER NOT NULL`
- `ended_at INTEGER`
- `outcome TEXT`
  - free-form string is allowed, but this slice should write a finite set matching runtime outcomes
- `comment_id INTEGER`
  - nullable pointer to the comment written from the agent result
- `error TEXT`
  - nullable failure summary / parse error excerpt

Notes:
- this table is only for agent approval attempts
- human approvals never create approval-run rows

---

## 3) Indexes

Add the following indexes.

### `task_approvals`

- `idx_task_approvals_task_id ON task_approvals(task_id)`
- `idx_task_approvals_status ON task_approvals(status)`
- `idx_task_approvals_type_status ON task_approvals(approver_type, status)`
- `idx_task_approvals_claimable ON task_approvals(status, approver_type, claim_lock)`

### `task_approval_runs`

- `idx_task_approval_runs_approval_id ON task_approval_runs(approval_id, started_at)`
- `idx_task_approval_runs_task_id ON task_approval_runs(task_id, started_at)`
- `idx_task_approval_runs_status ON task_approval_runs(status)`

These indexes are sufficient for:
- task-local approval lookups
- dispatcher claim scans
- run history inspection
- cleanup on task removal

Do not add speculative indexes beyond this set in the first slice.

---

## 4) Migration strategy

### 4.1 Additive migration only

Migration must be additive and safe for existing boards.

Implementation requirements:
- create `task_approvals` if missing
- create `task_approval_runs` if missing
- add any new indexes with `IF NOT EXISTS`
- expand task-status validation in Python code to accept `approving`

No backfill is required because existing boards have no approval rows.

### 4.2 No in-place synthetic backfill

Do **not** attempt to infer approval rows from:
- `blocked` tasks
- `review-required` comments
- `review` tasks
- past runs or events

This feature starts empty on existing boards.

Rationale:
- existing review/block conventions are not structurally equivalent to approval rows
- speculative migration would create incorrect hidden gates

### 4.3 Init ordering

Follow the existing Kanban migration pattern:
- create base tables
- run additive column/table migrations
- create indexes after the required schema exists

This avoids breaking older boards during initialization.

---

## 5) Row ownership and lifecycle

### 5.1 Ownership model

Approval rows and approval-run rows are owned by the parent task.

Consequences:
- approvals do not outlive the task
- approval runs do not outlive the task
- there is no standalone approval retention policy in this slice
- approval rows are reset whenever the task moves to a status other than `approving`, `done`, or `archived`

### 5.2 Task archival

Archiving a task does **not** delete approval rows or approval runs.

Rationale:
- `archived` is reversible board history, not permanent removal
- this matches current task/task_run behavior

### 5.3 Archived-task permanent removal

When an archived task is permanently deleted, the implementation must delete:
- `task_links`
- `task_comments`
- `task_events`
- `task_runs`
- `task_approvals`
- `task_approval_runs`
- `kanban_notify_subs`
- `tasks`

This must happen inside one write transaction using the same explicit child-row deletion pattern the current code uses.

### 5.4 Hard delete helper

If the internal hard-delete task helper remains available, it must also delete approval rows and approval runs using the same task-owned cleanup sequence.

---

## 6) Runtime field rules

### 6.1 `task_approvals.current_run_id`

- NULL when no agent approval run is active
- points to one live `task_approval_runs.id` while an agent approval run is active
- must be cleared when the run ends, fails, is reclaimed, or the approval no longer has an active claim

### 6.2 Claim fields

`claim_lock`, `claim_expires`, `worker_pid`, and `last_heartbeat_at` on `task_approvals` are the live scheduling fields.

These fields must only be non-NULL for:
- `approver_type='agent'`
- a currently active approval run

Human approvals never use claim fields.

### 6.3 Consecutive failure counter

`consecutive_failures` lives on `task_approvals`, not on `task_approval_runs`.

Rules:
- increment on spawn failure, timeout, crash, reclaim due to invalid/stuck run, and invalid structured output
- reset to 0 on a valid decision (`approved`, `rejected`, `escalated`)
- reset to 0 when an `escalated` or `failed` row is explicitly reopened back to `requested`

This is the counter used to decide when to mark an approval `failed` after 3 consecutive failures.

---

## 7) Minimal event payload requirements

This slice keeps audit history in events rather than in immutable approval rows.

Every approval-related event payload must include `approval_id`.

When an event refers to an approval run, it must also include `approval_run_id`.

At minimum, events that result from decisions or failures should include:
- `approval_id`
- `task_id`
- `approver_type`
- `approver_profile` when applicable
- `status` / decision / failure type
- `comment_id` when a decision comment was written

The runtime spec defines exact event names.

---

## 8) Migration acceptance criteria

A migration is correct only if all of the following hold:

1. Existing boards initialize successfully with zero approval rows.
2. Existing non-approval task behavior is unchanged.
3. New boards get both approval tables and indexes on first init.
4. `tasks.status='approving'` is accepted by validation and persistence.
5. Permanent deletion of an archived task removes all approval rows and approval runs for that task.
6. No migration code attempts to infer or backfill approvals from legacy review/block conventions.
7. The kernel enforces the one-human-per-task invariant.
8. The kernel enforces the one-agent-`(profile, skill)`-per-task invariant.
9. Approval rows are reset whenever a task moves to a status other than `approving`, `done`, or `archived`.
