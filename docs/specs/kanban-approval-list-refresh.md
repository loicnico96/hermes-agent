# Kanban approval list / ls refresh

## Goal

Replace the current `hermes kanban approval list` output and filtering model with a task-aware operator view that is useful for everyday approval triage, while keeping one explicit row-oriented escape hatch via `--flat`.

This spec covers only the approval listing surface.
It does not change approval state transitions, approval aggregation rules, approval request semantics, or the dispatcher/worker runtime.

## Scope

In scope:
- replace the current `approval list` behavior
- add `approval ls` as an alias with identical behavior
- default to hiding parent tasks in `done` / `archived`
- add `--all` and `--active` parent-task filters
- make grouped-by-task text output the default
- add `--flat` one-row-per-approval output
- support `--json` in both grouped and flat modes
- replace `--type` with `--agent` / `--human`
- support an optional positional `task_id` selector and preserve `--status`
- define exact ordering, formatting, padding, and JSON shapes
- update CLI/user-guide docs for the new surface

Out of scope:
- any approval runtime behavior change (`approve`, `reject`, `reset`, `reclaim`, dispatcher behavior, state machine)
- new approval statuses
- new approval commands beyond `list`/`ls`
- dashboard/web server changes
- changing `show` output
- changing the task-level kanban list ordering logic

## Current grounding

Current relevant code:
- `hermes_cli/kanban_approvals.py`
  - `_cmd_approval_list(...)`
  - `_format_approval_line(...)`
  - `register_approval_subparser(...)`
- `hermes_cli/kanban_approvals_db.py`
  - `list_approvals(...)`
  - `list_task_approvals(...)`
  - `Approval` dataclass
- `hermes_cli/kanban_db.py`
  - `Task` dataclass
  - `get_task(...)`
  - task priority / created-at semantics used by main kanban list
- `tests/hermes_cli/test_kanban_approvals_cli.py`
- docs:
  - `website/docs/user-guide/features/kanban-approvals.md`
  - `website/docs/reference/cli-commands.md`

Important current truths:
- parent task data needed for the new view already exists in `tasks`
- `Task` has `id`, `title`, `assignee`, `status`, `priority`, `created_at`
- task rows do not have a general `updated_at`
- approval rows do have `created_at` / `updated_at`, but grouped ordering for this slice must follow task ordering semantics, not approval-recency semantics
- normal `hermes kanban list` defaults to task ordering `priority DESC, created_at ASC`

## CLI contract

### Commands

The following command forms must be supported:

```bash
hermes kanban approval list
hermes kanban approval ls
```

`ls` is an alias of `list`. Both must execute the exact same implementation path and produce identical behavior.

### Flags

`approval list` / `approval ls` must support:
- `--all`
- `--active`
- `--flat`
- `--json`
- `[task_id]` (optional positional)
- `--status <approval_status>`
- `--agent`
- `--human`

### Mutual exclusivity

Parser-level mutual exclusivity must be enforced for:
- `--all` vs `--active`
- `--agent` vs `--human`

Usage examples that must fail with a normal CLI usage error:

```bash
hermes kanban approval list --all --active
hermes kanban approval list --agent --human
```

### Flag semantics

#### Default mode
With no flags:
- parent tasks in `done` and `archived` are excluded
- output is grouped by task
- task groups are ordered by `priority DESC, created_at ASC`
- approvals within a task are ordered by `approval_id ASC`

#### `--all`
- include parent tasks in `done` and `archived`
- otherwise keep default grouped behavior unless another mode flag changes it

#### `--active`
- restrict parent tasks to `task.status == "approval"`
- otherwise keep default grouped behavior unless another mode flag changes it

#### `--flat`
- switch text output from grouped-by-task to one-row-per-approval
- flat ordering is `approval_id ASC`

#### `--json`
- return JSON instead of text
- grouped JSON is the default JSON shape
- `--flat --json` returns flat JSON rows instead

#### positional `task_id`
- restrict listing to one specific task id
- implies `--flat`
- includes that task even if its status is `done` or `archived`
- still combines with `--status`, `--agent`, `--human`, and `--json`
- must fail if the task does not exist

The positional task-id selector does **not** require or imply `--all`.
The task-specific request bypasses the default done/archived exclusion by definition.

#### `--status <approval_status>`
- filter approval rows by approval-row status
- works with grouped, flat, `--all`, `--active`, positional `task_id`, `--agent`, `--human`, and `--json`
- in grouped mode, tasks with zero remaining matching approval rows after filtering are omitted entirely

#### `--agent`
- filter to `approver_type == "agent"`

#### `--human`
- filter to `approver_type == "human"`

## Query / data contract

## Listing model
The listing implementation should stop treating `approval list` as a pure approval-row dump.
It must operate on a joined task + approval view.

The implementation may either:
- add a dedicated helper in `hermes_cli/kanban_approvals_db.py` that joins `task_approvals` to `tasks`, or
- fetch approvals first and then materialize tasks in Python

For this slice, a dedicated joined helper is preferred because:
- parent-task filtering (`done` / `archived`, `approval`) belongs naturally in the query
- ordering and JSON shaping become simpler
- it avoids N+1 `get_task(...)` lookups in common list calls

### Required joined row fields
The listing implementation must have access to at least:
- approval:
  - `id`
  - `task_id`
  - `approver_type`
  - `approver_profile`
  - `approver_skill`
  - `status`
  - `comment_id`
  - `worker_pid`
  - `current_run_id`
  - `created_at`
- task:
  - `id`
  - `title`
  - `assignee`
  - `status`
  - `priority`
  - `created_at`

A small dedicated row dataclass or shaped dict is acceptable.
This slice does not require exposing every task/approval column in text output.

## Ordering contract

### Flat ordering
Flat mode must be ordered by:
1. `approval.id ASC`

No hidden task grouping or task-primary ordering is allowed in flat mode.
Rows from different tasks may interleave naturally.

### Grouped ordering
Grouped mode must order task groups by the same task-priority semantics used by normal kanban list:
1. `task.priority DESC`
2. `task.created_at ASC`
3. `task.id ASC` as a stable tie-breaker if needed

Approvals inside each task group must be ordered by:
1. `approval.id ASC`

If the implementation performs grouping in Python after a joined query, the underlying joined query must still preserve enough ordering to make group assembly deterministic.

## Rendering contract

### Approval target rendering
Use one canonical formatter for approval targets in list output.

Render targets exactly as:
- human approval: `human`
- agent approval with no skill: `agent @<profile>`
- agent approval with skill: `agent @<profile>:<skill>`

Examples:
- `human`
- `agent @coder`
- `agent @default:initiative-kanban-approver`

Do not use the old `skill=...` suffix form in the new list output.

### Optional badges on approval rows
Append optional suffix badges in this order when present:
1. `[run #<current_run_id>]`
2. `[pid: <worker_pid>]`
3. `[comment #<comment_id>]`

Examples:
- `agent @default:initiative-kanban-approver [run #5656] [pid: 819982]`
- `agent @coder [comment #402]`

Badges are appended only when the underlying field exists.
They do not reserve width.

### Fixed-width padding rules
These widths are minimum text widths used for left-aligned padding in text output.
Longer values are not truncated by this slice; they may overflow their minimum field width.

Required minimum widths:
- task id: `12`
- task status: `12`
- task assignee: `26`
- approval id (flat): `8`
- approval id (grouped): `12`
- approval status: `12`

Interpretation:
- `task id: 12` means `t_123456789` plus two spaces in the common case
- `task assignee: 26` means values such as `human` or `agent @default` are padded to width 26 before the title column starts
- grouped approval id width `12` includes the leading two-space indent and the padded `#<id>` field

Use `ljust(width)`-style semantics; do not right-align numeric ids.

## Text output shapes

### Flat text output
Format each row as:

```text
<approval_id_8><task_id_12><approval_status_12><approval_target_and_badges>
```

Canonical examples:

```text
#58     t_123456789  requested   human
#191    t_123456789  running     agent @default:initiative-kanban-approver [run #5656] [pid: 819982]
#197    t_123456790  approved    agent @coder [comment #402]
```

Flat mode must not print the task title.
Flat mode is intentionally dense and row-oriented.

### Grouped text output
Format each task header as:

```text
<task_id_12><task_status_12><task_assignee_26><task_title>
```

Format each approval row under it as:

```text
  <approval_id_padded_to_grouped_width><approval_status_12><approval_target_and_badges>
```

Canonical examples:

```text
t_123456789  approval    agent @coder              Fix gateway topic routing
  #58        requested   human
  #191       running     agent @default:initiative-kanban-approver [run #5656] [pid: 819982]
t_123456790  done        agent @default            Open new PR
  #197       approved    agent @coder [comment #402]
```

Rules:
- one header line per task
- header line appears only if at least one approval row remains after all filters
- if task assignee is null, render `-` and pad it to width 26
- no blank line between groups in v1

## JSON contract

### Grouped JSON shape (default with `--json`)
Return a JSON array of task groups:

```json
[
  {
    "task": {
      "id": "t_123456789",
      "status": "approval",
      "assignee": "coder",
      "title": "Fix gateway topic routing",
      "priority": 100,
      "created_at": 1710000000
    },
    "approvals": [
      {
        "id": 58,
        "task_id": "t_123456789",
        "status": "requested",
        "approver_type": "human",
        "approver_profile": null,
        "approver_skill": null,
        "comment_id": null,
        "current_run_id": null,
        "worker_pid": null,
        "created_at": 1710000001
      }
    ]
  }
]
```

Grouped JSON requirements:
- task object fields must reflect the real task, not a rendered string
- approvals array order must match grouped text order (`approval.id ASC`)
- groups must appear in grouped ordering (`priority DESC, created_at ASC`)

### Flat JSON shape (`--flat --json`, and implied by positional `task_id` + `--json`)
Return a JSON array of flat joined rows:

```json
[
  {
    "approval_id": 58,
    "task_id": "t_123456789",
    "approval_status": "requested",
    "approver_type": "human",
    "approver_profile": null,
    "approver_skill": null,
    "comment_id": null,
    "current_run_id": null,
    "worker_pid": null,
    "task_status": "approval",
    "task_assignee": "coder",
    "task_title": "Fix gateway topic routing",
    "task_priority": 100,
    "task_created_at": 1710000000,
    "approval_created_at": 1710000001
  }
]
```

Flat JSON requirements:
- row order must match flat text order (`approval.id ASC`)
- the shape must be explicitly flat and not a grouped object with one task

## Parser / command wiring changes

## `register_approval_subparser(...)`
Required changes:
- change `approval list` parser to add alias `ls`
- remove `--type`
- add parser-level mutually exclusive group for `--all` / `--active`
- add parser-level mutually exclusive group for `--agent` / `--human`
- add `--flat`
- keep `--json`
- replace `--task` with the optional positional `task_id`
- keep `--status`

Suggested parser shape:
- `--task` remains an optional argument rather than a subcommand split
- `--task` implication of `--flat` is applied in handler logic, not through duplicate parsers

## Implementation changes by file

### `hermes_cli/kanban_approvals_db.py`
Add a dedicated listing helper for the new surface.

Suggested new helper:
- `list_approval_rows_for_cli(...)`

Suggested inputs:
- `task_id: Optional[str] = None`
- `approval_status: Optional[str] = None`
- `approver_type: Optional[str] = None`
- `include_terminal_tasks: bool = False`
- `active_only: bool = False`
- `flat: bool = False`

Behavior:
- validate incompatible combinations only if the CLI layer did not already prevent them
- when `task_id` is provided:
  - verify the task exists
  - bypass done/archived exclusion
  - ignore `include_terminal_tasks` / `active_only` for parent-status gating
- otherwise:
  - default: exclude `done`, `archived`
  - `include_terminal_tasks=True`: include all parent statuses
  - `active_only=True`: require `tasks.status = 'approval'`
- apply approval-row filters (`status`, `approver_type`)
- order rows for grouped or flat assembly as required

The helper may return flat joined rows in all cases; grouping can happen in `kanban_approvals.py`.

### `hermes_cli/kanban_approvals.py`
Required changes:
- replace `_cmd_approval_list(...)` implementation
- add canonical render helpers for:
  - approval target string
  - task assignee header string
  - flat row formatting
  - grouped header formatting
  - grouped approval row formatting
- keep JSON shaping local here unless a stronger DB-layer reason appears
- ensure `--task` implies flat mode in one local place

Suggested helpers:
- `_format_task_assignee(task: kb.Task) -> str`
- `_format_approval_target_for_list(row) -> str`
- `_format_flat_approval_row(row) -> str`
- `_format_group_header(task) -> str`
- `_format_group_approval_row(row) -> str`
- `_group_approval_rows(rows) -> list[tuple[task_payload, list[row_payload]]]`

Do not mutate the existing `approve` / `reject` / `reset` / `reclaim` flows in this slice.

### `tests/hermes_cli/test_kanban_approvals_cli.py`
Add focused CLI coverage for:
- `list` and `ls` alias equivalence
- default exclusion of done/archived parent tasks
- `--all` includes terminal tasks
- `--active` restricts to parent task status `approval`
- `--all --active` usage error
- `--agent --human` usage error
- `--task` implies flat mode
- `--task` includes done/archived task results
- `--status` filtering in grouped mode omits empty groups
- flat ordering is `approval_id ASC`
- grouped ordering is `task.priority DESC, task.created_at ASC`, approvals inside group `approval_id ASC`
- text formatting exactness for:
  - flat examples
  - grouped examples
  - optional badges
  - null assignee rendering as `-`
- grouped JSON shape
- flat JSON shape
- `--task t_missing` failure

### Docs
Update:
- `website/docs/user-guide/features/kanban-approvals.md`
- `website/docs/reference/cli-commands.md`

Required doc updates:
- `approval approve/reject` now accept approval id or task id (landed behavior)
- `approval list` / `approval ls` new default grouped/task-aware behavior
- new flags: `--all`, `--active`, `--flat`, `--agent`, `--human`
- `--type` removal
- examples for grouped and flat output

## Algorithm sketch

### Handler pseudocode

```python
flat = bool(args.flat or args.task)
approver_type = "agent" if args.agent else "human" if args.human else None
rows = approvals_db.list_approval_rows_for_cli(
    conn,
    task_id=args.task,
    approval_status=args.status,
    approver_type=approver_type,
    include_terminal_tasks=args.all,
    active_only=args.active,
    flat=flat,
)

if args.json:
    if flat:
        print(flat_json(rows))
    else:
        print(grouped_json(rows))
    return 0

if not rows:
    print("(no matching approvals)")
    return 0

if flat:
    for row in rows:
        print(format_flat(row))
    return 0

for task, approvals in group_rows(rows):
    print(format_group_header(task))
    for row in approvals:
        print(format_group_approval_row(row))
```

### Grouping algorithm

```python
groups = OrderedDict()
for row in rows:
    groups.setdefault(row.task_id, {"task": task_payload(row), "approvals": []})
    groups[row.task_id]["approvals"].append(row)
return list(groups.values())
```

This assumes the input rows already arrive in grouped task order and per-task approval order.

## Acceptance criteria

This slice is correct only when all of the following are true:
- `approval list` and `approval ls` behave identically
- default grouped output hides done/archived parent tasks
- `--all` includes them
- `--active` restricts to parent tasks in `approval`
- `--all` and `--active` cannot be used together
- `--agent` and `--human` cannot be used together
- `--task` implies flat output and includes terminal tasks
- flat mode is ordered only by `approval_id ASC`
- grouped mode is ordered by `task.priority DESC, task.created_at ASC`, with approvals inside group by `approval_id ASC`
- text output matches the fixed padding contract
- grouped and flat JSON shapes both work and are distinct
- docs no longer mention `--type` for approval list
- docs show current approve/reject task-id-or-approval-id behavior

## Suggested execution plan

1. Add the dedicated joined listing helper in `hermes_cli/kanban_approvals_db.py`.
2. Rework parser flags in `register_approval_subparser(...)`.
3. Replace `_cmd_approval_list(...)` and add the new render/group helpers.
4. Add focused CLI tests for filtering, ordering, grouped vs flat, and exact formatting.
5. Update approval user-guide and CLI reference docs.
6. Run the focused CLI approval test file and any directly affected docs/tests.

## Validation

Minimum validation for the worker:
- targeted CLI suite:
  - `uv run --extra dev python -m pytest tests/hermes_cli/test_kanban_approvals_cli.py -q`
- if a new DB helper test is added, also run the targeted DB suite:
  - `uv run --extra dev python -m pytest tests/hermes_cli/test_kanban_approvals_db.py -q`

The worker should not stop after parser changes or partial formatting updates; the deliverable is the full shipped surface described here.
