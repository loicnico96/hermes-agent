"""Approval-specific CLI helpers for ``hermes kanban approval ...``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any

from hermes_cli import kanban_approvals_db as approvals_db
from hermes_cli import kanban_db as kb


def approval_run_to_dict(run: approvals_db.ApprovalRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "approval_id": run.approval_id,
        "task_id": run.task_id,
        "profile": run.profile,
        "status": run.status,
        "claim_lock": run.claim_lock,
        "claim_expires": run.claim_expires,
        "worker_pid": run.worker_pid,
        "last_heartbeat_at": run.last_heartbeat_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "outcome": run.outcome,
        "comment_id": run.comment_id,
        "error": run.error,
    }


def _profile_author() -> str:
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = os.environ.get(env)
        if value:
            return value
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "user"
    except Exception:
        return "user"


def approval_to_dict(approval: approvals_db.Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "task_id": approval.task_id,
        "approver_type": approval.approver_type,
        "approver_profile": approval.approver_profile,
        "approver_skill": approval.approver_skill,
        "status": approval.status,
        "comment_id": approval.comment_id,
        "claim_lock": approval.claim_lock,
        "claim_expires": approval.claim_expires,
        "worker_pid": approval.worker_pid,
        "last_heartbeat_at": approval.last_heartbeat_at,
        "current_run_id": approval.current_run_id,
        "consecutive_failures": approval.consecutive_failures,
        "last_failure_error": approval.last_failure_error,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
    }


def format_approval_line(approval: approvals_db.Approval, *, include_task_id: bool) -> str:
    if include_task_id:
        return _format_flat_approval_line(approval)
    return _format_grouped_approval_line(approval)


def format_approval_run_line(run: approvals_db.ApprovalRun) -> str:
    elapsed = (max(0, run.ended_at - run.started_at) if run.ended_at is not None else None)
    elapsed_text = f"{elapsed}s" if elapsed is not None else "active"
    outcome = run.outcome or run.status or "active"
    bits = [
        f"#{run.id}",
        f"approval=#{run.approval_id}",
        f"{outcome:12s}",
        f"@{run.profile or '-'}",
        elapsed_text,
    ]
    return "  " + "  ".join(bits)


def _approval_mutation_payload(conn: Any, approval: approvals_db.Approval) -> dict[str, Any]:
    task = kb.get_task(conn, approval.task_id)
    assert task is not None
    return {
        "approval": approval_to_dict(approval),
        "task_status": task.status,
    }


def _approval_task_to_dict(task: kb.Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "assignee": task.assignee,
        "created_at": task.created_at,
    }


def _list_task_assignee(task: kb.Task) -> str:
    if task.assignee is None:
        return "-"
    if task.assignee == "human":
        return "human"
    return f"agent @{task.assignee}"


def _format_list_approval_target(approval: approvals_db.Approval) -> str:
    if approval.approver_type == "human":
        return "human"
    target = f"agent @{approval.approver_profile}"
    if approval.approver_skill:
        target += f":{approval.approver_skill}"
    return target


def _format_list_approval_badges(approval: approvals_db.Approval) -> str:
    badges: list[str] = []
    if approval.current_run_id is not None:
        badges.append(f"[run #{approval.current_run_id}]")
    if approval.worker_pid is not None:
        badges.append(f"[pid: {approval.worker_pid}]")
    if approval.comment_id is not None:
        badges.append(f"[comment #{approval.comment_id}]")
    return f" {' '.join(badges)}" if badges else ""


def _truncate_single_line(text: str, *, limit: int = 160) -> str:
    collapsed = " ".join((text or "").split())
    effective_limit = max(0, min(limit, 2000))
    if len(collapsed) <= effective_limit:
        return collapsed
    return collapsed[: max(0, effective_limit - 1)].rstrip() + "…"


def _load_task_comment_bodies(
    conn: Any,
    *,
    task_id: str,
    comment_ids: list[int],
) -> dict[int, str]:
    if not comment_ids:
        return {}
    placeholders = ", ".join("?" for _ in comment_ids)
    rows = conn.execute(
        f"SELECT id, body FROM task_comments WHERE task_id = ? AND id IN ({placeholders})",
        [task_id, *comment_ids],
    ).fetchall()
    return {int(row["id"]): str(row["body"] or "") for row in rows}


def _list_approval_task_ids(approvals: list[approvals_db.Approval]) -> list[str]:
    return list(dict.fromkeys(approval.task_id for approval in approvals))


def _load_approval_tasks(conn: Any, approvals: list[approvals_db.Approval]) -> dict[str, kb.Task]:
    tasks: dict[str, kb.Task] = {}
    for task_id in _list_approval_task_ids(approvals):
        task = kb.get_task(conn, task_id)
        if task is not None:
            tasks[task_id] = task
    return tasks


def _matches_parent_task_filter(task: kb.Task, *, include_all: bool, active_only: bool) -> bool:
    if include_all:
        return True
    if active_only:
        return task.status == "approval"
    return task.status not in {"done", "archived"}


def _filter_approvals_by_parent_task(
    approvals: list[approvals_db.Approval],
    *,
    tasks_by_id: dict[str, kb.Task],
    include_all: bool,
    active_only: bool,
) -> list[approvals_db.Approval]:
    return [
        approval
        for approval in approvals
        if (task := tasks_by_id.get(approval.task_id)) is not None
        and _matches_parent_task_filter(task, include_all=include_all, active_only=active_only)
    ]


def _group_approvals_by_task(
    approvals: list[approvals_db.Approval],
    *,
    tasks_by_id: dict[str, kb.Task],
) -> list[tuple[kb.Task, list[approvals_db.Approval]]]:
    grouped: dict[str, list[approvals_db.Approval]] = defaultdict(list)
    for approval in sorted(approvals, key=lambda row: row.id):
        grouped[approval.task_id].append(approval)

    tasks = [tasks_by_id[task_id] for task_id in grouped]
    tasks.sort(key=lambda task: (-task.priority, task.created_at, task.id))
    return [(task, grouped[task.id]) for task in tasks]


def _format_grouped_task_header(task: kb.Task) -> str:
    return (
        f"{task.id.ljust(12)}"
        f"{task.status.ljust(12)}"
        f"{_list_task_assignee(task).ljust(26)}"
        f"{task.title}"
    )


def _format_flat_approval_line(approval: approvals_db.Approval) -> str:
    return (
        f"#{approval.id}".ljust(8)
        + f"{approval.task_id.ljust(12)}"
        + f"{approval.status.ljust(12)}"
        + f"{_format_list_approval_target(approval)}"
        + f"{_format_list_approval_badges(approval)}"
    )


def _format_grouped_approval_line(approval: approvals_db.Approval) -> str:
    return (
        f"  #{approval.id}".ljust(12)
        + f"{approval.status.ljust(12)}"
        + f"{_format_list_approval_target(approval)}"
        + f"{_format_list_approval_badges(approval)}"
    )


def _approval_group_to_dict(task: kb.Task, approvals: list[approvals_db.Approval]) -> dict[str, Any]:
    return {
        "task": _approval_task_to_dict(task),
        "approvals": [approval_to_dict(approval) for approval in approvals],
    }


def _flat_approval_row_to_dict(approval: approvals_db.Approval, task: kb.Task) -> dict[str, Any]:
    return {
        "approval_id": approval.id,
        "task_id": approval.task_id,
        "approval_status": approval.status,
        "approver_type": approval.approver_type,
        "approver_profile": approval.approver_profile,
        "approver_skill": approval.approver_skill,
        "comment_id": approval.comment_id,
        "current_run_id": approval.current_run_id,
        "worker_pid": approval.worker_pid,
        "task_status": task.status,
        "task_assignee": task.assignee,
        "task_title": task.title,
        "task_priority": task.priority,
        "task_created_at": task.created_at,
        "approval_created_at": approval.created_at,
    }


def _approval_run_identity(run: approvals_db.ApprovalRun) -> str:
    return f"@{run.profile or '-'}"


def _format_approval_run_badges(run: approvals_db.ApprovalRun) -> str:
    badges: list[str] = []
    if run.worker_pid is not None:
        badges.append(f"[pid: {run.worker_pid}]")
    if run.comment_id is not None:
        badges.append(f"[comment #{run.comment_id}]")
    return f" {' '.join(badges)}" if badges else ""


def _format_approval_run_line(run: approvals_db.ApprovalRun) -> str:
    elapsed = max(0, (run.ended_at or int(time.time())) - run.started_at)
    outcome = run.outcome or run.status or "active"
    return (
        f"#{run.id}".ljust(8)
        + f"{outcome.ljust(12)}"
        + f"{f'{elapsed}s'.ljust(8)}"
        + f"{_approval_run_identity(run)}"
        + f"{_format_approval_run_badges(run)}"
    )


def _approval_run_to_cli_dict(
    run: approvals_db.ApprovalRun,
    *,
    comment_body: str | None,
) -> dict[str, Any]:
    elapsed = max(0, (run.ended_at or int(time.time())) - run.started_at)
    return {
        **approval_run_to_dict(run),
        "display_status": run.outcome or run.status or "active",
        "elapsed_seconds": elapsed,
        "assignee": run.profile,
        "comment_body": comment_body,
        "comment_preview": _truncate_single_line(comment_body, limit=2000) if comment_body else None,
        "error_preview": _truncate_single_line(run.error, limit=2000) if run.error else None,
    }


def _cmd_approval_request(args: argparse.Namespace) -> int:
    approver_type = "human" if getattr(args, "human", False) else "agent"
    if approver_type == "human" and getattr(args, "skill", None):
        raise ValueError("--skill is only valid with --agent")

    with kb.connect() as conn:
        approval = approvals_db.create_task_approval(
            conn,
            task_id=args.task_id,
            approver_type=approver_type,
            approver_profile=getattr(args, "agent", None),
            approver_skill=getattr(args, "skill", None),
        )

    if getattr(args, "json", False):
        print(json.dumps(approval_to_dict(approval), indent=2, ensure_ascii=False))
    else:
        print(
            f"Requested approval #{approval.id} on {approval.task_id} "
            f"({approval.status}, {_format_list_approval_target(approval)})"
        )
    return 0


def _cmd_approval_list(args: argparse.Namespace) -> int:
    approver_type = None
    if getattr(args, "human", False):
        approver_type = "human"
    elif getattr(args, "agent", False):
        approver_type = "agent"

    with kb.connect() as conn:
        task_id = getattr(args, "task_id", None)
        task_filter = kb.get_task(conn, task_id) if task_id is not None else None
        if task_id is not None and task_filter is None:
            print(f"no such task: {task_id}", file=sys.stderr)
            return 1
        approvals = approvals_db.list_approvals(
            conn,
            task_id=task_id,
            status=getattr(args, "status", None),
            approver_type=approver_type,
        )
        tasks_by_id = _load_approval_tasks(conn, approvals)

    include_all = bool(getattr(args, "all", False)) or task_id is not None
    active_only = bool(getattr(args, "active", False))
    approvals = _filter_approvals_by_parent_task(
        approvals,
        tasks_by_id=tasks_by_id,
        include_all=include_all,
        active_only=active_only,
    )

    task_scoped = task_id is not None
    flat_text_view = bool(getattr(args, "flat", False))
    flat_json_view = flat_text_view or task_scoped
    if flat_text_view or flat_json_view:
        approvals.sort(key=lambda row: row.id)

    if getattr(args, "json", False):
        if flat_json_view:
            payload: Any = [
                _flat_approval_row_to_dict(approval, tasks_by_id[approval.task_id])
                for approval in approvals
                if approval.task_id in tasks_by_id
            ]
        else:
            payload = [
                _approval_group_to_dict(task, task_approvals)
                for task, task_approvals in _group_approvals_by_task(approvals, tasks_by_id=tasks_by_id)
            ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not approvals:
        print("(no matching approvals)")
        return 0

    if flat_text_view:
        for approval in approvals:
            print(_format_flat_approval_line(approval))
        return 0

    grouped = _group_approvals_by_task(approvals, tasks_by_id=tasks_by_id)
    for task, task_approvals in grouped:
        if not task_scoped:
            print(_format_grouped_task_header(task))
        for approval in task_approvals:
            print(_format_grouped_approval_line(approval))
    return 0


def _cmd_approval_runs(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        approval = approvals_db.get_task_approval(conn, args.approval_id)
        if approval is None:
            print(f"unknown approval {args.approval_id}", file=sys.stderr)
            return 1
        runs = approvals_db.list_approval_runs(conn, approval_id=args.approval_id)
        comment_ids = [run.comment_id for run in runs if run.comment_id is not None]
        comment_bodies = _load_task_comment_bodies(
            conn,
            task_id=approval.task_id,
            comment_ids=[int(comment_id) for comment_id in comment_ids],
        )

    if getattr(args, "json", False):
        payload = [
            _approval_run_to_cli_dict(run, comment_body=comment_bodies.get(run.comment_id, None))
            for run in runs
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not runs:
        print("(no approval runs)")
        return 0

    for run in runs:
        print(_format_approval_run_line(run))
        if run.error:
            print(f"    ! {_truncate_single_line(run.error)}")
        if run.comment_id is not None and (comment_body := comment_bodies.get(run.comment_id)):
            print(f"    → {_truncate_single_line(comment_body)}")
    return 0


def _cmd_approval_remove(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        approval = approvals_db.remove_task_approval(conn, args.approval_id)
        payload = _approval_mutation_payload(conn, approval)

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Removed approval #{approval.id} from {approval.task_id}; "
            f"task status is now {payload['task_status']}"
        )
    return 0


def _resolve_manual_approval_target(conn: Any, target: str) -> int:
    normalized_target = target.strip()
    if not normalized_target:
        raise ValueError("approval target is required")

    if normalized_target.startswith("t_"):
        task = kb.get_task(conn, normalized_target)
        if task is None:
            raise ValueError(f"unknown task {normalized_target}")
        if task.status != "approval":
            raise ValueError("parent task must be in approval status")

        human_approvals = approvals_db.list_task_approvals(
            conn,
            normalized_target,
            approver_type="human",
        )
        if len(human_approvals) > 1:
            raise ValueError(f"task {normalized_target} has multiple human approvals")
        if human_approvals:
            return human_approvals[0].id

        created = approvals_db.create_task_approval(
            conn,
            task_id=normalized_target,
            approver_type="human",
        )
        return created.id

    try:
        return int(normalized_target)
    except ValueError as exc:
        raise ValueError(f"unknown approval target {target!r}") from exc


def _cmd_approval_decide(args: argparse.Namespace, *, status: str) -> int:
    with kb.connect() as conn:
        approval_id = _resolve_manual_approval_target(conn, args.approval_target)
        aggregate_status = approvals_db.record_manual_task_approval_decision(
            conn,
            approval_id=approval_id,
            status=status,
            comment=getattr(args, "comment", None),
            comment_author=_profile_author(),
        )
        approval = approvals_db.get_task_approval(conn, approval_id)
        assert approval is not None
        payload = _approval_mutation_payload(conn, approval)
        payload["aggregate_status"] = aggregate_status

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        verb = "approved" if status == "approved" else "rejected"
        print(
            f"Recorded {verb} decision for approval #{approval.id} on {approval.task_id}; "
            f"task status is now {payload['task_status']}"
        )
    return 0


def _cmd_approval_reset(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        approval = approvals_db.reset_task_approval(conn, args.approval_id)
        payload = _approval_mutation_payload(conn, approval)

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Reset approval #{approval.id} on {approval.task_id} to requested; "
            f"task status remains {payload['task_status']}"
        )
    return 0


def _cmd_approval_reclaim(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        approval = approvals_db.reclaim_task_approval(conn, args.approval_id)
        payload = _approval_mutation_payload(conn, approval)

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Reclaimed approval #{approval.id} on {approval.task_id}; "
            f"task status remains {payload['task_status']}"
        )
    return 0


def _dispatch_approval(args: argparse.Namespace) -> int:
    sub = getattr(args, "approval_action", None)
    if not sub:
        print("kanban approval: specify a subcommand (request, ls, list, runs, remove, approve, reject, reset, reclaim)", file=sys.stderr)
        return 2
    if sub == "request":
        return _cmd_approval_request(args)
    if sub in {"ls", "list"}:
        return _cmd_approval_list(args)
    if sub == "runs":
        return _cmd_approval_runs(args)
    if sub == "remove":
        return _cmd_approval_remove(args)
    if sub == "approve":
        return _cmd_approval_decide(args, status="approved")
    if sub == "reject":
        return _cmd_approval_decide(args, status="rejected")
    if sub == "reset":
        return _cmd_approval_reset(args)
    if sub == "reclaim":
        return _cmd_approval_reclaim(args)
    print(f"kanban approval: unknown action {sub!r}", file=sys.stderr)
    return 2


def dispatch_approval_command(args: argparse.Namespace) -> int:
    return _dispatch_approval(args)


def register_approval_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p_approval = subparsers.add_parser(
        "approval",
        help="Manage task approval rows",
    )
    approval_sub = p_approval.add_subparsers(dest="approval_action")

    p_approval_request = approval_sub.add_parser("request", help="Request an approval row on a task")
    p_approval_request.add_argument("task_id")
    approval_identity = p_approval_request.add_mutually_exclusive_group(required=True)
    approval_identity.add_argument("--human", action="store_true", help="Create a human approval gate")
    approval_identity.add_argument("--agent", metavar="PROFILE", help="Create an agent approval gate for this profile")
    p_approval_request.add_argument("--skill", default=None, help="Optional skill name for an agent approval")
    p_approval_request.add_argument("--json", action="store_true")

    p_approval_list = approval_sub.add_parser("ls", aliases=["list"], help="List approval rows")
    p_approval_list.add_argument("task_id", nargs="?", default=None, help="Restrict to one task id")
    p_approval_list.add_argument(
        "--status",
        default=None,
        choices=sorted(approvals_db.VALID_APPROVAL_STATUSES),
        help="Restrict to one approval status",
    )
    active_scope = p_approval_list.add_mutually_exclusive_group()
    active_scope.add_argument("--all", action="store_true", help="Include parent tasks in any status")
    active_scope.add_argument("--active", action="store_true", help="Only include parent tasks in approval status")
    p_approval_list.add_argument("--flat", action="store_true", help="Show one row per approval instead of grouping by task")
    approver_type = p_approval_list.add_mutually_exclusive_group()
    approver_type.add_argument("--human", action="store_true", help="Restrict to human approvals")
    approver_type.add_argument("--agent", action="store_true", help="Restrict to agent approvals")
    p_approval_list.add_argument("--json", action="store_true")

    p_approval_runs = approval_sub.add_parser("runs", help="List runs for one approval row")
    p_approval_runs.add_argument("approval_id", type=int)
    p_approval_runs.add_argument("--json", action="store_true")

    p_approval_remove = approval_sub.add_parser("remove", help="Remove one approval row")
    p_approval_remove.add_argument("approval_id", type=int)
    p_approval_remove.add_argument("--json", action="store_true")

    p_approval_approve = approval_sub.add_parser("approve", help="Record a human approval decision")
    p_approval_approve.add_argument("approval_target", help="Approval id or task id")
    p_approval_approve.add_argument("--comment", default=None, help="Optional task comment to append")
    p_approval_approve.add_argument("--json", action="store_true")

    p_approval_reject = approval_sub.add_parser("reject", help="Record a human rejection decision")
    p_approval_reject.add_argument("approval_target", help="Approval id or task id")
    p_approval_reject.add_argument("--comment", default=None, help="Optional task comment to append")
    p_approval_reject.add_argument("--json", action="store_true")

    p_approval_reset = approval_sub.add_parser("reset", help="Reset one approval row back to requested")
    p_approval_reset.add_argument("approval_id", type=int)
    p_approval_reset.add_argument("--json", action="store_true")

    p_approval_reclaim = approval_sub.add_parser("reclaim", help="Reclaim one running agent approval and cancel it")
    p_approval_reclaim.add_argument("approval_id", type=int)
    p_approval_reclaim.add_argument("--json", action="store_true")

    return p_approval
