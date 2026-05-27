# Kanban Task Approvals — Dispatch Budget and Approval Pass Refactor Spec

Status: Draft
Depends on:
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/04-autonomous-agent-approval-runtime.md`
- `docs/specs/kanban-task-approvals/05-approval-event-and-worker-surface-hardening.md`
- Phase 4/5 approval runtime behavior already landed on the same branch/PR lane

Scope: refactor dispatcher budgeting and approval scheduling so worker live caps, approval live caps, and per-tick spawn budgeting are explicit, testable, and independent. This slice does not change dashboard UX.

---

## 1) Goal

Make dispatcher budgeting semantics explicit and non-overlapping.

After this phase:

1. `max_spawn` means a per-dispatch spawn budget across both worker and approval launches,
2. `max_in_progress` means the live running-worker cap only,
3. `max_approvers` means the live running-approval cap only,
4. approval scheduling runs through a dedicated `dispatch_approvals_once(...)` pass,
5. approval scheduling still runs even when worker scheduling is capped or skipped,
6. CLI/config naming matches the runtime semantics.

This phase is a runtime/dispatcher refactor. It does not change approval-row business rules, event payloads, or approval worker output contracts.

---

## 2) Canonical cap semantics

### 2.1 `max_spawn`

`max_spawn` is the maximum number of new subprocess spawns a single dispatcher tick may perform.

Rules:
- it counts worker spawns and approval-worker spawns together,
- it does not count already-running workers,
- it does not count already-running approval workers,
- `None` means no per-tick spawn budget.

Examples:
- if `max_spawn = 4` and there are already 10 running workers, the tick may still launch up to 4 new subprocesses,
- if worker dispatch launches 3 workers and `max_spawn = 4`, approval dispatch may launch at most 1 approval worker in the same tick,
- if worker dispatch launches 0 workers, approval dispatch can use the full remaining budget.

### 2.2 `max_in_progress`

`max_in_progress` is the maximum number of tasks that may already be in `tasks.status = 'running'` before worker dispatch stops launching new task workers.

Rules:
- it applies only to normal worker scheduling,
- it does not apply to approval workers,
- it is evaluated against live DB state before worker spawning,
- when the running-worker count is already at or above the cap, worker dispatch launches nothing for that tick.

This cap is a live concurrency cap, not a per-tick budget.

### 2.3 `max_approvers`

`max_approvers` is the maximum number of approval rows that may already be in `task_approvals.status = 'running'` before approval dispatch stops launching new approval workers.

Rules:
- it applies only to approval-worker scheduling,
- it does not apply to normal worker scheduling,
- it is evaluated against live DB state before approval spawning,
- when the running-approver count is already at or above the cap, approval dispatch launches nothing for that tick.

This cap is a live concurrency cap, not a per-tick budget.

---

## 3) Public config and CLI surface

### 3.1 Config key rename

The canonical kanban config key for live approval-worker concurrency is:
- `kanban.max_approvers`

The old key name `kanban.max_approval_spawn` must not remain the primary documented/runtime name after this phase.

Migration/compatibility rule for this slice:
- runtime may read legacy `max_approval_spawn` as a compatibility fallback,
- but the canonical read/write/documented key is `max_approvers`.

### 3.2 CLI flag

The CLI flag is:
- `--max-approvers`

Rules:
- it overrides config `kanban.max_approvers`,
- it does not change `max_spawn`,
- it does not change `max_in_progress`.

The old `--max-approvals` spelling must not remain the primary CLI surface after this phase.

### 3.3 CLI/config resolution order

For approval live concurrency:

1. explicit CLI `--max-approvers`,
2. config `kanban.max_approvers`,
3. compatibility fallback `kanban.max_approval_spawn`,
4. default `2`.

Parsing rules:
- values must parse as positive integers,
- invalid or non-positive values fall back to the default `2`.

---

## 4) Dispatcher structure

### 4.1 Shared orchestrator remains `dispatch_once(...)`

`dispatch_once(...)` remains the public board-tick entrypoint.

It continues to own the shared maintenance phase:
- stale claim release,
- stale/crashed worker detection,
- stale/crashed approval-worker detection,
- runtime timeout enforcement,
- ready-state recomputation,
- shared `DispatchResult` assembly.

This phase must not duplicate that maintenance logic in multiple public tick functions.

### 4.2 Dedicated worker pass

`dispatch_once(...)` must run a dedicated worker scheduling pass for:
- `ready` tasks,
- `review` tasks.

This pass consumes:
- `max_spawn` as a per-tick budget,
- `max_in_progress` as a live worker cap.

This pass returns the number of worker spawns it actually performed.

### 4.3 Dedicated approval pass

Add a dedicated:
- `dispatch_approvals_once(...)`

This pass is called sequentially after worker dispatch.

It consumes:
- `max_spawn` equal to the remaining tick budget after worker dispatch,
- `max_approvers` as the live running-approver cap.

This pass owns only approval-worker scheduling. It does not run shared maintenance logic again.

### 4.4 Budget handoff

If the original `max_spawn` passed to `dispatch_once(...)` is not `None`, then:

- `worker_spawned = <number of worker subprocesses launched>`
- `remaining_spawn_budget = max(0, original_max_spawn - worker_spawned)`

`dispatch_approvals_once(...)` receives `remaining_spawn_budget`.

If the original `max_spawn` is `None`, approval dispatch also receives `None` for its spawn budget.

---

## 5) Early-return rule

Current worker-capping logic must not prevent approval scheduling from running.

Required rule:
- if worker dispatch is skipped because `max_in_progress` is exhausted,
- or because the worker tick budget is exhausted,
- approval dispatch still runs afterward using the remaining per-tick spawn budget.

Implication:
- worker scheduling and approval scheduling are sequentially budgeted,
- but they are not mutually exclusive control-flow branches.

This is the primary behavioral reason for introducing `dispatch_approvals_once(...)`.

---

## 6) Result accounting

`DispatchResult` remains the public return type of `dispatch_once(...)`.

This phase does not need a new top-level result type.

Rules:
- worker launches continue to populate `result.spawned`,
- approval launches continue to populate `result.approval_spawned`,
- `dispatch_once(...)` merges the worker-pass and approval-pass effects into one final result,
- tests must assert the two lists independently where relevant.

If helper return values are needed internally, they should be internal-only and not become a second public tick API surface unless required for testability.

---

## 7) Testability requirements

This refactor must make the following focused tests straightforward:

1. `dispatch_once(...)` launches worker tasks only up to the remaining per-tick `max_spawn` budget.
2. `dispatch_approvals_once(...)` launches approval workers only up to the remaining per-tick `max_spawn` budget.
3. worker launches consume budget before approval launches.
4. `max_in_progress` stops worker scheduling but does not stop approval scheduling.
5. `max_approvers` stops approval scheduling but does not stop worker scheduling.
6. `--max-approvers` overrides config `kanban.max_approvers`.
7. legacy config `max_approval_spawn` still works as a compatibility fallback.
8. default approval live cap remains `2` when no explicit override is present.

The test suite should not need to infer these semantics indirectly through one large combined dispatcher code path.

---

## 8) Out of scope

This phase does not:
- change approval-row aggregate business rules,
- change event names or payloads,
- change approval-worker tool gating,
- change manual approval CLI semantics beyond the concurrency-flag rename,
- redesign the dispatcher into multiple public board-tick entrypoints,
- add independent per-type fairness/priority policies between workers and approvers.

---

## 9) Acceptance criteria

This phase is complete when all of the following are true:

1. `dispatch_once(...)` still performs one shared maintenance pass per tick.
2. worker scheduling and approval scheduling run as separate sequential passes.
3. `max_spawn` is implemented as a per-tick spawn budget, not a live concurrency cap.
4. `max_in_progress` remains a worker-only live concurrency cap.
5. `max_approvers` is the canonical approval-only live concurrency cap.
6. worker scheduling being capped/skipped does not prevent approval scheduling from running.
7. CLI/config naming uses `max_approvers` / `--max-approvers` as canonical surfaces.
8. focused tests exist for budget handoff and independent worker-vs-approval caps.
9. old `max_approval_spawn` behavior no longer remains as the primary documented semantic contract.
