# Kanban Task Approvals — Approval Event and Worker Surface Hardening Spec

Status: Draft
Depends on:
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/01-db-and-migration.md`
- `docs/specs/kanban-task-approvals/03-cli-and-manual-approval-workflows.md`
- `docs/specs/kanban-task-approvals/04-autonomous-agent-approval-runtime.md`
- Phase 4 autonomous approval runtime behavior already landed on the same branch/PR lane

Scope: Phase 5 semantic/runtime cleanup for approval status naming, approval/task event contracts, structured approval handoff via tools, approval-worker tool gating, and task-centric human approval decisions. Dashboard UX remains out of scope.

---

## 1) Goal

Tighten the approval runtime contract so the event stream, task status names, worker-vs-approver tool surface, and human CLI ergonomics all match the intended lifecycle semantics.

After this phase:

1. the task status formerly named `approving` is renamed to `approval`,
2. worker completion into approval emits `awaiting_approval` instead of overloading `completed`,
3. approval-row lifecycle emits a compact, stable event set with fixed payloads,
4. approval workers hand off decisions through a structured tool surface instead of log parsing,
5. task workers and approval workers see distinct Kanban mutation tools,
6. human `approve` / `reject` CLI operations target the task rather than an approval row id.

---

## 2) Explicit scope for this phase

This phase must deliver:

1. renaming task status `approving` -> `approval` across kernel/runtime/CLI/spec-facing contracts,
2. exact event names and payloads for task-to-approval and approval-row lifecycle transitions,
3. a structured `kanban_approval` tool as the approval-worker handoff path,
4. approval-worker tool gating driven by approval env rather than raw log parsing,
5. task-centric human `approve` / `reject` CLI commands,
6. focused tests covering the renamed status, event emissions, tool gating, and task-centric manual decisions.

This phase must **not** deliver:

- dashboard UI,
- gateway buttons or interactive approval UX,
- new multi-step approval workflow features,
- generalized event taxonomy cleanup outside task approvals,
- new task-creation flags that inline approval setup into `hermes kanban create`.

---

## 3) Canonical semantic changes

### 3.1 Task status rename

The task status formerly named `approving` is renamed to `approval`.

Meaning:
- `running`: implementation work is still in progress.
- `approval`: implementation work finished successfully, but the task is blocked on one or more approval rows.
- `done`: implementation work finished and all approval gates are satisfied.
- `todo`: implementation work must resume because an approval rejected the result.

All kernel helpers, dispatcher runnable predicates, CLI validation, and specs must use `approval` as the canonical status name after this phase.

### 3.2 Run outcome vs task event

A successful task-worker run that hands off into approval still ends with run outcome `completed`.

The task event emitted from that handoff is **not** `completed`.

Rules:
- use `awaiting_approval` when a worker run completes and the task enters `approval`,
- reserve `completed` for actual task movement to `done`, including `approval -> done`.

This separation is required so downstream logic such as respawn guards can distinguish:
- a successful worker run that produced an approval-gated result,
- a truly terminal task completion.

---

## 4) Exact event kinds and payloads

### 4.1 `awaiting_approval`

Emit `awaiting_approval` when a task worker finishes successfully and the task moves from `running` into `approval` because attached approval rows exist.

Payload:

```json
{
  "task_status": "approval",
  "run_id": "<task run id that just completed>"
}
```

Rules:
- `run_id` is the parent task-worker run id, not an approval-run id.
- this event replaces the previous overloaded use of `completed` for approval handoff.
- this event is task-lifecycle scoped, not approval-row scoped.

### 4.2 `approval_requested`

Emit `approval_requested` when an approval row becomes meaningfully live in `requested` state.

Payload:

```json
{
  "approval_id": "<id>",
  "approver_type": "human | agent",
  "approver_profile": "<agent profile>",
  "approver_skill": "<agent skill>",
  "requested_by_approval_id": "<id of escalated approver>"
}
```

Rules:
- emit when a new approval row is added in `requested` state,
- emit when an existing row is reset from a non-`requested` state back to `requested`,
- emit when escalation creates or re-requests the human approval gate,
- do **not** emit when a row is already `requested` and remains `requested` with no meaningful state transition,
- `requested_by_approval_id` is present only when the `requested` transition was caused by another approval row escalating.

### 4.3 `approval_removed`

Emit `approval_removed` when a CLI/operator flow removes an approval row.

Payload:

```json
{
  "approval_id": "<id>",
  "approver_type": "human | agent",
  "approver_profile": "<agent profile>",
  "approver_skill": "<agent skill>"
}
```

Rules:
- emit exactly once for the removed row,
- emit before the row becomes impossible to audit from the event stream,
- this event covers explicit approval-row removal only.

### 4.4 `approval_decided`

Emit `approval_decided` when an approval row authoritatively becomes `approved`, `rejected`, or `escalated`.

Payload:

```json
{
  "approval_id": "<id>",
  "approver_type": "human | agent",
  "approver_profile": "<agent profile>",
  "approver_skill": "<agent skill>",
  "decision": "approved | rejected | escalated",
  "comment_id": "<comment id>",
  "approval_run_id": "<agent run id>"
}
```

Rules:
- emit for both manual human decisions and autonomous agent decisions,
- `approval_run_id` is present only for agent decisions that came from an approval run,
- `comment_id` is present only when the decision path wrote a task comment,
- `decision` is the business decision value, not the approval-run outcome enum.

### 4.5 `completed`

Emit `completed` only when the task actually transitions to `done`.

Rules:
- this includes `approval -> done`,
- this does **not** include `running -> approval`,
- this remains the canonical terminal task-success event.

### 4.6 Escalation event ordering

Escalation can emit both:

1. `approval_decided` with `decision = escalated`, and
2. `approval_requested` for the human gate

if and only if the human gate was newly created or reset from a non-`requested` state.

If a human approval row already exists and is already `requested`, emit only `approval_decided`.

---

## 5) Structured approval handoff via tool

### 5.1 `kanban_approval` replaces log-parsing as the decision handoff path

Approval workers must not rely on the parent runtime scraping final CLI/log output to discover their decision.

This phase adds a structured approval-worker tool:

```text
kanban_approval
```

Its job is the approval-worker counterpart to `kanban_complete`.

Minimum contract:
- accepts one structured decision payload,
- validates that the worker owns the live approval row/run,
- applies the kernel-owned approval-decision path directly,
- returns structured success/error JSON to the model.

### 5.2 Tool payload shape

`kanban_approval` must accept exactly this payload shape:

```json
{
  "decision": "approved|rejected|escalated",
  "comment": "<comment>"
}
```

Rules:
- `decision` is required.
- `comment` is optional and may be omitted or passed as an empty string.
- no additional payload keys are part of the tool contract for this slice.

The implementation must not require the approval worker to synthesize raw CLI output that another process re-parses later.

### 5.3 Result application path

`kanban_approval` must route directly into the kernel-owned helpers that authoritatively:
- write the optional comment,
- finalize the approval row and approval run,
- emit `approval_decided`,
- emit `approval_requested` when escalation newly requests the human gate,
- recompute task state,
- emit `completed` only if the aggregate moves the task to `done`.

---

## 6) Worker-vs-approver env and tool gating

### 6.1 Reuse `HERMES_KANBAN_TASK`

Both normal task workers and approval workers continue to use `HERMES_KANBAN_TASK` for task scoping and task-ownership checks.

This phase does **not** split workers and approvers by removing task scope from approval workers.

### 6.2 Branch on approval env for the approver surface

Approval-worker detection must branch on approval env, using:
- `HERMES_KANBAN_APPROVAL_ID`

An approval worker therefore has:
- `HERMES_KANBAN_TASK` set,
- `HERMES_KANBAN_APPROVAL_ID` set,
- and the live approval-run id env required for ownership validation.

A normal task worker has:
- `HERMES_KANBAN_TASK` set,
- no approval env.

### 6.3 Tool visibility rules

Task-worker tool surface:
- can see `kanban_complete`, `kanban_block`, `kanban_heartbeat`, and related task-worker lifecycle tools,
- must not see `kanban_approval`.

Approval-worker tool surface:
- can see `kanban_approval`,
- must not see `kanban_complete`,
- may still use task-scoped read/comment helpers only if those helpers are explicitly allowed for approval workers.

The split must be enforced by schema gating, not just prompt instructions.

### 6.4 No accidental env inheritance

Worker spawn paths must ensure:
- normal task workers do not inherit approval env vars by mistake,
- approval workers do not inherit the normal task-worker mutation surface by virtue of `HERMES_KANBAN_TASK` alone.

`HERMES_KANBAN_APPROVAL_ID` is the approval-surface discriminator.

---

## 7) Task-centric human approve/reject CLI

### 7.1 Command forms

Human manual decisions should target the task, not an approval row id.

Required forms:

```bash
hermes kanban approval approve <task_id> [--comment "..."]
hermes kanban approval reject <task_id> [--comment "..."]
```

Rules:
- resolve the single human approval row for the task internally,
- reject the command when no human approval row exists,
- reject the command when the task is not currently in `approval`,
- reject the command if future invariants are violated and multiple human approval rows somehow exist.

### 7.2 Scope boundary

This change applies only to human `approve` / `reject` CLI flows.

This phase does not require converting every approval subcommand to task-centric addressing. Row-id-oriented remove/reset/admin surfaces may remain as-is unless separately tightened.

---

## 8) File-by-file implementation plan

### `hermes_cli/kanban_db.py`
Modify:
- task status constants and validation to use `approval`,
- `complete_task(...)` so worker handoff into approval emits `awaiting_approval` with `{task_status: "approval", run_id: <task_run_id>}` and reserves `completed` for actual `done` transitions,
- any helper that still treats approval handoff as a terminal completion event.

### `hermes_cli/kanban_approvals_db.py`
Modify:
- aggregate task-state helpers to use `approval`,
- approval request/reset/remove/decision helpers so they emit `approval_requested`, `approval_removed`, and `approval_decided` with the exact payload contracts above,
- escalation reuse logic so `approval_requested` is emitted only when the human gate is newly requested/reset,
- manual human decision resolution helpers so CLI can decide by task id.

### `tools/kanban_tools.py`
Modify:
- tool gating so task workers and approval workers see distinct lifecycle tools,
- add `kanban_approval`,
- hide `kanban_complete` from approval workers,
- use `HERMES_KANBAN_APPROVAL_ID` as the approval-worker discriminator.

### Approval worker spawn/runtime files
Modify:
- env wiring so approval workers receive `HERMES_KANBAN_APPROVAL_ID` and the live approval-run id,
- task-worker spawns so they do not inherit approval env accidentally,
- approval result plumbing so it uses `kanban_approval` rather than log parsing.

### `hermes_cli/kanban_approvals.py`
Modify:
- `approve` / `reject` command parsing to take `task_id`,
- output formatting to surface task-centric decisions while still reporting the resolved approval row in JSON if useful.

### Specs to keep aligned in the same branch
Patch:
- `00-master-spec.md`
- `01-db-and-migration.md`
- `02-runtime-and-dispatch.md`
- `03-cli-and-manual-approval-workflows.md`
- `04-autonomous-agent-approval-runtime.md`

---

## 9) Exact tests to add or rewrite

### `tests/hermes_cli/test_kanban_db.py`
Add/update cases for:
- worker completion with approvals emits `awaiting_approval`, not `completed`,
- task status becomes `approval`, not `approvaling`/`approving`,
- final transition from `approval` to `done` emits `completed`,
- rejection path returns task to `todo` without reusing the handoff event as terminal success.

### `tests/hermes_cli/test_kanban_approvals_db.py`
Add/update cases for:
- `approval_requested` on add,
- `approval_requested` on reset from non-`requested`,
- no duplicate `approval_requested` emission when already `requested`,
- `approval_removed` on remove,
- `approval_decided` payloads for approved/rejected/escalated,
- escalation emitting both `approval_decided` and `approval_requested` only when the human gate newly transitions to `requested`.

### `tests/hermes_cli/test_kanban_cli.py`
Add/update cases for:
- human `approve` / `reject` by task id,
- clear error when no human approval row exists,
- clear error when the task is not in `approval`.

### `tests/tools/test_kanban_tools.py` or equivalent kanban tool tests
Add/update cases for:
- normal workers see `kanban_complete` and do not see `kanban_approval`,
- approval workers see `kanban_approval` and do not see `kanban_complete`,
- `kanban_approval` applies valid decisions through the kernel path,
- stale/mismatched approval ownership is rejected cleanly.

### Dispatcher/runtime approval tests
Add/update cases for:
- approval worker env contains `HERMES_KANBAN_APPROVAL_ID`,
- task-worker env does not inherit approval env,
- runtime no longer depends on parsing arbitrary approver CLI/log text to apply the decision.

---

## 10) Acceptance criteria

Phase 5 is complete only if all of the following hold:

1. The canonical task status name is `approval` across runtime/CLI/spec-facing contracts.
2. A worker handoff into approval emits `awaiting_approval` with the completed task-worker `run_id`.
3. `completed` is emitted only when a task actually moves to `done`.
4. Approval-row lifecycle emits exactly `approval_requested`, `approval_removed`, and `approval_decided` for the flows covered here.
5. Escalation emits both `approval_decided` and `approval_requested` only when the human gate newly becomes `requested`.
6. Approval workers apply decisions through `kanban_approval`, not through raw log parsing.
7. Approval workers do not see `kanban_complete`.
8. Normal task workers do not see `kanban_approval`.
9. Approval-worker schema gating is keyed on `HERMES_KANBAN_APPROVAL_ID` while still reusing `HERMES_KANBAN_TASK` for task scope.
10. Human `approve` / `reject` CLI flows operate by task id and resolve the single human approval row internally.
