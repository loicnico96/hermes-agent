# Kanban Task Approvals — CLI and Manual Approval Workflows Spec

Status: Draft (Phase 3 manual workflow slice landed on the branch/PR lane; Phase 5 renames task status to `approval` and switches human `approve` / `reject` to task-centric addressing in `05-approval-event-and-worker-surface-hardening.md`)
Depends on:
- `docs/specs/kanban-task-approvals/00-master-spec.md`
- `docs/specs/kanban-task-approvals/01-db-and-migration.md`
- `docs/specs/kanban-task-approvals/02-runtime-and-dispatch.md`
- Phase 2 approval-kernel work already landed in `hermes_cli/kanban_db.py`

Scope: concrete Phase 3 CLI/operator surface for manual approval management. This spec intentionally stops before dispatcher-managed approval spawning and before approval-agent runtime execution.

---

## 1) Goal

Make the approval system manually operable through `hermes kanban` so a human can:

1. attach approval rows to a task,
2. inspect approval state on a task or across the board,
3. record human approval decisions,
4. remove or reset approvals intentionally,
5. observe the correct task lifecycle transitions without direct DB calls.

Phase 3 is the first user-facing slice built on top of the Phase 2 kernel semantics. It must reuse the existing kernel authority points rather than re-encoding approval lifecycle rules in CLI code.

---

## 2) Explicit scope for this phase

This phase must deliver:

1. a dedicated `hermes kanban approval ...` CLI namespace,
2. thin CLI wiring in `hermes_cli/kanban.py`,
3. any missing DB helpers needed to support manual approval CRUD and inspection,
4. task-centric approval visibility in `hermes kanban show`,
5. command-level tests for the manual workflow surface,
6. exact reuse of the Phase 2 kernel semantics already implemented in `hermes_cli/kanban_db.py`.

This phase must **not** deliver:

- dispatcher approval queue passes,
- approval worker spawn/claim/heartbeat/reclaim behavior,
- agent approval prompt construction or structured output parsing,
- automatic human escalation creation from live agent decisions,
- approval-run daemon/admin commands,
- create-time task flags such as `hermes kanban create --approval ...`,
- dashboard UI.

The intent is to make approvals manually testable before autonomous execution is added.

---

## 3) Authority and layering rules

### 3.1 CLI must stay thin

`hermes_cli/kanban.py` must not decide approval lifecycle semantics itself.

CLI responsibilities are limited to:
- argument parsing,
- human-friendly validation and error messages,
- output formatting,
- calling DB helpers.

All business-state transitions must remain kernel-owned in `hermes_cli/kanban_db.py`.

### 3.2 Existing kernel authority points to reuse

Phase 3 must build on the existing helpers rather than replacing them:

- `create_task_approval(...)`
- `get_task_approval(...)`
- `list_task_approvals(...)`
- `reset_task_approval(...)`
- `record_task_approval_decision(...)`
- `_compute_task_approval_aggregate_status(...)`
- `_apply_task_approval_aggregate_transition(...)`
- approval-aware `complete_task(...)`

### 3.3 New DB helpers allowed in this phase

Phase 3 may add narrow new helpers when the CLI needs operations not yet exposed cleanly, for example:

- `remove_task_approval(...)`
- task-centric read helpers that package approval rows for `kanban show`

These helpers must remain DB/kernel-scoped. They must not spawn workers or embed dispatcher policy.

---

## 4) Concrete CLI namespace

This phase adds exactly one nested approval namespace under `hermes kanban`:

```bash
hermes kanban approval ...
```

No top-level `hermes approval ...` alias is added.

### 4.1 `hermes kanban approval add`

Create one approval row.

Required forms:

```bash
hermes kanban approval add <task_id> --human
hermes kanban approval add <task_id> --agent <profile>
```

Optional flags:
- `--skill <name>` — allowed only with `--agent`
- `--json`

Rules:
- exactly one of `--human` or `--agent` is required,
- `--skill` appears at most once,
- adding an approval to a missing task is invalid,
- adding an approval to a task in `done` or `archived` is invalid,
- adding an approval must not itself mutate task status in this phase,
- duplicate human approvals are forbidden,
- duplicate agent approvals for the same `(profile, skill)` pair are forbidden.

Rationale:
- a task already in `approval` keeps its current task status,
- a task still in pre-completion execution (`todo`, `ready`, `running`, `blocked`, `scheduled`) simply gains a future gate that will matter on completion.

### 4.2 `hermes kanban approval list`

List approval rows.

Supported forms:

```bash
hermes kanban approval list
hermes kanban approval list --task <task_id>
hermes kanban approval list --status <requested|running|approved|rejected|escalated|failed>
hermes kanban approval list --type <human|agent>
hermes kanban approval list --json
```

Rules:
- `--task` restricts to one task,
- `--status` and `--type` may be combined with or without `--task`,
- default output is human-readable and compact,
- `--json` returns structured rows suitable for scripting.

### 4.3 `hermes kanban approval remove`

Remove one approval row.

```bash
hermes kanban approval remove <approval_id>
```

Optional flags:
- `--json`

Rules:
- removing a missing approval is invalid,
- removal must be transactional,
- removal must recompute parent task state when the parent task is currently `approval`,
- if removing the last approval row leaves an `approval` task with no live approvals, the task moves to `done`,
- Phase 3 does **not** implement the future special-case human-gate cleanup for `escalated` / `failed` dependency graphs because that flow depends on Phase 4 escalation wiring not yet being live.

Phase 3 removal semantics therefore operate only on the approval row being removed plus aggregate recomputation from the remaining rows.

### 4.4 `hermes kanban approval approve`

Record a manual human approval decision.

```bash
hermes kanban approval approve <task_id> [--comment "..."]
```

Optional flags:
- `--json`

Rules:
- valid only when the task has exactly one human approval row,
- valid only when the parent task is currently `approval`,
- the CLI resolves the single human approval row for the task internally rather than requiring the operator to pass `approval_id`,
- manual human decisions do not add a separate requested-only precondition; while a valid human approval row remains attached to a parent task in `approval`, the decision is allowed through the shared kernel path,
- `--comment` appends a task comment and stores the resulting `comment_id`,
- the command must use the same kernel decision path as other approval decisions rather than directly updating the row in CLI code,
- task state is recomputed transactionally.

Implementation note:
- the CLI may need a small manual-decision DB helper parallel to `record_task_approval_decision(...)` because human decisions do not come from a live agent approval run and therefore do not have `expected_run_id`.
- if such a helper is added, it must still route through the same aggregate transition logic and must not duplicate the resolver semantics.

### 4.5 `hermes kanban approval reject`

Record a manual human rejection decision.

```bash
hermes kanban approval reject <task_id> [--comment "..."]
```

Optional flags:
- `--json`

Rules:
- valid only when the task has exactly one human approval row,
- valid only when the parent task is currently `approval`,
- the CLI resolves the single human approval row for the task internally rather than requiring the operator to pass `approval_id`,
- manual human decisions do not add a separate requested-only precondition; while a valid human approval row remains attached to a parent task in `approval`, the decision is allowed through the shared kernel path,
- `--comment` appends a task comment and stores the resulting `comment_id`,
- the command must trigger the same authoritative rejection-cycle semantics already implemented in Phase 2:
  - rejection recorded first,
  - task returns from `approval` to `todo`,
  - attached approval rows reset to `requested` in the same transaction.

### 4.6 `hermes kanban approval reset`

Reset one approval row back to `requested`.

```bash
hermes kanban approval reset <approval_id>
```

Optional flags:
- `--json`

Rules:
- valid only when the parent task is currently `approval`,
- valid for any existing approval row state,
- resetting an already-`requested` row is an allowed no-op,
- reuses the first-class kernel reset operation,
- does not recompute task status; with the task still in `approval` and the row reset to `requested`, the aggregate necessarily remains `approval`.
- resetting a `running` row does not require eagerly terminating the already-spawned agent process; any later result from that old run is stale/discarded because the row no longer remains in the owned `running` state for that run.

In this phase, reset is an explicit operator tool. It is not a substitute for dispatcher reclaim logic.

---

## 5) Existing command changes

### 5.1 `hermes kanban show <task_id>`

`hermes kanban show` must become approval-aware.

Human-readable output must include:
- attached approval rows,
- approval status,
- approver type,
- approver profile / skill when present,
- approval comment reference when present.

`--json` output must include:
- `approvals: [...]`

This is the required inspection surface for Phase 3. Approval-run rows remain runtime bookkeeping and are not exposed in normal Phase 3 command responses.

### 5.2 `hermes kanban create`

No approval flags are added to task creation in this phase.

Approvals remain an explicit follow-up operation via `hermes kanban approval add`.

### 5.3 Slash command behavior

`/kanban ...` should inherit the new approval namespace automatically through the shared argparse tree in `hermes_cli/kanban.py`.

This phase does not add separate slash-only semantics.

---

## 6) Required DB behavior for Phase 3 manual decisions

### 6.1 Human decisions are not synthetic agent runs

Manual human CLI decisions must not fabricate `task_approval_runs` rows.

The distinction is:
- agent approval decisions close an existing approval-run row,
- human approval decisions mutate the approval row directly and then recompute task state.

### 6.2 Manual decision helper requirements

If Phase 3 adds a dedicated helper for human decisions, it must:

1. resolve the single human approval row for the task,
2. validate that it is a human row,
3. validate only that the parent task is currently `approval`,
4. optionally append a task comment and capture `comment_id`,
5. update the approval row status transactionally,
6. clear live mutable row fields exactly when the reset/decision semantics require it,
7. recompute the parent task state through the same aggregate transition helpers used elsewhere,
8. emit the same approval audit events as the agent decision path where the semantics match.

The helper must not require an approval run id.

### 6.3 Approval removal helper requirements

If Phase 3 adds `remove_task_approval(...)`, it must:

1. remove exactly one approval row,
2. reject unknown approval ids,
3. eagerly remove the approval row even when a worker currently owns it,
4. recompute the parent task state transactionally when needed,
5. preserve task comments and event history,
6. silently discard late worker results when the approval row and/or approval-run row no longer exists,
7. leave future dispatcher-specific reclaim/cancel behavior out of scope.

---

## 7) Output and UX expectations

### 7.1 Human-readable CLI output

The default CLI output should stay concise and task-centric.

For approval list rows, include at least:
- approval id,
- task id,
- status,
- type,
- profile / skill when applicable.

For manual decision / add / remove / reset commands, print a one-line success result plus the key resulting state, for example:
- approval id,
- new approval status,
- parent task id,
- parent task status when it changed.

### 7.2 JSON output

Every new approval command in this phase must support `--json`.

JSON should return structured fields, not formatted text blobs.

At minimum:
- row identity,
- task id,
- status,
- type,
- profile / skill,
- comment id when relevant,
- parent task status when the command can change it.

---

## 8) Concrete files in scope

### Primary implementation files
- `hermes_cli/kanban.py`
- `hermes_cli/kanban_db.py`

### Likely parser/dispatch touchpoints
- `hermes_cli/kanban.py:build_parser(...)`
- `hermes_cli/kanban.py:kanban_command(...)`
- existing task-show formatting and JSON assembly paths in `hermes_cli/kanban.py`

### Tests to extend
- `tests/hermes_cli/test_kanban_cli.py`
- `tests/hermes_cli/test_kanban_approvals_db.py`
- optionally `tests/hermes_cli/test_kanban_db.py` when task-state consequences are easiest to assert there

If the CLI cases become dense, a new dedicated file is acceptable:
- `tests/hermes_cli/test_kanban_approval_cli.py`

---

## 9) Acceptance criteria

Phase 3 is complete only if all of the following hold:

1. A human can add a human approval row from the CLI.
2. A human can add an agent approval row from the CLI.
3. Duplicate approval-gate invariants are enforced through the CLI with clear errors.
4. `hermes kanban show <task_id>` exposes attached approvals in both text and JSON output.
5. `hermes kanban approval list` can filter by task, status, and type.
6. A human can approve a task-centric human approval from the CLI and the parent task reaches `done` when the aggregate becomes satisfied.
7. A human can reject a task-centric human approval from the CLI and the parent task returns to `todo` with approval rows reset to `requested` in the same transaction.
8. Removing the last approval row from an `approval` task moves the task to `done`.
9. Resetting a non-running approval row through the CLI is allowed only while the parent task is `approval`, returns the row to `requested`, and leaves the parent task in `approval` without needing a separate recompute step.
10. Manual CLI decisions do not fabricate approval-run rows.
11. CLI code does not duplicate aggregate-lifecycle logic already owned by `kanban_db.py`.
12. Boards with no approval usage keep their existing behavior.

---

## 10) Deferred to later phases

The following remain intentionally deferred after Phase 3:

- dispatcher approval scanning and spawn budget,
- claim-before-spawn for approval rows,
- approval worker heartbeats and reclaim,
- agent decision prompt/output contract wiring,
- invalid-output failure handling from real approval subprocesses,
- automatic human escalation creation from `escalated` / `failed` agent decisions,
- special removal semantics for human gates that are actively satisfying escalated/failed agent rows,
- dashboard approval UI.

This keeps Phase 3 tight: manual CLI workflow first, autonomous execution later.
