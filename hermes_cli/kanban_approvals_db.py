"""Approval-row models and helpers for the Hermes Kanban board."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from hermes_cli import kanban_db as kb

VALID_APPROVAL_TYPES = {"human", "agent"}
VALID_APPROVAL_STATUSES = {
    "requested",
    "running",
    "approved",
    "rejected",
    "escalated",
    "failed",
}
TERMINAL_APPROVAL_STATUSES = VALID_APPROVAL_STATUSES - {"requested", "running"}
DECISION_APPROVAL_STATUSES = {"approved", "rejected", "escalated"}
VALID_APPROVAL_RUN_STATUSES = {
    "running",
    "approved",
    "rejected",
    "escalated",
    "failed",
    "crashed",
    "timed_out",
    "reclaimed",
    "spawn_failed",
}


@dataclass
class Approval:
    """In-memory view of a ``task_approvals`` row."""

    id: int
    task_id: str
    approver_type: str
    approver_profile: Optional[str]
    approver_skill: Optional[str]
    status: str
    comment_id: Optional[int]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    last_heartbeat_at: Optional[int]
    current_run_id: Optional[int]
    consecutive_failures: int
    last_failure_error: Optional[str]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Approval":
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            approver_type=row["approver_type"],
            approver_profile=row["approver_profile"],
            approver_skill=row["approver_skill"],
            status=row["status"],
            comment_id=(int(row["comment_id"]) if row["comment_id"] is not None else None),
            claim_lock=row["claim_lock"],
            claim_expires=(int(row["claim_expires"]) if row["claim_expires"] is not None else None),
            worker_pid=(int(row["worker_pid"]) if row["worker_pid"] is not None else None),
            last_heartbeat_at=(
                int(row["last_heartbeat_at"]) if row["last_heartbeat_at"] is not None else None
            ),
            current_run_id=(int(row["current_run_id"]) if row["current_run_id"] is not None else None),
            consecutive_failures=int(row["consecutive_failures"]),
            last_failure_error=row["last_failure_error"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass
class ApprovalRun:
    """In-memory view of a ``task_approval_runs`` row."""

    id: int
    approval_id: int
    task_id: str
    profile: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    comment_id: Optional[int]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ApprovalRun":
        return cls(
            id=int(row["id"]),
            approval_id=int(row["approval_id"]),
            task_id=row["task_id"],
            profile=row["profile"],
            status=row["status"],
            claim_lock=row["claim_lock"],
            claim_expires=(int(row["claim_expires"]) if row["claim_expires"] is not None else None),
            worker_pid=(int(row["worker_pid"]) if row["worker_pid"] is not None else None),
            last_heartbeat_at=(
                int(row["last_heartbeat_at"]) if row["last_heartbeat_at"] is not None else None
            ),
            started_at=int(row["started_at"]),
            ended_at=(int(row["ended_at"]) if row["ended_at"] is not None else None),
            outcome=row["outcome"],
            comment_id=(int(row["comment_id"]) if row["comment_id"] is not None else None),
            error=row["error"],
        )


def _normalize_approval_profile(profile: Optional[str]) -> Optional[str]:
    profile = kb._normalize_optional_text(profile)
    if profile is None:
        return None
    return kb._canonical_assignee(profile)


def _validate_approval_identity(
    *,
    approver_type: str,
    approver_profile: Optional[str],
    approver_skill: Optional[str],
) -> tuple[str, Optional[str], Optional[str]]:
    if approver_type not in VALID_APPROVAL_TYPES:
        raise ValueError(f"approver_type must be one of {sorted(VALID_APPROVAL_TYPES)}")

    normalized_profile = _normalize_approval_profile(approver_profile)
    normalized_skill = kb._normalize_optional_text(approver_skill)

    if approver_type == "human":
        if normalized_profile is not None:
            raise ValueError("human approvals cannot set approver_profile")
        if normalized_skill is not None:
            raise ValueError("human approvals cannot set approver_skill")
        return approver_type, None, None

    if normalized_profile is None:
        raise ValueError("agent approvals require approver_profile")
    return approver_type, normalized_profile, normalized_skill


def _validate_approval_status(status: str) -> str:
    normalized_status = kb._normalize_optional_text(status)
    if normalized_status not in VALID_APPROVAL_STATUSES:
        raise ValueError(f"approval status must be one of {sorted(VALID_APPROVAL_STATUSES)}")
    return normalized_status


def _validate_terminal_approval_status(status: str) -> str:
    normalized_status = _validate_approval_status(status)
    if normalized_status not in TERMINAL_APPROVAL_STATUSES:
        raise ValueError(
            f"terminal approval status must be one of {sorted(TERMINAL_APPROVAL_STATUSES)}"
        )
    return normalized_status


def _validate_approval_decision_status(status: str) -> str:
    normalized_status = _validate_approval_status(status)
    if normalized_status not in DECISION_APPROVAL_STATUSES:
        raise ValueError(
            f"approval decision status must be one of {sorted(DECISION_APPROVAL_STATUSES)}"
        )
    return normalized_status


def _validate_approval_run_status(status: str) -> str:
    normalized_status = kb._normalize_optional_text(status)
    if normalized_status not in VALID_APPROVAL_RUN_STATUSES:
        raise ValueError(
            f"approval run status must be one of {sorted(VALID_APPROVAL_RUN_STATUSES)}"
        )
    return normalized_status


def _validate_task_comment_reference(
    conn: sqlite3.Connection, *, task_id: str, comment_id: Optional[int]
) -> Optional[int]:
    if comment_id is None:
        return None

    normalized_comment_id = int(comment_id)
    row = conn.execute(
        "SELECT task_id FROM task_comments WHERE id = ?",
        (normalized_comment_id,),
    ).fetchone()
    if not row or row["task_id"] != task_id:
        raise ValueError("comment_id must reference a comment on the same task")
    return normalized_comment_id


def _approval_exists(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    approver_type: str,
    approver_profile: Optional[str],
    approver_skill: Optional[str],
) -> bool:
    if approver_type == "human":
        row = conn.execute(
            "SELECT 1 FROM task_approvals WHERE task_id = ? AND approver_type = 'human'",
            (task_id,),
        ).fetchone()
        return row is not None

    if approver_skill is None:
        row = conn.execute(
            """
            SELECT 1
              FROM task_approvals
             WHERE task_id = ?
               AND approver_type = 'agent'
               AND approver_profile = ?
               AND approver_skill IS NULL
            """,
            (task_id, approver_profile),
        ).fetchone()
        return row is not None

    row = conn.execute(
        """
        SELECT 1
          FROM task_approvals
         WHERE task_id = ?
           AND approver_type = 'agent'
           AND approver_profile = ?
           AND approver_skill = ?
        """,
        (task_id, approver_profile, approver_skill),
    ).fetchone()
    return row is not None


def create_task_approval(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    approver_type: str,
    approver_profile: Optional[str] = None,
    approver_skill: Optional[str] = None,
    status: str = "requested",
    comment_id: Optional[int] = None,
) -> Approval:
    approver_type, approver_profile, approver_skill = _validate_approval_identity(
        approver_type=approver_type,
        approver_profile=approver_profile,
        approver_skill=approver_skill,
    )
    status = _validate_approval_status(status)
    now = int(time.time())

    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        if task.status in {"done", "archived"}:
            raise ValueError(f"cannot add approvals to tasks in status {task.status}")
        comment_id = _validate_task_comment_reference(
            conn, task_id=task_id, comment_id=comment_id
        )
        if _approval_exists(
            conn,
            task_id=task_id,
            approver_type=approver_type,
            approver_profile=approver_profile,
            approver_skill=approver_skill,
        ):
            if approver_type == "human":
                raise ValueError(f"task {task_id} already has a human approval")
            raise ValueError(
                "task already has an agent approval for that profile/skill combination"
            )

        cur = conn.execute(
            """
            INSERT INTO task_approvals (
                task_id, approver_type, approver_profile, approver_skill,
                status, comment_id, claim_lock, claim_expires, worker_pid,
                last_heartbeat_at, current_run_id, consecutive_failures,
                last_failure_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, NULL, ?, ?)
            """,
            (
                task_id,
                approver_type,
                approver_profile,
                approver_skill,
                status,
                comment_id,
                now,
                now,
            ),
        )
        approval_id = cur.lastrowid
        assert approval_id is not None
        approval = get_task_approval(conn, approval_id)
        assert approval is not None
        return approval


def get_task_approval(conn: sqlite3.Connection, approval_id: int) -> Optional[Approval]:
    row = conn.execute(
        "SELECT * FROM task_approvals WHERE id = ?",
        (int(approval_id),),
    ).fetchone()
    return Approval.from_row(row) if row else None


def list_approvals(
    conn: sqlite3.Connection,
    *,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    approver_type: Optional[str] = None,
) -> list[Approval]:
    query = "SELECT * FROM task_approvals WHERE 1 = 1"
    params: list[object] = []
    if task_id is not None:
        query += " AND task_id = ?"
        params.append(task_id)
    if status is not None:
        query += " AND status = ?"
        params.append(_validate_approval_status(status))
    if approver_type is not None:
        if approver_type not in VALID_APPROVAL_TYPES:
            raise ValueError(
                f"approver_type must be one of {sorted(VALID_APPROVAL_TYPES)}"
            )
        query += " AND approver_type = ?"
        params.append(approver_type)

    if task_id is None:
        query += " ORDER BY task_id ASC, created_at ASC, id ASC"
    else:
        query += " ORDER BY created_at ASC, id ASC"
    rows = conn.execute(query, params).fetchall()
    return [Approval.from_row(row) for row in rows]


def list_task_approvals(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: Optional[str] = None,
    approver_type: Optional[str] = None,
) -> list[Approval]:
    return list_approvals(
        conn,
        task_id=task_id,
        status=status,
        approver_type=approver_type,
    )


def list_runnable_task_approvals(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> list[Approval]:
    """Return approval rows currently eligible for autonomous execution."""
    query = """
        SELECT a.*
          FROM task_approvals a
          JOIN tasks t ON t.id = a.task_id
         WHERE a.approver_type = 'agent'
           AND a.status = 'requested'
           AND a.approver_profile IS NOT NULL
           AND a.claim_lock IS NULL
           AND t.status = 'approving'
           AND t.status != 'archived'
         ORDER BY a.created_at ASC, a.id ASC
    """
    params: list[object] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(query, params).fetchall()
    return [Approval.from_row(row) for row in rows]


def claim_task_approval(
    conn: sqlite3.Connection,
    approval_id: int,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
    now: Optional[int] = None,
) -> Optional[Approval]:
    """Atomically transition one runnable approval row into a live run."""
    effective_now = int(time.time()) if now is None else int(now)
    claim_lock = claimer or kb._claimer_id()
    claim_expires = effective_now + kb._resolve_claim_ttl_seconds(ttl_seconds)

    with kb.write_txn(conn):
        cur = conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   last_heartbeat_at = ?,
                   updated_at = ?
             WHERE id = ?
               AND approver_type = 'agent'
               AND status = 'requested'
               AND approver_profile IS NOT NULL
               AND claim_lock IS NULL
               AND EXISTS (
                    SELECT 1
                      FROM tasks t
                     WHERE t.id = task_approvals.task_id
                       AND t.status = 'approving'
                       AND t.status != 'archived'
               )
            """,
            (
                claim_lock,
                claim_expires,
                effective_now,
                effective_now,
                int(approval_id),
            ),
        )
        if cur.rowcount != 1:
            return None

        approval = get_task_approval(conn, approval_id)
        assert approval is not None
        run_cur = conn.execute(
            """
            INSERT INTO task_approval_runs (
                approval_id, task_id, profile, status, claim_lock, claim_expires,
                worker_pid, last_heartbeat_at, started_at, ended_at, outcome,
                comment_id, error
            ) VALUES (?, ?, ?, 'running', ?, ?, NULL, ?, ?, NULL, NULL, NULL, NULL)
            """,
            (
                approval.id,
                approval.task_id,
                approval.approver_profile,
                claim_lock,
                claim_expires,
                effective_now,
                effective_now,
            ),
        )
        run_id = run_cur.lastrowid
        assert run_id is not None
        conn.execute(
            "UPDATE task_approvals SET current_run_id = ? WHERE id = ?",
            (int(run_id), approval.id),
        )
        kb._append_event(
            conn,
            approval.task_id,
            "approval_claimed",
            {
                "approval_id": approval.id,
                "lock": claim_lock,
                "expires": claim_expires,
                "run_id": int(run_id),
                "approver_profile": approval.approver_profile,
            },
            run_id=int(run_id),
        )
        claimed = get_task_approval(conn, approval.id)
        assert claimed is not None
        return claimed


def heartbeat_task_approval(
    conn: sqlite3.Connection,
    approval_id: int,
    *,
    run_id: int,
    note: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    now: Optional[int] = None,
) -> bool:
    """Touch approval heartbeat state for the active run and extend its claim TTL."""
    effective_now = int(time.time()) if now is None else int(now)
    claim_expires = effective_now + kb._resolve_claim_ttl_seconds(ttl_seconds)
    run_id = int(run_id)

    with kb.write_txn(conn):
        cur = conn.execute(
            """
            UPDATE task_approvals
               SET last_heartbeat_at = ?,
                   claim_expires = ?,
                   updated_at = ?
             WHERE id = ?
               AND status = 'running'
               AND current_run_id = ?
            """,
            (
                effective_now,
                claim_expires,
                effective_now,
                int(approval_id),
                run_id,
            ),
        )
        if cur.rowcount != 1:
            return False

        run_cur = conn.execute(
            """
            UPDATE task_approval_runs
               SET last_heartbeat_at = ?,
                   claim_expires = ?
             WHERE id = ?
               AND approval_id = ?
               AND status = 'running'
            """,
            (effective_now, claim_expires, run_id, int(approval_id)),
        )
        if run_cur.rowcount != 1:
            return False

        approval = get_task_approval(conn, approval_id)
        assert approval is not None
        payload: dict[str, object] = {"approval_id": approval.id}
        if note:
            payload["note"] = note
        kb._append_event(
            conn,
            approval.task_id,
            "approval_heartbeat",
            payload,
            run_id=run_id,
        )
    return True


def set_task_approval_worker_pid(
    conn: sqlite3.Connection,
    approval_id: int,
    pid: int,
    *,
    run_id: int,
) -> bool:
    """Record the spawned approval worker pid on the active run and approval row."""
    run_id = int(run_id)
    with kb.write_txn(conn):
        cur = conn.execute(
            """
            UPDATE task_approvals
               SET worker_pid = ?
             WHERE id = ?
               AND status = 'running'
               AND current_run_id = ?
            """,
            (int(pid), int(approval_id), run_id),
        )
        if cur.rowcount != 1:
            return False

        run_cur = conn.execute(
            """
            UPDATE task_approval_runs
               SET worker_pid = ?
             WHERE id = ?
               AND approval_id = ?
               AND status = 'running'
            """,
            (int(pid), run_id, int(approval_id)),
        )
        if run_cur.rowcount != 1:
            return False

        approval = get_task_approval(conn, approval_id)
        assert approval is not None
        kb._append_event(
            conn,
            approval.task_id,
            "approval_spawned",
            {"approval_id": approval.id, "pid": int(pid)},
            run_id=run_id,
        )
    return True


def record_manual_task_approval_decision(
    conn: sqlite3.Connection,
    *,
    approval_id: int,
    status: str,
    comment: Optional[str] = None,
    comment_author: Optional[str] = None,
    now: Optional[int] = None,
) -> str:
    """Record a human approval decision through the same aggregate authority path."""
    normalized_status = _validate_approval_decision_status(status)
    effective_now = int(time.time()) if now is None else int(now)

    with kb.write_txn(conn):
        approval = get_task_approval(conn, approval_id)
        if approval is None:
            raise ValueError(f"unknown approval {approval_id}")
        if approval.approver_type != "human":
            raise ValueError("manual approval decisions require a human approval")
        task = kb.get_task(conn, approval.task_id)
        assert task is not None
        if task.status != "approving":
            raise ValueError("parent task must be approving")
        comment_id = None
        if comment is not None:
            effective_author = kb._normalize_optional_text(comment_author)
            if effective_author is None:
                raise ValueError("comment author is required")
            comment_id = kb._insert_task_comment(
                conn,
                task_id=approval.task_id,
                author=effective_author,
                body=comment,
                now=effective_now,
            )

        cur = conn.execute(
            """
            UPDATE task_approvals
               SET status = ?,
                   comment_id = ?,
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   last_heartbeat_at = NULL,
                   current_run_id = NULL,
                   consecutive_failures = 0,
                   last_failure_error = NULL,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                normalized_status,
                comment_id,
                effective_now,
                int(approval_id),
            ),
        )
        assert cur.rowcount == 1

        approvals = list_task_approvals(conn, approval.task_id)
        return _apply_task_approval_aggregate_transition(
            conn,
            approval.task_id,
            approvals=approvals,
            now=effective_now,
        )


def remove_task_approval(conn: sqlite3.Connection, approval_id: int) -> Approval:
    with kb.write_txn(conn):
        approval = get_task_approval(conn, approval_id)
        if approval is None:
            raise ValueError(f"unknown approval {approval_id}")

        task = kb.get_task(conn, approval.task_id)
        assert task is not None
        conn.execute("DELETE FROM task_approval_runs WHERE approval_id = ?", (approval.id,))
        deleted = conn.execute("DELETE FROM task_approvals WHERE id = ?", (approval.id,))
        assert deleted.rowcount == 1

        if task.status == "approving":
            remaining = list_task_approvals(conn, approval.task_id)
            _apply_task_approval_aggregate_transition(
                conn,
                approval.task_id,
                approvals=remaining,
            )

        return approval


def reset_task_approval(conn: sqlite3.Connection, approval_id: int) -> Approval:
    now = int(time.time())

    with kb.write_txn(conn):
        approval = get_task_approval(conn, approval_id)
        if approval is None:
            raise ValueError(f"unknown approval {approval_id}")

        task = kb.get_task(conn, approval.task_id)
        assert task is not None
        if task.status != "approving":
            raise ValueError("parent task must be approving")

        cur = _reset_task_approval_row(conn, approval_id=int(approval_id), now=now)
        assert cur.rowcount == 1

        approval = get_task_approval(conn, approval_id)
        assert approval is not None
        return approval


_TASK_APPROVAL_RESET_SET_SQL = """
status = 'requested',
comment_id = NULL,
claim_lock = NULL,
claim_expires = NULL,
worker_pid = NULL,
last_heartbeat_at = NULL,
current_run_id = NULL,
consecutive_failures = 0,
last_failure_error = NULL,
updated_at = ?
"""


def _reset_task_approval_row(
    conn: sqlite3.Connection,
    *,
    approval_id: int,
    now: int,
) -> sqlite3.Cursor:
    return conn.execute(
        f"""
        UPDATE task_approvals
        SET {_TASK_APPROVAL_RESET_SET_SQL}
        WHERE id = ?
        """,
        (now, int(approval_id)),
    )


def _reset_task_approvals_for_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: Optional[int] = None,
) -> int:
    effective_now = int(time.time()) if now is None else int(now)
    cur = conn.execute(
        f"""
        UPDATE task_approvals
        SET {_TASK_APPROVAL_RESET_SET_SQL}
        WHERE task_id = ?
        """,
        (effective_now, task_id),
    )
    return int(cur.rowcount)


def _prepare_task_approvals_for_new_completion_cycle(
    conn: sqlite3.Connection,
    task_id: str,
) -> None:
    _reset_task_approvals_for_task(conn, task_id)


def _compute_task_approval_aggregate_status(
    approvals: Sequence[Approval],
) -> str:
    statuses = {approval.status for approval in approvals}
    if "running" in statuses:
        return "approving"
    if "rejected" in statuses:
        return "todo"
    if "requested" in statuses:
        return "approving"
    return "done"


def _apply_task_approval_aggregate_transition(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    approvals: Sequence[Approval],
    now: Optional[int] = None,
) -> str:
    aggregate_status = _compute_task_approval_aggregate_status(approvals)
    effective_now = int(time.time()) if now is None else int(now)

    if aggregate_status == "todo":
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'todo'
             WHERE id = ?
               AND status = 'approving'
            """,
            (task_id,),
        )
        if cur.rowcount == 1:
            _reset_task_approvals_for_task(conn, task_id, now=effective_now)
        return aggregate_status

    if aggregate_status == "done":
        conn.execute(
            """
            UPDATE tasks
               SET status = 'done'
             WHERE id = ?
               AND status = 'approving'
            """,
            (task_id,),
        )

    return aggregate_status


def _finalize_task_approval_result_if_owned(
    conn: sqlite3.Connection,
    *,
    approval: Approval,
    approval_id: int,
    expected_run_id: int,
    status: str,
    comment_id: Optional[int] = None,
    now: Optional[int] = None,
) -> Optional[str]:
    effective_now = int(time.time()) if now is None else int(now)
    finalized = _finalize_task_approval_row_if_owned(
        conn,
        approval_id=approval_id,
        expected_run_id=expected_run_id,
        status=status,
        comment_id=comment_id,
        now=effective_now,
    )
    if not finalized:
        return None
    approvals = list_task_approvals(conn, approval.task_id)
    return _apply_task_approval_aggregate_transition(
        conn,
        approval.task_id,
        approvals=approvals,
        now=effective_now,
    )


def record_task_approval_decision(
    conn: sqlite3.Connection,
    *,
    approval_id: int,
    expected_run_id: int,
    status: str,
    comment_id: Optional[int] = None,
    now: Optional[int] = None,
) -> Optional[str]:
    normalized_status = _validate_approval_decision_status(status)
    normalized_run_status = _validate_approval_run_status(status)
    effective_now = int(time.time()) if now is None else int(now)

    with kb.write_txn(conn):
        approval = get_task_approval(conn, approval_id)
        if approval is None:
            return None

        run_row = conn.execute(
            """
            SELECT id
              FROM task_approval_runs
             WHERE id = ?
               AND approval_id = ?
               AND status = 'running'
            """,
            (int(expected_run_id), int(approval_id)),
        ).fetchone()
        if run_row is None:
            return None

        aggregate_status = _finalize_task_approval_result_if_owned(
            conn,
            approval=approval,
            approval_id=approval_id,
            expected_run_id=expected_run_id,
            status=normalized_status,
            comment_id=comment_id,
            now=effective_now,
        )
        if aggregate_status is None:
            return None

        run_cur = conn.execute(
            """
            UPDATE task_approval_runs
               SET status = ?,
                   ended_at = ?,
                   outcome = ?,
                   comment_id = ?,
                   error = NULL
             WHERE id = ?
               AND approval_id = ?
               AND status = 'running'
            """,
            (
                normalized_run_status,
                effective_now,
                normalized_status,
                comment_id,
                int(expected_run_id),
                int(approval_id),
            ),
        )
        if run_cur.rowcount != 1:
            return None

        return aggregate_status


def _finalize_task_approval_row_if_owned(
    conn: sqlite3.Connection,
    *,
    approval_id: int,
    expected_run_id: int,
    status: str,
    comment_id: Optional[int] = None,
    now: Optional[int] = None,
) -> bool:
    approval = get_task_approval(conn, approval_id)
    if approval is None:
        raise ValueError(f"unknown approval {approval_id}")

    normalized_status = _validate_terminal_approval_status(status)
    normalized_comment_id = _validate_task_comment_reference(
        conn, task_id=approval.task_id, comment_id=comment_id
    )
    effective_now = int(time.time()) if now is None else int(now)

    cur = conn.execute(
        """
        UPDATE task_approvals
           SET status = ?,
               comment_id = ?,
               claim_lock = NULL,
               claim_expires = NULL,
               worker_pid = NULL,
               last_heartbeat_at = NULL,
               current_run_id = NULL,
               consecutive_failures = 0,
               last_failure_error = NULL,
               updated_at = ?
         WHERE id = ?
           AND status = 'running'
           AND current_run_id = ?
        """,
        (
            normalized_status,
            normalized_comment_id,
            effective_now,
            int(approval_id),
            int(expected_run_id),
        ),
    )
    return cur.rowcount == 1


def create_task_approval_run(
    conn: sqlite3.Connection,
    *,
    approval_id: int,
    status: str = "running",
    profile: Optional[str] = None,
    claim_lock: Optional[str] = None,
    claim_expires: Optional[int] = None,
    worker_pid: Optional[int] = None,
    last_heartbeat_at: Optional[int] = None,
    started_at: Optional[int] = None,
    ended_at: Optional[int] = None,
    outcome: Optional[str] = None,
    comment_id: Optional[int] = None,
    error: Optional[str] = None,
) -> ApprovalRun:
    status = _validate_approval_run_status(status)
    normalized_profile = _normalize_approval_profile(profile)

    with kb.write_txn(conn):
        approval = get_task_approval(conn, approval_id)
        if approval is None:
            raise ValueError(f"unknown approval {approval_id}")
        if approval.approver_type != "agent":
            raise ValueError("approval runs require an agent approval")

        effective_profile = normalized_profile or approval.approver_profile
        if effective_profile is None:
            raise ValueError("approval runs require profile")

        normalized_comment_id = _validate_task_comment_reference(
            conn, task_id=approval.task_id, comment_id=comment_id
        )
        effective_started_at = int(started_at) if started_at is not None else int(time.time())
        effective_ended_at = int(ended_at) if ended_at is not None else None

        cur = conn.execute(
            """
            INSERT INTO task_approval_runs (
                approval_id, task_id, profile, status, claim_lock, claim_expires,
                worker_pid, last_heartbeat_at, started_at, ended_at, outcome,
                comment_id, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(approval_id),
                approval.task_id,
                effective_profile,
                status,
                kb._normalize_optional_text(claim_lock),
                (int(claim_expires) if claim_expires is not None else None),
                (int(worker_pid) if worker_pid is not None else None),
                (int(last_heartbeat_at) if last_heartbeat_at is not None else None),
                effective_started_at,
                effective_ended_at,
                kb._normalize_optional_text(outcome),
                normalized_comment_id,
                kb._normalize_optional_text(error),
            ),
        )
        run_id = cur.lastrowid
        assert run_id is not None
        row = conn.execute(
            "SELECT * FROM task_approval_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        return ApprovalRun.from_row(row)
