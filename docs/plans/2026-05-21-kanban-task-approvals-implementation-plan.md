# Kanban Task Approvals Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan phase-by-phase in one shared implementation lane.

**Goal:** Implement first-class task approvals for Hermes Kanban, including schema, task-state integration, dispatcher-managed agent approvers, and CLI support, while keeping dashboard UI out of scope.

**Architecture:** The implementation is split into a small number of sequential phases that follow the existing specs and preserve one authority model: the kernel owns approval/task state, the dispatcher owns spawning and reclaiming approval workers, and agents return strict structured decisions instead of mutating state directly. The work should land in one shared branch/worktree/PR, with each phase leaving the tree in a testable state before the next begins.

**Tech Stack:** Python, SQLite, Hermes Kanban DB/runtime/CLI, pytest.

**Source specs:**
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/01-db-and-migration.md`
- `docs/specs/kanban-task-approvals/02-runtime-and-dispatch.md`

---

## 1) Delivery strategy

This feature should be implemented in one sequential lane, not parallelized across multiple branches.

Reason:
- task-state rules, DB schema, CLI, and dispatcher behavior all cross-cut the same kernel files
- approval semantics depend on one authoritative aggregate resolver
- parallel implementation would create merge churn and inconsistent intermediate semantics

Execution rule:
- complete each phase fully
- run targeted tests at the end of each phase
- do not start the next phase while the current phase still has unresolved semantic mismatches with the specs

---

## 2) Primary code areas

Expected main files:
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban.py`
- dispatcher/runtime code in the Kanban implementation path already responsible for claim/reclaim/spawn behavior
- any prompt-builder / worker-launch path used for Kanban workers

Expected main tests:
- existing Kanban DB / CLI / dispatcher tests under `tests/hermes_cli/`
- add dedicated approval-focused tests next to current Kanban tests rather than inventing a new distant test area

Likely test files to create or extend:
- `tests/hermes_cli/test_kanban_core_functionality.py`
- `tests/hermes_cli/test_kanban_db.py`
- additional focused Kanban approval tests if the existing files become unreadable

---

## 3) Phase ordering

## Phase 1 — Schema, migration, and kernel data helpers

**Objective:** Add the minimal storage model and kernel helper surface without yet wiring the full approval runtime.

**Deliverables:**
- `approving` added to task status validation
- new `task_approvals` table
- new `task_approval_runs` table
- required indexes
- application-enforced invariants:
  - one human approver per task
  - one agent `(profile, skill)` approver per task
- explicit additive migration path for existing boards
- cleanup hooks updated so archived-task permanent deletion also removes approval rows and approval runs
- first-class approval reset helper in the kernel

**Primary files:**
- `hermes_cli/kanban_db.py`

**Required tests:**
- DB init/migration tests for existing board compatibility
- invariant enforcement tests
- approval reset helper tests
- archived-task permanent deletion cleanup tests
- state-validation tests for `approving`

**Phase exit criteria:**
- existing boards initialize without approval data
- new boards create the new schema correctly
- approval rows persist across task lifecycle but can be reset back to `requested`
- deletion cleanup is correct and test-covered

**Notes:**
Do not add dispatcher behavior or CLI commands in this phase beyond whatever minimal internal helpers are needed to support the tests.

---

## Phase 2 — Aggregate task-state integration

**Objective:** Make approvals a real part of task lifecycle semantics before any agent approver execution is enabled.

**Deliverables:**
- one authoritative aggregate resolver for approval/task state
- transition to `approving` when a completed task has approval rows
- correct `approving -> done` behavior when:
  - all decision-participating approvals are approved
  - escalated/failed rows have opted out and the required human gate has approved
  - the last approval row is removed
- correct rejection-cycle behavior:
  - rejection recorded first
  - task remains `approving` while any approval run is active
  - task returns to `todo` only after all active approval runs finish
  - approval rows reset in the same transaction as the return to `todo`
- rule that approval rows are reset whenever task leaves `approving` / `done` / `archived`

**Primary files:**
- `hermes_cli/kanban_db.py`
- any existing task completion / task mutation helpers that currently finalize task status

**Required tests:**
- transition-to-approving tests
- done-rule tests
- last-approval-removed => done tests
- rejection waits for active approval runs tests
- non-approving state transition reset tests
- escalated/failed opt-out semantics tests

**Phase exit criteria:**
- task-state semantics match the master spec exactly
- no runtime spawning is required yet to validate the lifecycle rules
- the aggregate resolver is the only place deciding these transitions

**Notes:**
This phase is the semantic spine. Do not move on while any rule still depends on ad-hoc logic in multiple call sites.

---

## Phase 3 — CLI write/read surface

**Objective:** Expose approval management to users and tests through a stable CLI before enabling dispatcher-managed agent approval execution.

**Deliverables:**
- new CLI namespace:
  - `hermes kanban approval request`
  - `hermes kanban approval list`
  - `hermes kanban approval remove`
  - `hermes kanban approval approve`
  - `hermes kanban approval reject`
  - `hermes kanban approval reset`
- `hermes kanban show` extended to display approval rows and approval-run history
- CLI actions wired to kernel helpers, not inline business logic
- adding an approval to a `done` or `archived` task is rejected cleanly

**Primary files:**
- `hermes_cli/kanban.py`
- `hermes_cli/kanban_db.py`

**Required tests:**
- CLI parser/help tests
- approval request/remove/list tests
- human approve/reject/reset tests
- show output tests (`human` and `--json` modes)
- done/archived-task request rejection tests
- invariant violation tests surfaced cleanly through CLI

**Phase exit criteria:**
- all approval state can be created/inspected/changed manually from CLI
- task lifecycle responds correctly to those manual approval operations
- no dashboard work is introduced

**Notes:**
Keep the CLI slice deliberately small. Do not add create-time approval flags or dashboard affordances in this version.

---

## Phase 4 — Approval-agent runtime and dispatcher integration

**Objective:** Add real spawned agent approvers with their own claim/reclaim lifecycle and strict structured result handling.

**Deliverables:**
- approval rows become runnable units for dispatcher scheduling
- separate approval-worker spawn budget
- claim-before-spawn flow for approval rows
- `task_approval_runs` creation and lifecycle tracking
- strict approval-worker output contract:
  - `approved`
  - `rejected`
  - `escalated`
  - optional comment text
- invalid output treated as failure
- retry handling with 3-consecutive-failure limit
- `failed` row behavior that assigns/reuses the human approver and opts out of decision-making
- heartbeat / stale / reclaim behavior aligned with task workers
- late-result protection so stale approval results cannot incorrectly advance a task

**Primary files:**
- dispatcher/runtime implementation path used by Kanban today
- `hermes_cli/kanban_db.py`
- any worker-launch/prompt-builder code used for Kanban workers

**Required tests:**
- dispatcher claimability tests for approval rows
- duplicate-spawn prevention tests
- structured-output success tests
- invalid-output failure tests
- timeout/crash/reclaim tests
- 3-failure => `failed` + human gate tests
- multi-agent escalation sharing one human approver tests
- late-result ignored / task-not-readvanced tests

**Phase exit criteria:**
- approval workers are fully dispatcher-managed
- no approval worker directly mutates approval rows
- agent approval runtime semantics match the runtime spec exactly

**Notes:**
This is the highest-risk phase. Expect the most iteration here, especially around claim lifecycle and race conditions.

---

## Phase 5 — Cross-phase cleanup, compatibility, and observability polish

**Objective:** Tighten the feature after end-to-end behavior exists, without expanding scope.

**Deliverables:**
- remove duplicated or stale helper logic introduced during earlier phases
- ensure events emitted for approval lifecycle are complete and consistent
- verify cleanup and reset behavior across all task-state transitions
- verify `archive` / permanent-delete flows remain correct
- verify existing non-approval Kanban behavior still passes unchanged
- update any relevant operator/dev documentation that describes Kanban status or task inspection behavior

**Primary files:**
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban.py`
- relevant docs if needed

**Required tests:**
- regression sweep across Kanban tests
- targeted approval event/audit tests
- terminal-state cleanup tests
- compatibility tests ensuring tasks without approvals still behave exactly as before

**Phase exit criteria:**
- end-to-end approval feature is stable and documented
- spec and implementation terminology match
- no scope creep into dashboard or richer workflow systems

---

## 4) Implementation risks and where to be careful

### A. Aggregate resolver drift
Risk:
- task/approval rules get duplicated across CLI helpers, worker completion, and dispatcher code

Mitigation:
- one authoritative aggregate-state helper in kernel code
- everything else calls into it

### B. Runtime duplication / double-spawn
Risk:
- two dispatcher passes spawn the same approval agent

Mitigation:
- claim consumed atomically before process creation
- approval row is the scheduling unit

### C. Rejection timing bugs
Risk:
- task moves to `todo` too early on first rejection, while other approval runs are still active

Mitigation:
- explicit tests for “rejection recorded now, return to todo later after final active run ends”

### D. Human-gate sharing bugs
Risk:
- multiple escalated/failed agents create duplicate human rows

Mitigation:
- invariant tests and exact reuse behavior

### E. Reset semantics drift
Risk:
- some flows clear rows, others reset rows, others forget to clear failure counters/comments

Mitigation:
- one first-class approval reset helper
- use it everywhere the specs require reset semantics

---

## 5) Suggested commit/PR rhythm

Use one shared branch and keep commits aligned to phases.

Suggested commit buckets:
1. `feat(kanban): add approval schema and migration helpers`
2. `feat(kanban): integrate approval aggregate state machine`
3. `feat(kanban): add approval CLI surface`
4. `feat(kanban): dispatch and manage approval agents`
5. `test(kanban): add approval regression coverage`
6. `docs(kanban): update approval implementation notes`

Do not split runtime and aggregate semantics across separate PRs if the intermediate state violates the specs.

---

## 6) Verification commands

Exact command set may be refined once the test files are in place, but the implementation should be validated with targeted Kanban test slices first, then a broader sweep.

Expected commands during development:

```bash
pytest tests/hermes_cli/test_kanban_db.py -v
pytest tests/hermes_cli/test_kanban_core_functionality.py -v
pytest tests/hermes_cli -k approval -v
```

Before wrapping the feature:

```bash
pytest tests/hermes_cli -v
```

If approval runtime logic touches gateway-owned dispatch loops or shared worker-launch code, run the smallest relevant additional dispatcher tests rather than broad unrelated suites.

---

## 7) Definition of done

This implementation is done only when all of the following are true:
- the three task-approval specs remain accurate descriptions of the code
- DB migration is additive and safe for existing boards
- CLI approval management works end-to-end
- agent approvers are dispatcher-managed with claim/reclaim/heartbeat semantics
- invalid output is treated as failure
- 3 consecutive failures mark the row `failed` and assign/reuse one human approver
- multiple escalated/failed agent rows share one human gate
- rejection waits for active approval runs to finish before returning task to `todo`
- approval rows are reset, not deleted, on task-state transitions outside `approving` / `done` / `archived`
- dashboard UI remains untouched

---

## 8) Recommended execution posture

Use one shared implementation lane and move phase-by-phase. If a phase reveals a spec mismatch, patch the specs first, then continue. Do not let implementation become the place where unresolved semantics hide.
