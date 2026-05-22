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

This plan now includes one shameless late Phase 2 extension: add a live approval-row `running` status that separates “pending and runnable” from “claimed and in flight” without changing the aggregate rule that either state keeps the parent task in `approving`.

This phase must deliver:
- one authoritative aggregate resolver in kernel code
- completion-time routing where successful task-worker completion goes:
  - directly to `done` when no approval rows exist
  - to `approving` only after attached approval rows are reset to `requested`
- exact `approving -> done` behavior from the master spec
- exact rejection-cycle behavior from the master spec
- explicit reset wiring for the only approval-reset boundaries in this slice:
  - successful task completion
  - rejection-driven `approving -> todo`
  - manual movement from `done` / `approving` / `archived` to any non-approval-bearing status
- explicit handling for `escalated` / `failed` as agent opt-out states that do not themselves block `done`
- guardrails so stale approval-worker results cannot incorrectly mutate approval/task state

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
- decide whether the task must remain `approving`, move to `done`, or move to `todo`
- perform rejection-cycle resets transactionally when the task returns to `todo`
- leave non-approval task semantics unchanged when a task has no approval rows

Authority rule for this phase:
- approval-row state is the final business authority
- approval-run rows are execution/audit bookkeeping, not the source of truth for lifecycle resolution
- only approval rows in `requested` or `running` participate in live unresolved approval decision-making
- `requested` means pending and runnable/unclaimed
- `running` means the row has been claimed by an agent approval run and is currently in flight
- aggregate priority is exact: any `running` -> `approving`; else any `rejected` -> `todo`; else any `requested` -> `approving`; else `done`
- if an approval worker later reports against a row that is no longer in the live owned `running` state for that same run, that result must have no effect

A good shape would be one read helper plus one mutation helper, for example:
- `_compute_task_approval_aggregate_state(...)`
- `_apply_task_approval_aggregate_transition(...)`

Exact names may differ, but the implementation should end with one obvious authority point.

## 3.2 Entering `approving`

When `complete_task(...)` finishes normally:
- if the task has no approval rows, it moves directly to `done`
- if the task has one or more approval rows, reset all attached approval rows to `requested` and then move the task to `approving`

Important details:
- the worker/completion path does not decide whether approvals are required based on worker output
- the kernel checks for attached approval rows
- resetting to `requested` on successful completion is deliberate, even if the rows already happen to be `requested`
- any previously non-requested approval state from an older completion cycle must not be reused as if it approved the newly finished task result

This preserves one explicit semantic boundary:
- successful task-worker completion means “a fresh result now exists, so attached approvers must be re-requested before approval begins”

## 3.3 While unresolved approvals exist

A task must remain `approving` while any approval row is `requested` or `running`.

Important clarifications:
- `requested` and `running` are the live blocking approval-row states
- `escalated` and `failed` are opt-out states for agent approval rows and do not themselves block movement
- `approved` is satisfied
- `rejected` triggers the rejection-cycle path described below
- the blocking human gate in escalation/failure flows is the human approval row that remains `requested`, not the original `escalated` / `failed` agent row

## 3.4 Rejection cycle

If any approval row is `rejected` and no approval row remains `running`, the resolver must implement the full rejection cycle from the master spec:
1. rejection is recorded first
2. task transitions from `approving` to `todo`
3. in that same transaction, reset all approval rows to `requested`
4. in that same transaction, clear each row’s `comment_id`
5. in that same transaction, clear each row’s consecutive-failure state

Important clarifications:
- a still-`running` approval row intentionally outranks a recorded `rejected` row so already-running approvers can finish and contribute stacking feedback/comments
- a merely `requested` approval row does not outrank `rejected`; once no row remains `running`, the rejection cycle fires immediately
- in the intended model, an approval result is only applied when the corresponding approval row is still in the live owned `running` state for that same run
- if a stale worker later reports against a row that has already been moved out of that owned `running` state, that late result is discarded and does not alter task or approval state
- explicit/early cancellation of the stale worker is not required in v1

## 3.5 Done rule

A task may move from `approving` to `done` only when:
- there is no approval row in `requested`
- there is no approval row in `running`
- there is no approval row in `rejected`
- every remaining approval row is in `approved`, `escalated`, or `failed`

Critical implications to preserve:
- `escalated` and `failed` are opt-out states for agent approval rows
- those rows remain present for audit/history but do not independently block `done`
- the actual blocking gate is the human approval row that exists on top
- removing the last approval row while the task is `approving` should move the task to `done`

## 3.6 Reset-on-task-transition rule

Approval reset boundaries in this slice are only:
1. successful task completion
2. rejection-driven `approving -> todo`
3. manual movement from one of `done` / `approving` / `archived` to any non-approval-bearing status

Do not specify a broader generic rule like “every transition outside approving/done/archived resets approvals.” The reset contract should stay attached to those explicit semantic boundaries.

Important distinctions to preserve:
- successful task-worker completion is approval-aware and may route to `done` or `approving` depending on whether approval rows exist
- an explicit manual move to `done` is a pure override and must not invent a fresh approval cycle or route through `approving`

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

## Task 2 — Replace the Phase 1 placeholder with explicit per-operation reset ownership

Do **not** introduce a new generic centralized “task status A -> status B” approval-reset helper. Hermes does not currently have one shared status-transition function for tasks, and this phase should not invent one just to carry approval semantics.

Instead, wire approval reset only in the concrete operations that actually own one of the explicit reset boundaries.

Implementation requirements:
- no-op when status does not change
- no-op for ordinary non-boundary status transitions that do not cross an explicit approval reset boundary
- bulk-reset all approval rows only in the concrete operations that own the three explicit boundaries:
  - successful completion with attached approval rows
  - rejection-driven return to `todo`
  - manual movement from `done` / `approving` / `archived` to a non-approval-bearing status
- preserve transaction boundaries so the task-status change and approval reset happen atomically when they belong to the same mutation flow

Concrete function ownership for this phase:
- `complete_task(...)`
  - owns the “successful task completion” reset boundary
  - if approval rows exist, it resets them to `requested` and moves the task to `approving`
- the approval-result application/finalization helper introduced or extended in this phase
  - owns the rejection-driven `approving -> todo` reset boundary
  - when a `rejected` result is authoritatively recorded, it resets approval rows in the same transaction as the task move back to `todo`
- any explicit/manual status override helper that can move a task out of `done`, `approving`, or `archived` into a non-approval-bearing status
  - today this may be no existing function at all; if such a helper already exists or is added later, that exact helper must own the reset itself

Concrete functions that should **not** care about approval reset in this phase **as currently implemented** because their allowed source statuses do not cross the defined reset boundaries:
- `recompute_ready(...)`
- `claim_task(...)`
- `reclaim_task(...)`
- `block_task(...)` in its current `running|ready -> blocked` shape
- `unblock_task(...)` in its current `blocked|scheduled -> ready|todo` shape
- `archive_task(...)` because `archived` is itself approval-bearing
- `schedule_task(...)` in its current `todo|ready|running|blocked -> scheduled` shape
- normal create/specify/promote/spawn flows

Important qualification:
- this is a statement about the **current allowed source statuses**, not a permanent exemption by function name
- if a helper such as `block_task(...)`, `schedule_task(...)`, or another status mutator later becomes allowed to move a task out of `done`, `approving`, or `archived` into a non-approval-bearing status, that exact helper becomes an approval-reset owner for that path

Recommended refinement:
- introduce a task-scoped internal bulk reset primitive rather than looping through `reset_task_approval(...)` one row at a time inside nested write transactions
- keep the single-row `reset_task_approval(...)` helper as the canonical row-level primitive
- do **not** let that bulk primitive turn into a generic all-status-transition router; it should be called only from the concrete owning operations above

## Task 3 — Introduce one authoritative aggregate resolver helper

Add a dedicated internal resolver in `hermes_cli/kanban_db.py` that computes the correct task outcome from current approval rows.

Minimum inputs:
- `task_id`
- current task status
- current approval rows for the task

Minimum outputs/behaviors:
- “stay `approving`”
- “move to `done`”
- “move to `todo` and reset approvals now”
- “while already in `approving`, no approval rows remain, so move to `done`”

Implementation notes:
- treat the master spec’s aggregate rules as the only truth source
- do not encode CLI-specific policy in this helper
- do not couple it to future dispatcher spawning logic
- keep run-liveness/stale-result handling outside the aggregate decision itself except where needed to reject stale finalization attempts transactionally

## Task 4 — Wire `complete_task(...)` through the aggregate resolver

Update `complete_task(...)` so completion does not hardcode `done` when approval rows exist.

Concrete behavior change:
- if no approval rows exist, retain current `-> done` behavior
- if one or more approval rows exist, reset them all to `requested` in the same transaction and persist `-> approving`
- keep this completion path distinct from explicit manual `done` commands; only successful task-worker completion should invoke this approval-aware routing

Important guardrails:
- preserve existing run-closing/result-recording behavior as much as possible
- preserve non-approval completion behavior for boards that never use approvals
- do not let the result summary / metadata write path fork approval semantics in a second location

## Task 5 — Tighten stale-result finalization semantics around row ownership

Phase 2 is not adding approval-run execution, but it should lock down the business rule that approval results only apply while the approval row is still in the live owned running state for the same run.

Implementation work:
- define the finalization/update shape so an approval result is applied only by transactionally updating the same approval row/run pair it still owns
- require the write to match at least:
  - the target approval row id
  - `status = 'running'`
  - the corresponding `current_run_id` (or equivalent run-ownership marker)
- if that conditional write matches no row, treat the result as stale/discarded with no business effect

This is tighter than a generic “discard if status is no longer live” rule because it binds the finalization to both:
- the live approval-row state
- the exact run that still owns the row

## Task 6 — Encode the rejection-cycle resolver path

Implement the branch where a task with one or more `rejected` approvals is resolved.

Required semantics:
- once a rejection is authoritatively recorded on a row, task becomes `todo`
- in the same transaction as that `todo` transition, all approval rows are bulk-reset to `requested`
- reset must clear `comment_id`, claim fields, `current_run_id`, `consecutive_failures`, and `last_failure_error`

This is the only Phase 2 path that should return a task from approval back into execution.

## Task 7 — Encode `approving -> done` resolution, including opt-out semantics

Implement the branch where the task can finish approval.

Required semantics:
- `approved` rows are satisfied
- `escalated` / `failed` rows remain present but are treated as opted out
- task may reach `done` only when there is no `requested`, no `running`, and no `rejected`
- if the approval set becomes empty while task is `approving`, resolver moves the task to `done`

Be explicit in tests and code comments that the master spec wins here: `escalated` / `failed` do not themselves block `done`; the human approval row created on top is the real blocking gate.

## Task 8 — Route only the explicit manual exit boundaries through the owning operations

After the resolver exists, update only the concrete status-mutation operations that actually can manually move a task from one of `done` / `approving` / `archived` to a non-approval-bearing status.

Likely cases to cover:
- explicit/manual CLI-facing status mutation helpers that can move a task out of `done`, `approving`, or `archived`
- recovery/admin helpers that can force a task back into `todo`, `ready`, `blocked`, or `scheduled`

Important implementation note:
- if no such helper currently exists, do not invent one in Phase 2 just to satisfy the plan
- instead, document that the boundary is part of the contract and must be implemented in the exact operation that eventually provides that manual move

Do not broaden this into “all non-terminal transitions reset approvals.” Keep it attached to the explicit manual exit boundaries above.

## Task 9 — Add narrow defensive assertions around impossible state combinations

Add low-cost guardrails in the resolver/helper layer for cases that would otherwise hide semantic drift, for example:
- task in `approving` with zero approvals after recompute
- a completion path that enters `approving` without first resetting attached approvals to `requested`
- a stale approval result attempting to finalize a row/run pair it no longer owns

Do not add speculative recovery logic. Assertions or tightly-scoped normalization are enough if they make tests clearer and reduce future drift.

---

## 5. Required tests

## 5.1 Completion-time entry into `approving`

Add tests covering:
- completing a task with no approval rows still ends in `done`
- completing a task with a human approval row resets that row to `requested` and ends in `approving`
- completing a task with an agent approval row resets that row to `requested` and ends in `approving`
- completion still records result/run history correctly when the terminal task status becomes `approving` instead of `done`
- explicit manual move to `done` does not invent or re-trigger an approval cycle

Likely file:
- `tests/hermes_cli/test_kanban_db.py`

## 5.2 Aggregate `approving -> done` resolution

Add tests covering:
- `approving` task with no approval rows resolves to `done`
- `approving` task with all approvals `approved` resolves to `done`
- `approving` task with one `approved` agent row plus one `approved` human row resolves to `done`
- `approving` task with `escalated` or `failed` agent row plus approved human gate resolves to `done`
- `approving` task with last approval row removed resolves to `done`

Likely file:
- `tests/hermes_cli/test_kanban_approvals_db.py`
- or a new `tests/hermes_cli/test_kanban_approvals_lifecycle.py`

## 5.3 Rejection-cycle behavior

Add tests covering:
- one approval row becomes `rejected` -> task immediately moves from `approving` to `todo`
- the `approving -> todo` transition resets all approval rows in the same transaction
- after reset, all rows are back to `requested`
- after reset, all `comment_id` values are cleared
- after reset, all failure counters and last-failure errors are cleared
- a stale later result for the old run has no effect after the rejection/reset has already happened

This is the most important semantic test cluster in Phase 2.

## 5.4 Reset-boundary coverage

Add tests proving approval rows reset only at the explicit boundaries described above.

Minimum scenarios:
- successful completion with approvals triggers reset-to-requested before entering `approving`
- rejection-driven `approving -> todo` triggers reset
- manual move from `approving` to `todo` triggers reset if that helper exists
- manual move from `done` to `todo` or `ready` triggers reset if that helper exists
- transitions into `done` and `archived` do **not** themselves reset as a generic side effect
- transitions that never changed status do not reset

## 5.5 Opt-out semantics for `escalated` / `failed`

Add tests proving:
- `escalated` alone does not block `done`; the unresolved blocker is the human gate row that remains `requested`
- `failed` alone does not block `done`; the unresolved blocker is the human gate row that remains `requested`
- once the human gate is approved, remaining `escalated` / `failed` agent rows do not block `done`

## 5.6 Stale-result ownership enforcement

Add tests proving:
- a finalization attempt succeeds only when the approval row is still `running` and still owned by the same `current_run_id`
- if the row status is no longer `running`, the result is discarded
- if `current_run_id` no longer matches, the result is discarded
- discarded stale results do not alter task status, approval status, or audit-facing live row state

## 5.7 No-regression coverage for boards without approvals

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
pytest tests/hermes_cli/test_kanban_db.py -k "complete or archive or reclaim or approving or done" -q
pytest tests/hermes_cli/test_kanban_approvals_db.py -k "approval and (done or rejected or escalated or failed or reset or stale)" -q
```

---

## 7. Phase 2 exit criteria

Phase 2 is complete only when all of the following are true:
- successful `complete_task(...)` sends tasks with no approval rows directly to `done`
- successful `complete_task(...)` sends approval-bearing tasks to `approving` only after resetting attached approvals to `requested`
- one authoritative kernel helper owns approval-aware task aggregate resolution
- a task remains `approving` only while at least one approval row is still `requested` or `running`
- a `rejected` approval moves the task back to `todo` and resets approval rows in the same transaction
- a task moves to `done` only under the exact master-spec rule set
- `escalated` / `failed` are treated as opt-out agent rows and do not independently block `done`
- manual `done` remains an explicit override and does not invent a fresh approval cycle
- stale approval results can finalize only when they still own a `running` row via the matching run id
- non-approval task behavior still passes targeted DB tests

---

## 8. Explicit deferrals to Phase 3+

Do not pull these into Phase 2:
- `hermes kanban approval add|list|remove|approve|reject|reset`
- task-show approval rendering work
- dispatcher-owned claiming or spawning of agent approvers
- approval-run stale detection, heartbeat, timeout, or reclaim behavior beyond the narrow stale-result ownership guard described above
- approval-agent prompt contracts or output parsing
- automatic human-gate creation during `escalated` / `failed` result application
- dashboard UI or gateway approval UX

Phase 2 should end with the lifecycle semantics in place, but without expanding the user-facing or runtime surface beyond what the kernel and tests need.