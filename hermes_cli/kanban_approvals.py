"""Approval-specific CLI helpers for ``hermes kanban approval ...``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from hermes_cli import kanban_db as kb


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


def _approval_to_dict(approval: kb.Approval) -> dict[str, Any]:
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


def _format_approval_target(approval: kb.Approval) -> str:
    if approval.approver_type == "human":
        return "human"

    target = f"agent @{approval.approver_profile}"
    if approval.approver_skill:
        target += f" skill={approval.approver_skill}"
    return target


def _format_approval_line(approval: kb.Approval, *, include_task_id: bool) -> str:
    bits = [f"#{approval.id}", f"{approval.status:10s}", _format_approval_target(approval)]
    if include_task_id:
        bits.append(f"task={approval.task_id}")
    if approval.comment_id is not None:
        bits.append(f"comment=#{approval.comment_id}")
    return "  " + "  ".join(bits)


def _approval_mutation_payload(conn: Any, approval: kb.Approval) -> dict[str, Any]:
    task = kb.get_task(conn, approval.task_id)
    assert task is not None
    return {
        "approval": _approval_to_dict(approval),
        "task_status": task.status,
    }


def _cmd_approval_add(args: argparse.Namespace) -> int:
    approver_type = "human" if getattr(args, "human", False) else "agent"
    if approver_type == "human" and getattr(args, "skill", None):
        raise ValueError("--skill is only valid with --agent")

    with kb.connect() as conn:
        approval = kb.create_task_approval(
            conn,
            task_id=args.task_id,
            approver_type=approver_type,
            approver_profile=getattr(args, "agent", None),
            approver_skill=getattr(args, "skill", None),
        )

    if getattr(args, "json", False):
        print(json.dumps(_approval_to_dict(approval), indent=2, ensure_ascii=False))
    else:
        print(
            f"Added approval #{approval.id} to {approval.task_id} "
            f"({approval.status}, {_format_approval_target(approval)})"
        )
    return 0


def _cmd_approval_list(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        task_id = getattr(args, "task", None)
        if task_id is not None and kb.get_task(conn, task_id) is None:
            print(f"no such task: {task_id}", file=sys.stderr)
            return 1
        approvals = kb.list_approvals(
            conn,
            task_id=task_id,
            status=getattr(args, "status", None),
            approver_type=getattr(args, "approver_type", None),
        )

    if getattr(args, "json", False):
        print(json.dumps([_approval_to_dict(approval) for approval in approvals], indent=2, ensure_ascii=False))
        return 0

    if not approvals:
        print("(no matching approvals)")
        return 0

    include_task_id = task_id is None
    for approval in approvals:
        print(_format_approval_line(approval, include_task_id=include_task_id))
    return 0


def _cmd_approval_remove(args: argparse.Namespace) -> int:
    with kb.connect() as conn:
        approval = kb.remove_task_approval(conn, args.approval_id)
        payload = _approval_mutation_payload(conn, approval)

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Removed approval #{approval.id} from {approval.task_id}; "
            f"task status is now {payload['task_status']}"
        )
    return 0


def _cmd_approval_decide(args: argparse.Namespace, *, status: str) -> int:
    with kb.connect() as conn:
        aggregate_status = kb.record_manual_task_approval_decision(
            conn,
            approval_id=args.approval_id,
            status=status,
            comment=getattr(args, "comment", None),
            comment_author=_profile_author(),
        )
        approval = kb.get_task_approval(conn, args.approval_id)
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
        approval = kb.reset_task_approval(conn, args.approval_id)
        payload = _approval_mutation_payload(conn, approval)

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Reset approval #{approval.id} on {approval.task_id} to requested; "
            f"task status remains {payload['task_status']}"
        )
    return 0


def _dispatch_approval(args: argparse.Namespace) -> int:
    sub = getattr(args, "approval_action", None)
    if not sub:
        print("kanban approval: specify a subcommand (add, list, remove, approve, reject, reset)", file=sys.stderr)
        return 2
    if sub == "add":
        return _cmd_approval_add(args)
    if sub == "list":
        return _cmd_approval_list(args)
    if sub == "remove":
        return _cmd_approval_remove(args)
    if sub == "approve":
        return _cmd_approval_decide(args, status="approved")
    if sub == "reject":
        return _cmd_approval_decide(args, status="rejected")
    if sub == "reset":
        return _cmd_approval_reset(args)
    print(f"kanban approval: unknown action {sub!r}", file=sys.stderr)
    return 2


def register_approval_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p_approval = subparsers.add_parser(
        "approval",
        help="Manage task approval rows",
    )
    approval_sub = p_approval.add_subparsers(dest="approval_action")

    p_approval_add = approval_sub.add_parser("add", help="Attach an approval row to a task")
    p_approval_add.add_argument("task_id")
    approval_identity = p_approval_add.add_mutually_exclusive_group(required=True)
    approval_identity.add_argument("--human", action="store_true", help="Create a human approval gate")
    approval_identity.add_argument("--agent", metavar="PROFILE", help="Create an agent approval gate for this profile")
    p_approval_add.add_argument("--skill", default=None, help="Optional skill name for an agent approval")
    p_approval_add.add_argument("--json", action="store_true")

    p_approval_list = approval_sub.add_parser("list", help="List approval rows")
    p_approval_list.add_argument("--task", default=None, help="Restrict to one task id")
    p_approval_list.add_argument(
        "--status",
        default=None,
        choices=sorted(kb.VALID_APPROVAL_STATUSES),
        help="Restrict to one approval status",
    )
    p_approval_list.add_argument(
        "--type",
        dest="approver_type",
        default=None,
        choices=sorted(kb.VALID_APPROVAL_TYPES),
        help="Restrict to human or agent approvals",
    )
    p_approval_list.add_argument("--json", action="store_true")

    p_approval_remove = approval_sub.add_parser("remove", help="Remove one approval row")
    p_approval_remove.add_argument("approval_id", type=int)
    p_approval_remove.add_argument("--json", action="store_true")

    p_approval_approve = approval_sub.add_parser("approve", help="Record a human approval decision")
    p_approval_approve.add_argument("approval_id", type=int)
    p_approval_approve.add_argument("--comment", default=None, help="Optional task comment to append")
    p_approval_approve.add_argument("--json", action="store_true")

    p_approval_reject = approval_sub.add_parser("reject", help="Record a human rejection decision")
    p_approval_reject.add_argument("approval_id", type=int)
    p_approval_reject.add_argument("--comment", default=None, help="Optional task comment to append")
    p_approval_reject.add_argument("--json", action="store_true")

    p_approval_reset = approval_sub.add_parser("reset", help="Reset one approval row back to requested")
    p_approval_reset.add_argument("approval_id", type=int)
    p_approval_reset.add_argument("--json", action="store_true")

    return p_approval
