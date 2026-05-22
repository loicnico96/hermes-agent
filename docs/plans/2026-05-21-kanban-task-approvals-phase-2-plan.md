# Kanban Task Approvals — Phase 2 Implementation Plan

**Phase scope only:** aggregate task-state integration.

**Work lane:** all Phase 2 work happens in the dedicated worktree at `/home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521` on branch `noame/kanban-task-approvals-v1`. The intentionally dirty main checkout is not touched by this phase.

**Source contract:**
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/01-db-and-migration.md`
- `docs/specs/kanban-task-approvals/02-runtime-and-dispatch.md`
- `docs/plans/2026-05-21-kanban-task-approvals-implementation-plan.md`
- `docs/plans/2026-05-21-kanban-task-approvals-phase-1-plan.md`

**Priority rule:** if the high-level implementation plan and the master spec diverge, follow the master spec.

---

## 1. Objective

Land the kernel-owned task/approval aggregate state rules so approvals affect task lifecycle correctly before any Phase 3 CLI approval surface or Phase 4 dispatcher-managed approval execution is added.

This phase must deliver:
- one authoritative aggregate resolver in kernel code
- completion-time routing from `running|ready|blocked -> approving` when approval rows exist
- exact `approving -> done` behavior from the master spec
- exact rejection-cycle behavior from the master spec
- task-state-transition reset wiring for approval rows when a task moves to a status other than `approving`, `done`, or `archived`
- explicit handling for `escalated` / `failed` as agent opt-out states that do not themselves block `done`
- guardrails so late or stale approval state cannot incorrectly push a task through the wrong lifecycle path

This phase must **not** deliver:
- `hermes kanban approval ...` CLI commands
- dispatcher approval queue passes or approval worker spawning
- approval-run heartbeat/reclaim logic
- approval-agent prompt/output handling
- dashboard/task-show rendering work beyond whatever existing DB-facing test helpers need

---

## 2. Likely files in scope

### Primary implementation file
- `hermes_cli/kanban_db.py`

### Likely call sites to update in that file
- `complete_task(...)`
- `_handle_task_status_transition_approval_reset(...)`
- any helper that directly mutates persisted task status to `todo`, `ready`, `blocked`, `scheduled`, `done`, or `archived`
- any task mutation path that can move a task out of `approving` without going through `complete_task(...)`

### Primary tests to extend
- `tests/hermes_cli/test_kanban_db.py`
- `tests/hermes_cli/test_kanban_approvals_db.py`

### Optional focused test file if semantics become too dense for the existing files
- `tests/hermes_cli/test_kanban_approvals_lifecycle.py`

Keep this phase DB/kernel-scoped. Do not edit `hermes_cli/kanban.py` for new commands in Phase 2.

---

## 3. Semantic rules to encode

## 3.1 Aggregate authority

Phase 2 should introduce one kernel helper that owns approval-aware task resolution. Everything else should call it instead of open-coding status decisions.

Recommended responsibilities for the resolver/helper layer:
- inspect all approval rows for a task
- inspect whether any approval run is still active for that task
- decide whether the task must remain `approving`, move to `done`, or move to `todo`
- perform rejection-cycle resets transactionally when the task returns to `todo`
- leave non-approval task semantics unchanged when a task has no approval rows

A good shape would be one read helper plus one mutation helper, for example:
- `_compute_task_approval_aggregate_state(...)`
- `_apply_task_approval_aggregate_transition(...)`

Exact names may differ, but the implementation should end with one obvious authority point.

## 3.2 Entering `approving`

When `complete_task(...)` finishes normally and the task has one or more approval rows, the task must go to `approving`, not `done`.

Important details:
- the worker/completion path does not decide whether approvals are required based on worker output
- the kernel checks for attached approval rows
- this rule applies even if all rows currently happen to be `approved`; completion routing still goes through the aggregate helper so later transitions are not split across codepaths

## 3.3 While unresolved approvals exist

A task must remain `approving` while any approval row is:
- `requested`
- `escalated`
- `failed`

A task must also remain `approving` while any approval run for that task is still active, even if a rejection has already been recorded.

For Phase 2, “active approval run” should be derived from persisted approval-run state, not from future dispatcher assumptions.

## 3.4 Rejection cycle

If any approval row is `rejected`, the resolver must implement the full rejection cycle from the master spec:
1. rejection is recorded first
2. task stays `approving` while any approval run for that task is still active
3. after the last active approval run finishes, task transitions from `approving` to `todo`
4. in that same transaction, reset all approval rows to `requested`
5. in that same transaction, clear each row’s `comment_id`
6. in that same transaction, clear each row’s consecutive-failure state

Preserve the distinction between:
- “rejection exists, but another approval run is still live” -> task stays `approving`
- “rejection exists, and no approval run is still live” -> task goes to `todo` and all approval rows reset immediately in the same transaction

## 3.5 Done rule

A task may move from `approving` to `done` only when:
- there is no approval row in `requested` or `rejected`
- there is no active approval run
- every remaining approval row is in `approved`, `escalated`, or `failed`

Critical implications to preserve:
- `escalated` and `failed` are opt-out states for agent approval rows
- those rows remain present for audit/history but do not independently block `done`
- the actual blocking gate is the human approval row that exists on top
- removing the last approval row while the task is `approving` should move the task to `done`

## 3.6 Reset-on-task-transition rule

Approval rows must be reset whenever a task moves to a status other than `approving`, `done`, or `archived`.

In practice, Phase 2 should wire this through a centralized task-status transition path rather than trying to remember it ad hoc in scattered helpers.

This rule matters for transitions such as:
- `approving -> todo`
- `approving -> ready` if such a path exists internally
- `approving -> blocked`
- `approving -> scheduled`
- any later manual/internal status mutation that exits approval mode without archiving or terminalizing

The Phase 1 placeholder hook in `_handle_task_status_transition_approval_reset(...)` should become real in this phase.

---

## 4. Ordered implementation tasks

## Task 1 — Inventory current task-status mutation points

Before changing semantics, audit `hermes_cli/kanban_db.py` for every helper that can persist a task status transition.

Concrete targets to inspect:
- `complete_task(...)`
- `archive_task(...)`
- `block_task(...)`
- `unblock_task(...)`
- `schedule_task(...)`
- task claim/reclaim helpers that demote or restore status
- any promotion/demotion helper that issues direct `UPDATE tasks SET status = ...`

Goal:
- identify which codepaths must route through the new aggregate/reset authority
- avoid leaving one stray direct transition that silently skips approval reset semantics

## Task 2 — Replace the Phase 1 placeholder with real approval-reset transition wiring

Turn `_handle_task_status_transition_approval_reset(...)` into a real kernel operation.

Implementation requirements:
- no-op when status does not change
- no-op when new status is one of `approving`, `done`, or `archived`
- otherwise bulk-reset all approval rows for that task using the first-class reset semantics from Phase 1
- preserve transaction boundaries so the task-status change and approval reset happen atomically when they belong to the same mutation flow

Recommended refinement:
- introduce a task-scoped bulk reset helper rather than looping through `reset_task_approval(...)` one row at a time inside nested write transactions
- keep the single-row `reset_task_approval(...)` helper as the canonical row-level primitive, but add an internal bulk version for transactional task transitions

## Task 3 — Introduce one authoritative aggregate resolver helper

Add a dedicated internal resolver in `hermes_cli/kanban_db.py` that computes the correct task outcome from current approval rows plus approval-run activity.

Minimum inputs:
- `task_id`
- current task status
- current approval rows for the task
- whether any approval run is still active

Minimum outputs/behaviors:
- “stay `approving`”
- “move to `done`”
- “move to `todo` and reset approvals now”
- “no approval effect because no approval rows exist”

Implementation notes:
- treat the master spec’s aggregate rules as the only truth source
- do not encode CLI-specific policy in this helper
- do not couple it to future dispatcher spawning logic

## Task 4 — Wire `complete_task(...)` through the aggregate resolver

Update `complete_task(...)` so completion does not hardcode `done` when approval rows exist.

Concrete behavior change:
- if no approval rows exist, retain current `-> done` behavior
- if one or more approval rows exist, persist `-> approving` instead
- after the row update, run the aggregate resolver inside the same write transaction so the persisted terminal state is consistent with current approval rows and approval-run activity

Important guardrails:
- preserve existing run-closing/result-recording behavior as much as possible
- preserve non-approval completion behavior for boards that never use approvals
- do not let the result summary / metadata write path fork approval semantics in a second location

## Task 5 — Implement approval-run activity checks for aggregate resolution

Phase 2 is not adding approval-run execution, but the resolver still needs a persisted definition of “active approval run” for rejection-cycle tests and later phases.

Implementation work:
- add a small internal helper that answers whether the task currently has any active approval runs
- base it on `task_approval_runs.status` and/or `task_approvals.current_run_id` in a way that is compatible with the runtime spec’s future statuses
- document in code which statuses are considered active in Phase 2

Recommended active set for planning purposes:
- treat `task_approval_runs.status='running'` as active
- avoid prematurely treating terminal statuses (`approved`, `rejected`, `escalated`, `failed`, `crashed`, `timed_out`, `reclaimed`, `spawn_failed`) as active

## Task 6 — Encode the rejection-cycle resolver path

Implement the branch where a task with one or more `rejected` approvals is resolved.

Required semantics:
- if any approval run is still active, task stays `approving`
- once no approval run is active, task becomes `todo`
- in the same transaction as that `todo` transition, all approval rows are bulk-reset to `requested`
- reset must clear `comment_id`, claim fields, `current_run_id`, `consecutive_failures`, and `last_failure_error`

This is the only Phase 2 path that should return a task from approval back into execution.

## Task 7 — Encode `approving -> done` resolution, including opt-out semantics

Implement the branch where the task can finish approval.

Required semantics:
- `approved` rows are satisfied
- `escalated` / `failed` rows remain present but are treated as opted out
- task may reach `done` only when there is no `requested`, no `rejected`, and no active approval run
- if the approval set becomes empty while task is `approving`, resolver moves the task to `done`

Be explicit in tests and code comments that the master spec wins here: `escalated` / `failed` do not themselves block `done`; the human approval row created on top is the real blocking gate.

## Task 8 — Route all non-terminal status transitions through the reset hook

After the resolver exists, update the rest of the task-status mutation helpers so any transition out of `approving` into a non-`approving` / non-`done` / non-`archived` state triggers approval reset.

Likely cases to cover:
- reclaim/demotion helpers that set `todo` or `ready`
- block/unblock paths
- scheduling/promote/demote paths if they can touch an `approving` task

The goal is not to add new user-visible flows, but to make the invariant true regardless of which internal codepath performs the status change.

## Task 9 — Add narrow defensive assertions around impossible state combinations

Add low-cost guardrails in the resolver/helper layer for cases that would otherwise hide semantic drift, for example:
- task in `done` with a still-active approval run
- task in `approving` with zero approvals and no active approval run after recompute
- task leaving `approving` without routing through the reset hook

Do not add speculative recovery logic. Assertions or tightly-scoped normalization are enough if they make tests clearer and reduce future drift.

---

## 5. Required tests

## 5.1 Completion-time entry into `approving`

Add tests covering:
- completing a task with no approval rows still ends in `done`
- completing a task with a human approval row ends in `approving`
- completing a task with an agent approval row ends in `approving`
- completion still records result/run history correctly when the terminal task status becomes `approving` instead of `done`

Likely file:
- `tests/hermes_cli/test_kanban_db.py`

## 5.2 Aggregate `approving -> done` resolution

Add tests covering:
- `approving` task with all approvals `approved` and no active approval runs resolves to `done`
- `approving` task with one `approved` agent row plus one `approved` human row resolves to `done`
- `approving` task with `escalated` or `failed` agent row plus approved human gate resolves to `done`
- `approving` task with last approval row removed resolves to `done`

Likely file:
- `tests/hermes_cli/test_kanban_approvals_db.py`
- or a new `tests/hermes_cli/test_kanban_approvals_lifecycle.py`

## 5.3 Rejection-cycle behavior

Add tests covering:
- one approval row becomes `rejected` while another approval run is still active -> task remains `approving`
- once the last active approval run is no longer active, the same task resolves to `todo`
- the `approving -> todo` transition resets all approval rows in the same transaction
- after reset, all rows are back to `requested`
- after reset, all `comment_id` values are cleared
- after reset, all failure counters and last-failure errors are cleared

This is the most important semantic test cluster in Phase 2.

## 5.4 Reset-on-task-transition coverage

Add tests proving approval rows reset whenever the task leaves `approving`, `done`, or `archived` for another state.

Minimum scenarios:
- direct/internal `approving -> todo` path triggers reset
- internal `approving -> blocked` path triggers reset if such a transition helper exists
- transitions into `done` and `archived` do **not** reset
- transitions that never changed status do not reset

## 5.5 Opt-out semantics for `escalated` / `failed`

Add tests proving:
- `escalated` alone keeps the task in `approving` while the human gate is unresolved
- `failed` alone keeps the task in `approving` while the human gate is unresolved
- once the human gate is approved and no run is active, `escalated` / `failed` rows remaining in place do not block `done`

## 5.6 No-regression coverage for boards without approvals

Run or extend existing tests around:
- normal completion
- archiving
- ready/todo promotion
- reclaim/demotion flows

Goal: confirm that approval-aware routing is inert when a task has no approval rows.

---

## 6. Verification commands

Run from the dedicated worktree only:

```bash
cd /home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521
pytest tests/hermes_cli/test_kanban_db.py -q
pytest tests/hermes_cli/test_kanban_approvals_db.py -q
```

If lifecycle tests move into a focused file:

```bash
cd /home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521
pytest tests/hermes_cli/test_kanban_approvals_lifecycle.py -q
pytest tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_approvals_db.py -q
```

Recommended targeted sweep during implementation:

```bash
cd /home/hermes/worktrees/hermes-agent/kanban-task-approvals-20260521
pytest tests/hermes_cli/test_kanban_db.py -k "complete or archive or reclaim or approving" -q
pytest tests/hermes_cli/test_kanban_approvals_db.py -k "approval and (done or rejected or escalated or failed or reset)" -q
```

---

## 7. Phase 2 exit criteria

Phase 2 is complete only when all of the following are true:
- `complete_task(...)` sends approval-bearing tasks to `approving`, not directly to `done`
- one authoritative kernel helper owns approval-aware task aggregate resolution
- a task remains `approving` while any approval row is `requested`, `escalated`, or `failed`
- a task remains `approving` while any approval run for that task is active
- a `rejected` approval does not move the task to `todo` until the final active approval run finishes
- the eventual `approving -> todo` rejection transition resets all approval rows in the same transaction
- a task moves to `done` only under the exact master-spec rule set
- `escalated` / `failed` are treated as opt-out agent rows and do not independently block `done`
- approval rows reset whenever a task moves to a status other than `approving`, `done`, or `archived`
- non-approval task behavior still passes targeted DB tests

---

## 8. Explicit deferrals to Phase 3+

Do not pull these into Phase 2:
- `hermes kanban approval add|list|remove|approve|reject|reset`
- task-show approval rendering work
- dispatcher-owned claiming or spawning of agent approvers
- approval-run stale detection, heartbeat, timeout, or reclaim behavior
- approval-agent prompt contracts or output parsing
- automatic human-gate creation during `escalated` / `failed` result application
- dashboard UI or gateway approval UX

Phase 2 should end with the lifecycle semantics in place, but without expanding the user-facing or runtime surface beyond what the kernel and tests need.