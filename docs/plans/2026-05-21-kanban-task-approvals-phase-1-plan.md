# Kanban Task Approvals — Phase 1 Implementation Plan

**Phase scope only:** schema, migration, and kernel data helpers.

**Work lane:** all Phase 1 work happens in the dedicated worktree at `/home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521` on branch `noame/kanban-task-approvals-v1`. The intentionally dirty main checkout is not touched by this phase.

**Source contract:**
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/01-db-and-migration.md`
- `docs/specs/kanban-task-approvals/02-runtime-and-dispatch.md`
- `docs/plans/2026-05-21-kanban-task-approvals-implementation-plan.md`

---

## 1. Objective

Land the storage model and kernel-side helper surface required for approvals without enabling any later-phase behavior.

This phase must deliver:
- `approving` accepted as a task status
- new `task_approvals` table
- new `task_approval_runs` table
- required indexes
- additive migration for existing boards
- application-enforced approval-row invariants
- task-owned cleanup for permanent deletion
- a first-class approval reset helper

This phase must **not** deliver:
- CLI approval commands
- approval dashboard/UI
- dispatcher approval queue pass
- approval worker spawning
- runtime decision application flow

---

## 2. Likely files in scope

### Primary implementation file
- `hermes_cli/kanban_db.py`

### Primary tests to extend
- `tests/hermes_cli/test_kanban_db.py`
- `tests/hermes_cli/test_kanban_db_init.py`

### Optional new focused test file if DB tests get too large
- `tests/hermes_cli/test_kanban_approvals_db.py`

Keep Phase 1 isolated to DB/kernel-layer docs and tests. Do not edit CLI command plumbing or plugin runtime code in this phase.

---

## 3. Planned data-model additions

## 3.1 Status constants and validation

In `hermes_cli/kanban_db.py`:
- extend `VALID_STATUSES` to include `approving`
- leave existing status meanings unchanged except where the specs already define approval-aware semantics
- do **not** widen `VALID_INITIAL_STATUSES` unless current code requires it for persistence tests; this phase is not introducing direct create-time use of `approving`

## 3.2 New row types

Add new dataclasses beside the existing `Task`, `Run`, `Comment`, and `Event` models:
- `Approval`
- `ApprovalRun`

Expected fields should mirror the spec closely so later phases do not need a shape rewrite.

`Approval` should include at minimum:
- `id`
- `task_id`
- `approver_type`
- `approver_profile`
- `approver_skill`
- `status`
- `comment_id`
- `claim_lock`
- `claim_expires`
- `worker_pid`
- `last_heartbeat_at`
- `current_run_id`
- `consecutive_failures`
- `last_failure_error`
- `created_at`
- `updated_at`

`ApprovalRun` should include at minimum:
- `id`
- `approval_id`
- `task_id`
- `profile`
- `status`
- `claim_lock`
- `claim_expires`
- `worker_pid`
- `last_heartbeat_at`
- `started_at`
- `ended_at`
- `outcome`
- `comment_id`
- `error`

Use `from_row()` constructors matching current `Task` / `Run` style.

## 3.3 New schema objects

Add to `SCHEMA_SQL`:
- `task_approvals`
- `task_approval_runs`

Use the exact spec column set from `01-db-and-migration.md`. Keep constraints application-enforced, consistent with the current kanban DB style.

Required indexes:

`task_approvals`
- `idx_task_approvals_task_id`
- `idx_task_approvals_status`
- `idx_task_approvals_type_status`
- `idx_task_approvals_claimable`

`task_approval_runs`
- `idx_task_approval_runs_approval_id`
- `idx_task_approval_runs_task_id`
- `idx_task_approval_runs_status`

Do not add speculative indexes in Phase 1.

---

## 4. Ordered implementation tasks

## Task 1 — Extend status acceptance to include `approving`

Update the task-status validation surface in `hermes_cli/kanban_db.py` so rows with `status='approving'` are accepted anywhere normal persisted task statuses are validated or materialized.

Checks:
- `Task.from_row()` continues to work unchanged
- any helper that validates target status can persist `approving`
- no existing ready/running/done behavior is changed yet beyond acceptance of the new literal

## Task 2 — Add approval dataclasses and row parsers

Add `Approval` and `ApprovalRun` dataclasses next to the existing DB row models.

Implementation notes:
- keep field naming aligned with the DB column names
- preserve nullable handling for `comment_id`, claim fields, and failure fields
- do not add runtime-only convenience fields yet

Rationale: later phases will need typed helper returns immediately; Phase 1 should establish the canonical row shapes now.

## Task 3 — Add schema for `task_approvals` and `task_approval_runs`

Extend `SCHEMA_SQL` with the two new tables.

Important details to preserve from the spec:
- no foreign-key cascade reliance
- `task_id` is denormalized onto `task_approval_runs`
- human approvals never require run rows, but the table still exists at init time
- status columns remain open text validated by kernel code, not SQL `CHECK`

Keep the table definitions close to the existing `task_runs` style so later claim/reclaim code can follow the same patterns.

## Task 4 — Add additive migration support for old boards

Update `_migrate_add_optional_columns()` or split out a clearly named helper used from there, so opening an older board safely creates the new approval tables and indexes if missing.

Recommended shape:
- keep table creation additive and idempotent
- use `CREATE TABLE IF NOT EXISTS` for approval tables
- use `CREATE INDEX IF NOT EXISTS` for approval indexes
- keep this work after base schema init, matching the current migration ordering pattern

Do **not** backfill or infer approvals from:
- `blocked`
- `review`
- `review-required` comments
- prior runs/events

Existing boards must remain empty with respect to approvals after migration.

## Task 5 — Add kernel validation helpers for approval identity and status

Add small internal helpers in `hermes_cli/kanban_db.py` for approval-row validation. Likely helper responsibilities:
- validate `approver_type` is exactly `human` or `agent`
- require `approver_profile is NULL` and `approver_skill is NULL` for human rows
- require `approver_profile` for agent rows
- validate approval `status` is one of `requested|approved|rejected|escalated|failed`
- normalize empty skill/profile values consistently before duplicate checks

Keep these helpers kernel-local; this phase is about the authoritative storage semantics.

## Task 6 — Add approval CRUD-style kernel helpers needed for Phase 1 tests

Add only the minimal data helpers needed to exercise the schema and invariants in tests. Likely helpers:
- `create_task_approval(...)`
- `get_task_approval(...)`
- `list_task_approvals(...)`
- `create_task_approval_run(...)` or a narrower internal insert helper for tests

Scope rules:
- these helpers should perform validation and write rows
- they should **not** spawn workers or run aggregate task-state transitions beyond whatever is necessary to keep row semantics valid
- they should enforce uniqueness in application code before insert

Invariant enforcement required by this phase:
- at most one human approval row per task
- at most one agent approval row per `(task_id, approver_profile, approver_skill)`

Duplicate attempts should fail deterministically with `ValueError`-style errors consistent with existing kanban DB helpers.

## Task 7 — Add first-class approval reset helper

Implement a dedicated kernel reset operation in `hermes_cli/kanban_db.py` for approval rows.

Reset semantics must match the spec exactly:
1. set `status = 'requested'`
2. clear `comment_id`
3. clear live claim fields
4. clear `current_run_id`
5. set `consecutive_failures = 0`
6. clear `last_failure_error`

Even though later phases will call this helper from more flows, Phase 1 should land it now as the single authoritative reset primitive.

Recommended shape:
- single-row reset helper first
- optional task-wide wrapper only if tests or existing mutation flows clearly need it

Do not wire reset into broad task-state transitions yet unless required to preserve current behavior for newly added tests.

## Task 8 — Hook task-owned cleanup into permanent deletion paths

Update permanent deletion helpers in `hermes_cli/kanban_db.py` so approval-owned rows are deleted with the same explicit child-row cleanup strategy already used for task comments/events/runs.

Concrete targets:
- `delete_archived_task(...)`
- `delete_task(...)` if the internal hard-delete helper is intentionally still supported

Deletion order should explicitly include:
- `task_links`
- `task_comments`
- `task_events`
- `task_runs`
- `task_approvals`
- `task_approval_runs`
- `kanban_notify_subs`
- `tasks`

This must remain transactional.

## Task 9 — Decide whether any task-status mutation helper needs Phase 1-only reset wiring

The specs say approval rows are reset whenever a task moves to a status other than `approving`, `done`, or `archived`.

For Phase 1, implement only the minimum safe kernel hook needed so this invariant is not forgotten when later phases land. Preferred approach:
- add an internal helper stub or narrowly used mutation hook in `kanban_db.py` now
- if full integration would drag in Phase 2 aggregate semantics, defer the broader wiring and explicitly cover it as a Phase 2 dependency in comments/tests

Phase 1 should avoid semantic creep, but it should leave a clear, named insertion point rather than letting reset logic scatter later.

---

## 5. Required tests

## 5.1 Schema/init and migration coverage

In `tests/hermes_cli/test_kanban_db_init.py` and/or `tests/hermes_cli/test_kanban_db.py` add coverage for:
- fresh DB init creates `task_approvals`
- fresh DB init creates `task_approval_runs`
- fresh DB init creates all required approval indexes
- legacy DB missing the approval tables migrates cleanly on `connect()` / `init_db()`
- migrated legacy boards contain zero approval rows
- `approving` survives round-trip persistence

## 5.2 Approval invariant coverage

Add tests for:
- one human approval allowed per task
- second human approval on the same task is rejected
- same agent/profile with `NULL` skill duplicates are rejected
- same agent/profile with same explicit skill duplicates are rejected
- same agent/profile with different skill is allowed
- human rows reject non-null profile/skill
- agent rows require profile
- invalid approval status is rejected

## 5.3 Reset helper coverage

Add tests proving reset clears exactly the live mutable fields:
- status resets to `requested`
- `comment_id` cleared
- `claim_lock` cleared
- `claim_expires` cleared
- `worker_pid` cleared
- `last_heartbeat_at` cleared
- `current_run_id` cleared
- `consecutive_failures` reset to `0`
- `last_failure_error` cleared

Also verify identity fields do **not** change:
- `task_id`
- `approver_type`
- `approver_profile`
- `approver_skill`
- `created_at`

## 5.4 Permanent-delete cleanup coverage

Add tests for:
- deleting an archived task removes matching `task_approvals`
- deleting an archived task removes matching `task_approval_runs`
- unrelated task approvals/runs remain intact
- internal `delete_task()` path, if still intentionally supported, also removes approval rows/runs

## 5.5 Non-regression coverage

Run or extend existing tests around:
- standard task creation
- task completion
- archiving
- board init

Goal: ensure schema changes do not perturb boards with no approvals.

---

## 6. Verification commands

Run from the dedicated worktree only:

```bash
cd /home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521
pytest tests/hermes_cli/test_kanban_db_init.py -q
pytest tests/hermes_cli/test_kanban_db.py -q
```

If approval tests move into a focused file:

```bash
cd /home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521
pytest tests/hermes_cli/test_kanban_approvals_db.py -q
pytest tests/hermes_cli/test_kanban_db_init.py tests/hermes_cli/test_kanban_db.py -q
```

Optional targeted migration sanity check during development:

```bash
cd /home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521
pytest tests/hermes_cli/test_kanban_db.py -k "migrate or approval or archived" -q
```

---

## 7. Phase 1 exit criteria

Phase 1 is complete only when all of the following are true:
- `hermes_cli/kanban_db.py` accepts `tasks.status='approving'`
- both approval tables exist on fresh init
- both approval tables are created additively on old boards
- required approval indexes exist
- no migration attempts to infer or backfill approval rows from legacy review/block conventions
- one-human-per-task invariant is enforced
- one-agent-per-`(profile, skill)`-per-task invariant is enforced
- a dedicated approval reset helper exists and is test-covered
- archived-task permanent deletion removes approval rows and approval runs in the same cleanup transaction pattern as other task-owned rows
- existing non-approval board flows still pass DB tests

---

## 8. Explicit deferrals to Phase 2+

Do not pull these into Phase 1:
- aggregate `approving -> done` / `approving -> todo` resolution
- completion-time switch from `done` to `approving`
- dispatcher claim/reclaim for approvals
- approval-run heartbeat behavior
- agent output parsing
- human escalation-row creation
- CLI approval namespace
- dashboard/task-show rendering of approvals

If implementation pressure appears in Phase 1, add narrow kernel helpers only when they reduce rework later without changing runtime behavior now.
