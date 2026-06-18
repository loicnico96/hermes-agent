"""Focused tests for Kanban approval-row helpers and invariants."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_approvals_db as approvals_db


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_create_get_and_list_task_approvals(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        comment_id = kb.add_comment(conn, task_id, "user", "needs review")

        human = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="requested",
            comment_id=comment_id,
        )
        agent = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="Reviewer",
            approver_skill="security-review",
            status="running",
        )

        fetched = approvals_db.get_task_approval(conn, human.id)
        approvals = approvals_db.list_task_approvals(conn, task_id)
        running = approvals_db.list_task_approvals(conn, task_id, status="running")
        agents = approvals_db.list_task_approvals(conn, task_id, approver_type="agent")

    assert fetched == human
    assert [approval.id for approval in approvals] == [human.id, agent.id]
    assert [approval.id for approval in running] == [agent.id]
    assert [approval.id for approval in agents] == [agent.id]
    assert human.comment_id == comment_id
    assert human.approver_profile is None
    assert agent.approver_profile == "reviewer"
    assert agent.approver_skill == "security-review"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"approver_type": "robot"}, "approver_type must be one of"),
        (
            {"approver_type": "human", "approver_profile": "reviewer"},
            "human approvals cannot set approver_profile",
        ),
        (
            {"approver_type": "human", "approver_skill": "security-review"},
            "human approvals cannot set approver_skill",
        ),
        ({"approver_type": "agent"}, "agent approvals require approver_profile"),
        (
            {"approver_type": "agent", "approver_profile": "reviewer", "status": "pending"},
            "approval status must be one of",
        ),
    ],
)
def test_create_task_approval_rejects_invalid_rows(kanban_home, kwargs, message):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        with pytest.raises(ValueError, match=message):
            approvals_db.create_task_approval(conn, task_id=task_id, **kwargs)


def test_create_task_approval_rejects_unknown_task_and_cross_task_comment(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        other_task_id = kb.create_task(conn, title="other")
        other_comment_id = kb.add_comment(conn, other_task_id, "user", "foreign")

        with pytest.raises(ValueError, match="unknown task"):
            approvals_db.create_task_approval(
                conn,
                task_id="t_missing",
                approver_type="human",
            )

        with pytest.raises(ValueError, match="comment_id must reference a comment on the same task"):
            approvals_db.create_task_approval(
                conn,
                task_id=task_id,
                approver_type="human",
                comment_id=other_comment_id,
            )


@pytest.mark.parametrize("status", ["done", "archived"])
def test_create_task_approval_rejects_terminal_tasks(kanban_home, status):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"{status} target")
        if status == "done":
            assert kb.complete_task(conn, task_id, result="finished") is True
        else:
            assert kb.archive_task(conn, task_id) is True

        with pytest.raises(ValueError, match=f"cannot add approvals to tasks in status {status}"):
            approvals_db.create_task_approval(conn, task_id=task_id, approver_type="human")


def test_create_task_approval_enforces_human_and_agent_uniqueness(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approvals_db.create_task_approval(conn, task_id=task_id, approver_type="human")
        approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="Reviewer",
        )
        approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            approver_skill="security-review",
        )

        with pytest.raises(ValueError, match="already has a human approval"):
            approvals_db.create_task_approval(conn, task_id=task_id, approver_type="human")

        with pytest.raises(ValueError, match="already has an agent approval"):
            approvals_db.create_task_approval(
                conn,
                task_id=task_id,
                approver_type="agent",
                approver_profile="reviewer",
                approver_skill="",
            )

        with pytest.raises(ValueError, match="already has an agent approval"):
            approvals_db.create_task_approval(
                conn,
                task_id=task_id,
                approver_type="agent",
                approver_profile="Reviewer",
                approver_skill="security-review",
            )


def test_list_approvals_supports_board_wide_filters(kanban_home):
    with kb.connect() as conn:
        first_task_id = kb.create_task(conn, title="first target")
        second_task_id = kb.create_task(conn, title="second target")

        first_human = approvals_db.create_task_approval(conn, task_id=first_task_id, approver_type="human")
        first_agent = approvals_db.create_task_approval(
            conn,
            task_id=first_task_id,
            approver_type="agent",
            approver_profile="reviewer-a",
            status="running",
        )
        second_agent = approvals_db.create_task_approval(
            conn,
            task_id=second_task_id,
            approver_type="agent",
            approver_profile="reviewer-b",
            status="approved",
        )

        all_rows = approvals_db.list_approvals(conn)
        agent_rows = approvals_db.list_approvals(conn, approver_type="agent")
        running_rows = approvals_db.list_approvals(conn, status="running")

    expected_all = sorted(
        [first_human, first_agent, second_agent],
        key=lambda approval: (approval.task_id, approval.created_at, approval.id),
    )
    expected_agents = sorted(
        [first_agent, second_agent],
        key=lambda approval: (approval.task_id, approval.created_at, approval.id),
    )

    assert [approval.id for approval in all_rows] == [approval.id for approval in expected_all]
    assert [approval.id for approval in agent_rows] == [approval.id for approval in expected_agents]
    assert [approval.id for approval in running_rows] == [first_agent.id]


def test_list_runnable_task_approvals_filters_to_requested_agents_on_approval_tasks(kanban_home):
    with kb.connect() as conn:
        runnable_task_id = kb.create_task(conn, title="runnable", assignee="ops")
        blocked_task_id = kb.create_task(conn, title="blocked", assignee="ops")
        archived_task_id = kb.create_task(conn, title="archived", assignee="ops")

        runnable = approvals_db.create_task_approval(
            conn,
            task_id=runnable_task_id,
            approver_type="agent",
            approver_profile="reviewer-a",
            status="requested",
        )
        approvals_db.create_task_approval(
            conn,
            task_id=runnable_task_id,
            approver_type="human",
            status="requested",
        )
        claimed = approvals_db.create_task_approval(
            conn,
            task_id=runnable_task_id,
            approver_type="agent",
            approver_profile="reviewer-b",
            status="requested",
        )
        wrong_status = approvals_db.create_task_approval(
            conn,
            task_id=runnable_task_id,
            approver_type="agent",
            approver_profile="reviewer-c",
            status="running",
        )
        wrong_task_status = approvals_db.create_task_approval(
            conn,
            task_id=blocked_task_id,
            approver_type="agent",
            approver_profile="reviewer-d",
            status="requested",
        )
        archived = approvals_db.create_task_approval(
            conn,
            task_id=archived_task_id,
            approver_type="agent",
            approver_profile="reviewer-e",
            status="requested",
        )

        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (runnable_task_id,))
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (blocked_task_id,))
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (archived_task_id,))
        conn.execute(
            "UPDATE task_approvals SET claim_lock = ?, updated_at = ? WHERE id = ?",
            ("lease-1", 1_500, claimed.id),
        )

        approvals = approvals_db.list_runnable_task_approvals(conn)
        limited = approvals_db.list_runnable_task_approvals(conn, limit=1)

    assert [approval.id for approval in approvals] == [runnable.id]
    assert [approval.id for approval in limited] == [runnable.id]
    assert claimed.id not in [approval.id for approval in approvals]
    assert wrong_status.id not in [approval.id for approval in approvals]
    assert wrong_task_status.id not in [approval.id for approval in approvals]
    assert archived.id not in [approval.id for approval in approvals]


def test_claim_task_approval_creates_live_run_and_claim_event(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="Reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        claimed = approvals_db.claim_task_approval(
            conn,
            approval.id,
            ttl_seconds=90,
            claimer="dispatcher:123",
            now=1_000,
        )
        run_row = conn.execute(
            "SELECT * FROM task_approval_runs WHERE approval_id = ?",
            (approval.id,),
        ).fetchone()
        event_row = conn.execute(
            "SELECT kind, payload, run_id FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.claim_lock == "dispatcher:123"
    assert claimed.claim_expires == 1_090
    assert claimed.last_heartbeat_at == 1_000
    assert claimed.current_run_id is not None
    assert run_row is not None
    assert run_row["id"] == claimed.current_run_id
    assert run_row["status"] == "running"
    assert run_row["profile"] == "reviewer"
    assert run_row["claim_lock"] == "dispatcher:123"
    assert run_row["claim_expires"] == 1_090
    assert run_row["last_heartbeat_at"] == 1_000
    assert run_row["started_at"] == 1_000
    assert event_row is not None
    assert event_row["kind"] == "approval_claimed"
    assert event_row["run_id"] is None
    assert json.loads(event_row["payload"]) == {
        "approval_id": approval.id,
        "lock": "dispatcher:123",
        "expires": 1090,
        "approval_run_id": claimed.current_run_id,
        "approver_profile": "reviewer",
    }


def test_claim_task_approval_returns_none_when_row_is_not_runnable(kanban_home):
    with kb.connect() as conn:
        todo_task_id = kb.create_task(conn, title="todo target")
        reviewing_task_id = kb.create_task(conn, title="review target")
        todo_approval = approvals_db.create_task_approval(
            conn,
            task_id=todo_task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        human_approval = approvals_db.create_task_approval(
            conn,
            task_id=reviewing_task_id,
            approver_type="human",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (reviewing_task_id,))

        todo_claim = approvals_db.claim_task_approval(conn, todo_approval.id, claimer="dispatcher:1", now=2_000)
        human_claim = approvals_db.claim_task_approval(conn, human_approval.id, claimer="dispatcher:1", now=2_000)
        run_count = conn.execute("SELECT COUNT(*) AS count FROM task_approval_runs").fetchone()["count"]

    assert todo_claim is None
    assert human_claim is None
    assert run_count == 0


def test_task_approval_runtime_helpers_update_live_row_and_run_together(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        claimed = approvals_db.claim_task_approval(
            conn,
            approval.id,
            ttl_seconds=60,
            claimer="dispatcher:55",
            now=500,
        )
        assert claimed is not None
        assert claimed.current_run_id is not None

        assert approvals_db.set_task_approval_worker_pid(conn, approval.id, 4321, run_id=claimed.current_run_id)
        assert approvals_db.heartbeat_task_approval(
            conn,
            approval.id,
            run_id=claimed.current_run_id,
            note="still reviewing",
            ttl_seconds=120,
            now=700,
        )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        run_row = conn.execute(
            "SELECT worker_pid, last_heartbeat_at, claim_expires FROM task_approval_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        event_rows = conn.execute(
            "SELECT kind, payload, run_id FROM task_events WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()

    assert refreshed is not None
    assert refreshed.worker_pid == 4321
    assert refreshed.last_heartbeat_at == 700
    assert refreshed.claim_expires == 820
    assert run_row is not None
    assert run_row["worker_pid"] == 4321
    assert run_row["last_heartbeat_at"] == 700
    assert run_row["claim_expires"] == 820
    assert [row["kind"] for row in event_rows[-2:]] == ["approval_spawned", "approval_heartbeat"]
    assert event_rows[-2]["run_id"] is None
    assert json.loads(event_rows[-2]["payload"]) == {
        "approval_id": approval.id,
        "approval_run_id": claimed.current_run_id,
        "pid": 4321,
    }
    assert event_rows[-1]["run_id"] is None
    assert json.loads(event_rows[-1]["payload"]) == {
        "approval_id": approval.id,
        "approval_run_id": claimed.current_run_id,
        "note": "still reviewing",
    }


def test_record_task_approval_decision_emits_approval_run_in_payload_but_not_task_event_run_id(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   current_run_id = ?,
                   claim_lock = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (run.id, "lease-1", 8_900, approval.id),
        )

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="approved",
            now=9_000,
        )
        event_row = conn.execute(
            "SELECT kind, payload, run_id FROM task_events WHERE task_id = ? AND kind = 'approval_decided' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert aggregate_status == "done"
    assert event_row is not None
    assert event_row["kind"] == "approval_decided"
    assert event_row["run_id"] is None
    assert json.loads(event_row["payload"]) == {
        "approval_id": approval.id,
        "approver_type": "agent",
        "approver_profile": "reviewer",
        "decision": "approved",
        "approval_run_id": run.id,
        "next_status": "done",
    }


def test_task_approval_runtime_helpers_require_matching_run_id(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        claimed = approvals_db.claim_task_approval(
            conn,
            approval.id,
            ttl_seconds=60,
            claimer="dispatcher:55",
            now=500,
        )
        assert claimed is not None
        assert claimed.current_run_id is not None

        assert not approvals_db.set_task_approval_worker_pid(conn, approval.id, 4321, run_id=claimed.current_run_id + 1)
        assert not approvals_db.heartbeat_task_approval(
            conn,
            approval.id,
            run_id=claimed.current_run_id + 1,
            note="wrong run",
            ttl_seconds=120,
            now=700,
        )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        run_row = conn.execute(
            "SELECT worker_pid, last_heartbeat_at, claim_expires FROM task_approval_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        event_rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()

    assert refreshed is not None
    assert refreshed.worker_pid is None
    assert refreshed.last_heartbeat_at == 500
    assert refreshed.claim_expires == 560
    assert run_row is not None
    assert run_row["worker_pid"] is None
    assert run_row["last_heartbeat_at"] == 500
    assert run_row["claim_expires"] == 560
    assert [row["kind"] for row in event_rows] == ["created", "approval_requested", "approval_claimed"]


def test_list_task_approval_runs_filters_by_task_and_approval(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        other_task_id = kb.create_task(conn, title="other target")
        first = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
        )
        second = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="coder",
        )
        foreign = approvals_db.create_task_approval(
            conn,
            task_id=other_task_id,
            approver_type="agent",
            approver_profile="ops",
        )

        run1 = approvals_db._create_task_approval_run_for_tests(conn, approval_id=first.id, status="running", started_at=10)
        run2 = approvals_db._create_task_approval_run_for_tests(conn, approval_id=first.id, status="timed_out", started_at=20)
        run3 = approvals_db._create_task_approval_run_for_tests(conn, approval_id=second.id, status="approved", started_at=30)
        approvals_db._create_task_approval_run_for_tests(conn, approval_id=foreign.id, status="failed", started_at=40)

        task_runs = approvals_db.list_task_approval_runs(conn, task_id)
        first_runs = approvals_db.list_task_approval_runs(conn, task_id, approval_id=first.id)
        timed_out = approvals_db.list_approval_runs(conn, status="timed_out")

    assert [run.id for run in task_runs] == [run1.id, run2.id, run3.id]
    assert [run.id for run in first_runs] == [run1.id, run2.id]
    assert [run.id for run in timed_out] == [run2.id]


def test_record_task_approval_decision_treats_missing_approval_as_stale_noop(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")

        conn.execute("DELETE FROM task_approvals WHERE id = ?", (approval.id,))

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="approved",
            now=9_000,
        )
        run_row = conn.execute(
            "SELECT status, ended_at, outcome FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()

    assert aggregate_status is None
    assert run_row is not None
    assert run_row["status"] == "running"
    assert run_row["ended_at"] is None
    assert run_row["outcome"] is None


def test_record_task_approval_decision_treats_missing_run_row_as_stale_noop(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   current_run_id = ?,
                   claim_lock = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (run.id, "lease-1", 8_900, approval.id),
        )
        conn.execute("DELETE FROM task_approval_runs WHERE id = ?", (run.id,))

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="approved",
            now=9_100,
        )
        refreshed = approvals_db.get_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert aggregate_status is None
    assert refreshed is not None
    assert refreshed.status == "running"
    assert refreshed.current_run_id == run.id
    assert refreshed.claim_lock == "lease-1"
    assert refreshed.updated_at == 8_900
    assert task is not None
    assert task.status == "approval"


def test_record_task_approval_decision_rejects_live_or_retry_statuses(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="running",
        )

        with pytest.raises(ValueError, match="approval decision status must be one of"):
            approvals_db.record_task_approval_decision(
                conn,
                approval_id=approval.id,
                expected_run_id=1,
                status="running",
            )

        with pytest.raises(ValueError, match="approval decision status must be one of"):
            approvals_db.record_task_approval_decision(
                conn,
                approval_id=approval.id,
                expected_run_id=1,
                status="failed",
            )


def test_reset_task_approval_clears_mutable_fields_and_preserves_identity(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 5_000)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        comment_id = kb.add_comment(conn, task_id, "user", "stale review")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            approver_skill="security-review",
            status="running",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")

        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                SET comment_id = ?,
                    claim_lock = ?,
                    claim_expires = ?,
                    worker_pid = ?,
                    last_heartbeat_at = ?,
                    current_run_id = ?,
                    consecutive_failures = ?,
                    last_failure_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    comment_id,
                    "lease-1",
                    123,
                    456,
                    789,
                    run.id,
                    3,
                    "spawn failed",
                    4_000,
                    approval.id,
                ),
            )
            conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        reset = approvals_db.reset_task_approval(conn, approval.id)
        run_row = conn.execute(
            "SELECT status, ended_at, outcome FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()

    assert reset.id == approval.id
    assert reset.task_id == task_id
    assert reset.approver_type == "agent"
    assert reset.approver_profile == "reviewer"
    assert reset.approver_skill == "security-review"
    assert reset.created_at == approval.created_at
    assert reset.status == "requested"
    assert reset.comment_id is None
    assert reset.claim_lock is None
    assert reset.claim_expires is None
    assert reset.worker_pid is None
    assert reset.last_heartbeat_at is None
    assert reset.current_run_id is None
    assert reset.consecutive_failures == 0
    assert reset.last_failure_error is None
    assert reset.updated_at == 5_000
    assert run_row is not None
    assert run_row["status"] == "reclaimed"
    assert run_row["ended_at"] == 5_000
    assert run_row["outcome"] == "reclaimed"


def test_reset_task_approval_requires_parent_task_to_be_approval(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="approved",
        )

        with pytest.raises(ValueError, match="parent task must be in approval status"):
            approvals_db.reset_task_approval(conn, approval.id)


@pytest.mark.parametrize("status", ["requested", "running", "approved", "rejected", "escalated", "failed"])
def test_reset_task_approval_accepts_any_existing_status(kanban_home, monkeypatch, status):
    monkeypatch.setattr(kb.time, "time", lambda: 8_000)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status=status,
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        reset = approvals_db.reset_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert reset.status == "requested"
    assert reset.updated_at == 8_000
    assert task is not None
    assert task.status == "approval"


def test_reset_task_approval_is_noop_for_requested_rows(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_100)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        requested_events_before = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'approval_requested'",
            (task_id,),
        ).fetchone()[0]

        reset = approvals_db.reset_task_approval(conn, approval.id)
        requested_events_after = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'approval_requested'",
            (task_id,),
        ).fetchone()[0]

    assert reset.status == "requested"
    assert reset.updated_at == approval.updated_at
    assert requested_events_after == requested_events_before



def test_reset_task_approval_rejects_unknown_approval(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="unknown approval"):
            approvals_db.reset_task_approval(conn, 999999)



def test_reclaim_task_approval_reclaims_running_agent_row(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_200)
    killed: list[tuple[int | None, str | None]] = []

    def fake_terminate(pid, claim_lock, *, signal_fn=None):
        killed.append((pid, claim_lock))
        return {
            "prev_pid": pid,
            "host_local": True,
            "termination_attempted": True,
            "terminated": True,
            "sigkill": False,
        }

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", fake_terminate)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   worker_pid = ?,
                   last_heartbeat_at = ?,
                   current_run_id = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            ("lease-1", 123, 456, 789, run.id, 8_100, approval.id),
        )

        reclaimed = approvals_db.reclaim_task_approval(conn, approval.id)
        run_row = conn.execute(
            "SELECT status, ended_at, outcome, worker_pid FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()
        cancelled_event = conn.execute(
            "SELECT kind, payload, run_id FROM task_events WHERE task_id = ? AND kind = 'approval_cancelled' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert reclaimed.status == "cancelled"
    assert reclaimed.worker_pid is None
    assert reclaimed.current_run_id is None
    assert reclaimed.updated_at == 8_200
    assert run_row is not None
    assert run_row["status"] == "reclaimed"
    assert run_row["ended_at"] == 8_200
    assert run_row["outcome"] == "reclaimed"
    assert run_row["worker_pid"] is None
    assert cancelled_event is not None
    assert cancelled_event["kind"] == "approval_cancelled"
    assert cancelled_event["run_id"] is None
    assert json.loads(cancelled_event["payload"]) == {
        "approval_id": approval.id,
        "approver_type": "agent",
        "approver_profile": "reviewer",
        "approval_run_id": run.id,
    }
    assert killed == [(456, "lease-1")]



def test_reclaim_task_approval_requires_running_agent_row(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        human = approvals_db.create_task_approval(conn, task_id=task_id, approver_type="human")
        agent = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        with pytest.raises(ValueError, match="reclaim requires a running agent approval"):
            approvals_db.reclaim_task_approval(conn, human.id)
        with pytest.raises(ValueError, match="reclaim requires a running agent approval"):
            approvals_db.reclaim_task_approval(conn, agent.id)



def test_reclaim_task_approval_transitions_task_done_when_last_running_agent_is_reclaimed(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_300)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   worker_pid = ?,
                   last_heartbeat_at = ?,
                   current_run_id = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            ("lease-1", 123, 456, 789, run.id, 8_250, approval.id),
        )

        reclaimed = approvals_db.reclaim_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert reclaimed.status == "cancelled"
    assert task is not None
    assert task.status == "done"



def test_reclaim_task_approval_transitions_task_todo_when_other_rejection_exists(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_350)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        running = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        rejected = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer-2",
            status="rejected",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=running.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   worker_pid = ?,
                   last_heartbeat_at = ?,
                   current_run_id = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            ("lease-2", 321, 654, 987, run.id, 8_325, running.id),
        )

        reclaimed = approvals_db.reclaim_task_approval(conn, running.id)
        task = kb.get_task(conn, task_id)
        rejected_after = approvals_db.get_task_approval(conn, rejected.id)

    assert reclaimed.status == "requested"
    assert task is not None
    assert task.status == "todo"
    assert rejected_after is not None
    assert rejected_after.status == "requested"



def test_remove_task_approval_recomputes_approval_parent_state(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        removed = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        survivor = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
        )
        removed_run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=removed.id, status="running")
        conn.execute(
            "UPDATE task_approvals SET current_run_id = NULL, claim_lock = NULL, updated_at = ? WHERE id = ?",
            (7_500, removed.id),
        )
        conn.execute(
            "UPDATE task_approval_runs SET status = 'released', ended_at = ? WHERE id = ?",
            (7_500, removed_run.id),
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        deleted = approvals_db.remove_task_approval(conn, removed.id)
        task = kb.get_task(conn, task_id)
        survivor_after = approvals_db.get_task_approval(conn, survivor.id)
        removed_after = approvals_db.get_task_approval(conn, removed.id)
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_approval_runs WHERE approval_id = ?",
            (removed.id,),
        ).fetchone()[0]

    assert deleted.id == removed.id
    assert deleted.task_id == task_id
    assert removed_after is None
    assert survivor_after is not None
    assert survivor_after.id == survivor.id
    assert run_count == 0
    assert task is not None
    assert task.status == "done"



def test_remove_task_approval_leaves_non_approval_parent_status_unchanged(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )

        deleted = approvals_db.remove_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert deleted.id == approval.id
    assert task is not None
    assert task.status == "ready"



def test_remove_task_approval_rejects_unknown_approval(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="unknown approval"):
            approvals_db.remove_task_approval(conn, 999999)


def test_remove_task_approval_allows_actively_owned_rows_and_deletes_run_rows(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute(
            "UPDATE task_approvals SET current_run_id = ?, claim_lock = ?, updated_at = ? WHERE id = ?",
            (run.id, "lease-1", 7_600, approval.id),
        )

        deleted = approvals_db.remove_task_approval(conn, approval.id)

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_approval_runs WHERE approval_id = ?",
            (approval.id,),
        ).fetchone()[0]

    assert deleted.id == approval.id
    assert refreshed is None
    assert run_count == 0


def test_record_manual_task_approval_decision_allows_valid_human_runtime_states(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_950)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        human = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="rejected",
        )
        agent = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        aggregate_status = approvals_db.record_manual_task_approval_decision(
            conn,
            approval_id=human.id,
            status="approved",
            comment="re-opened and approved",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed_human = approvals_db.get_task_approval(conn, human.id)
        refreshed_agent = approvals_db.get_task_approval(conn, agent.id)
        comments = kb.list_comments(conn, task_id)

    assert aggregate_status == "done"
    assert task is not None
    assert task.status == "done"
    assert refreshed_human is not None
    assert refreshed_human.status == "approved"
    assert refreshed_human.comment_id == comments[0].id
    assert refreshed_human.updated_at == 8_950
    assert refreshed_agent is not None
    assert refreshed_agent.status == "requested"
    assert [comment.body for comment in comments] == ["re-opened and approved"]



def test_record_manual_task_approval_decision_approves_human_gate_and_keeps_comment(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_000)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(conn, task_id=task_id, approver_type="human")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        aggregate_status = approvals_db.record_manual_task_approval_decision(
            conn,
            approval_id=approval.id,
            status="approved",
            comment="ship it",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed = approvals_db.get_task_approval(conn, approval.id)
        comments = kb.list_comments(conn, task_id)

    assert aggregate_status == "done"
    assert task is not None
    assert task.status == "done"
    assert refreshed is not None
    assert refreshed.status == "approved"
    assert refreshed.comment_id == comments[0].id
    assert refreshed.updated_at == 8_000
    assert [comment.author for comment in comments] == ["reviewer"]
    assert [comment.body for comment in comments] == ["ship it"]



def test_record_manual_task_approval_decision_rejects_and_resets_cycle(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_500)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        human = approvals_db.create_task_approval(conn, task_id=task_id, approver_type="human")
        agent = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="approved",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        aggregate_status = approvals_db.record_manual_task_approval_decision(
            conn,
            approval_id=human.id,
            status="rejected",
            comment="needs changes",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed_human = approvals_db.get_task_approval(conn, human.id)
        refreshed_agent = approvals_db.get_task_approval(conn, agent.id)
        comments = kb.list_comments(conn, task_id)

    assert aggregate_status == "todo"
    assert task is not None
    assert task.status == "todo"
    assert refreshed_human is not None
    assert refreshed_human.status == "requested"
    assert refreshed_human.comment_id is None
    assert refreshed_human.updated_at == 8_500
    assert refreshed_agent is not None
    assert refreshed_agent.status == "requested"
    assert refreshed_agent.comment_id is None
    assert refreshed_agent.updated_at == 8_500
    assert [comment.author for comment in comments] == ["reviewer"]
    assert [comment.body for comment in comments] == ["needs changes"]


def test_record_manual_task_approval_decision_allows_human_change_of_mind_while_approval(
    kanban_home,
    monkeypatch,
):
    monkeypatch.setattr(kb.time, "time", lambda: 8_750)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        human = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
        )
        agent = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        aggregate_status = approvals_db.record_manual_task_approval_decision(
            conn,
            approval_id=human.id,
            status="rejected",
            comment="changed my mind",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed_human = approvals_db.get_task_approval(conn, human.id)
        refreshed_agent = approvals_db.get_task_approval(conn, agent.id)
        comments = kb.list_comments(conn, task_id)

    assert aggregate_status == "todo"
    assert task is not None
    assert task.status == "todo"
    assert refreshed_human is not None
    assert refreshed_human.status == "requested"
    assert refreshed_human.comment_id is None
    assert refreshed_human.updated_at == 8_750
    assert refreshed_agent is not None
    assert refreshed_agent.status == "requested"
    assert refreshed_agent.updated_at == 8_750
    assert [comment.author for comment in comments] == ["reviewer"]
    assert [comment.body for comment in comments] == ["changed my mind"]


def test_record_manual_task_approval_decision_allows_reaffirming_human_approval_while_approval(
    kanban_home,
    monkeypatch,
):
    monkeypatch.setattr(kb.time, "time", lambda: 8_900)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        human = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
        )
        agent = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        aggregate_status = approvals_db.record_manual_task_approval_decision(
            conn,
            approval_id=human.id,
            status="approved",
            comment="still approved",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed_human = approvals_db.get_task_approval(conn, human.id)
        refreshed_agent = approvals_db.get_task_approval(conn, agent.id)
        comments = kb.list_comments(conn, task_id)

    assert aggregate_status == "done"
    assert task is not None
    assert task.status == "done"
    assert refreshed_human is not None
    assert refreshed_human.status == "approved"
    assert refreshed_human.comment_id == comments[0].id
    assert refreshed_human.updated_at == 8_900
    assert refreshed_agent is not None
    assert refreshed_agent.status == "requested"
    assert [comment.author for comment in comments] == ["reviewer"]
    assert [comment.body for comment in comments] == ["still approved"]


@pytest.mark.parametrize(
    ("approval_kwargs", "task_status", "message"),
    [
        (
            {"approver_type": "agent", "approver_profile": "reviewer"},
            "approval",
            "manual approval decisions require a human approval",
        ),
            ({"approver_type": "human"}, "done", "parent task must be in approval status"),
],
)
def test_record_manual_task_approval_decision_enforces_human_and_approval_gate(
    kanban_home,
    approval_kwargs,
    task_status,
    message,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(conn, task_id=task_id, **approval_kwargs)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (task_status, task_id))

        with pytest.raises(ValueError, match=message):
            approvals_db.record_manual_task_approval_decision(
                conn,
                approval_id=approval.id,
                status="approved",
            )


def test_reset_task_approvals_for_task_resets_only_target_task_rows(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 7_000)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        other_task_id = kb.create_task(conn, title="other target")
        comment_id = kb.add_comment(conn, task_id, "user", "stale review")
        other_comment_id = kb.add_comment(conn, other_task_id, "user", "other stale review")

        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="failed",
        )
        other = approvals_db.create_task_approval(
            conn,
            task_id=other_task_id,
            approver_type="agent",
            approver_profile="security",
            status="approved",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")

        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                SET comment_id = ?,
                    claim_lock = ?,
                    claim_expires = ?,
                    worker_pid = ?,
                    last_heartbeat_at = ?,
                    current_run_id = ?,
                    consecutive_failures = ?,
                    last_failure_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (comment_id, "lease-1", 123, 456, 789, run.id, 3, "spawn failed", 6_000, approval.id),
            )
            conn.execute(
                """
                UPDATE task_approvals
                SET comment_id = ?,
                    consecutive_failures = ?,
                    last_failure_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (other_comment_id, 2, "keep me", 6_100, other.id),
            )

            reset_count = approvals_db.reset_task_approvals_in_txn(conn, task_id)

        reset = approvals_db.get_task_approval(conn, approval.id)
        untouched = approvals_db.get_task_approval(conn, other.id)

    assert reset_count == 1
    assert reset is not None
    assert reset.status == "requested"
    assert reset.comment_id is None
    assert reset.claim_lock is None
    assert reset.claim_expires is None
    assert reset.worker_pid is None
    assert reset.last_heartbeat_at is None
    assert reset.current_run_id is None
    assert reset.consecutive_failures == 0
    assert reset.last_failure_error is None
    assert reset.updated_at == 7_000

    assert untouched is not None
    assert untouched.status == "approved"
    assert untouched.comment_id == other_comment_id
    assert untouched.consecutive_failures == 2
    assert untouched.last_failure_error == "keep me"
    assert untouched.updated_at == 6_100


@pytest.mark.parametrize(
    ("statuses", "expected_status"),
    [
        ([], "done"),
        (["requested"], "approval"),
        (["running"], "approval"),
        (["approved"], "done"),
        (["approved", "approved"], "done"),
        (["approved", "requested"], "done"),
        (["approved", "running"], "done"),
        (["approved", "escalated"], "done"),
        (["requested", "rejected"], "todo"),
        (["running", "rejected"], "approval"),
        (["approved", "rejected"], "done"),
        (["escalated", "approved"], "approval"),
        (["escalated"], "approval"),
        (["failed", "approved"], "done"),
        (["failed", "requested"], "approval"),
        (["failed", "running"], "approval"),
    ],
)
def test_compute_task_approval_aggregate_status(kanban_home, statuses, expected_status):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="aggregate target")
        approvals = []
        for index, status in enumerate(statuses):
            if index == 0:
                approvals.append(
                    approvals_db.create_task_approval(
                        conn,
                        task_id=task_id,
                        approver_type="human",
                        status=status,
                    )
                )
            else:
                approvals.append(
                    approvals_db.create_task_approval(
                        conn,
                        task_id=task_id,
                        approver_type="agent",
                        approver_profile=f"reviewer-{index}",
                        status=status,
                    )
                )

    assert approvals_db.compute_task_approval_aggregate_status(approvals) == expected_status


def test_apply_task_approval_aggregate_transition_marks_approval_task_done_when_all_gates_are_satisfied(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="aggregate target", assignee="ops")
        agent = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="escalated",
        )
        human = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
        )
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        kb._append_event(
            conn,
            task_id,
            "awaiting_approval",
            {
                "result_len": 12,
                "summary": "fresh summary",
                "task_status": "approval",
                "verified_cards": ["t_child1234"],
                "artifacts": ["/tmp/report.txt"],
            },
            run_id=77,
        )

        with kb.write_txn(conn):
            aggregate_status = approvals_db.apply_task_approval_aggregate_transition_in_txn(
                conn,
                task_id,
                approvals=[agent, human],
                now=9_000,
            )

        task = kb.get_task(conn, task_id)
        refreshed_agent = approvals_db.get_task_approval(conn, agent.id)
        refreshed_human = approvals_db.get_task_approval(conn, human.id)
        completed_event = conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert aggregate_status == "done"
    assert task is not None
    assert task.status == "done"
    assert refreshed_agent is not None
    assert refreshed_agent.status == "escalated"
    assert refreshed_human is not None
    assert refreshed_human.status == "approved"
    assert completed_event is not None
    assert completed_event["run_id"] == 77
    assert json.loads(completed_event["payload"]) == {
        "result_len": 12,
        "summary": "fresh summary",
        "task_status": "done",
        "verified_cards": ["t_child1234"],
        "artifacts": ["/tmp/report.txt"],
    }



def test_apply_task_approval_aggregate_transition_marks_approval_task_done_when_last_gate_is_removed(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="aggregate target", assignee="ops")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        with kb.write_txn(conn):
            aggregate_status = approvals_db.apply_task_approval_aggregate_transition_in_txn(
                conn,
                task_id,
                approvals=[],
                now=9_000,
            )

        task = kb.get_task(conn, task_id)

    assert aggregate_status == "done"
    assert task is not None
    assert task.status == "done"



def test_record_task_approval_decision_marks_approval_task_done_when_live_gate_is_satisfied(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval_comment_id = kb.add_comment(conn, task_id, "reviewer", "looks good")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                   SET status = 'running',
                       claim_lock = ?,
                       claim_expires = ?,
                       worker_pid = ?,
                       last_heartbeat_at = ?,
                       current_run_id = ?,
                       consecutive_failures = ?,
                       last_failure_error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                ("lease-1", 123, 456, 789, run.id, 2, "old failure", 7_000, approval.id),
            )

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="approved",
            comment_id=approval_comment_id,
            now=9_000,
        )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)
        run_row = conn.execute(
            "SELECT status, ended_at, outcome, comment_id, error FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()

    assert aggregate_status == "done"
    assert refreshed is not None
    assert refreshed.status == "approved"
    assert refreshed.comment_id == approval_comment_id
    assert refreshed.current_run_id is None
    assert refreshed.consecutive_failures == 0
    assert refreshed.last_failure_error is None
    assert task is not None
    assert task.status == "done"
    assert run_row is not None
    assert run_row["status"] == "approved"
    assert run_row["ended_at"] == 9_000
    assert run_row["outcome"] == "approved"
    assert run_row["comment_id"] == approval_comment_id
    assert run_row["error"] is None


def test_record_task_approval_decision_escalated_resets_existing_human_gate(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        agent_comment_id = kb.add_comment(conn, task_id, "reviewer", "needs human eyes")
        human_comment_id = kb.add_comment(conn, task_id, "approver", "previously approved")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        human = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
            comment_id=human_comment_id,
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                   SET status = 'running',
                       claim_lock = ?,
                       claim_expires = ?,
                       worker_pid = ?,
                       last_heartbeat_at = ?,
                       current_run_id = ?,
                       consecutive_failures = ?,
                       last_failure_error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                ("lease-1", 123, 456, 789, run.id, 2, "old failure", 7_000, approval.id),
            )
            conn.execute(
                """
                UPDATE task_approvals
                   SET comment_id = ?,
                       consecutive_failures = ?,
                       last_failure_error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (human_comment_id, 2, "stale human state", 7_100, human.id),
            )

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="escalated",
            comment_id=agent_comment_id,
            now=9_000,
        )

        task = kb.get_task(conn, task_id)
        refreshed_agent = approvals_db.get_task_approval(conn, approval.id)
        refreshed_human = approvals_db.get_task_approval(conn, human.id)
        run_row = conn.execute(
            "SELECT status, ended_at, outcome, comment_id, error FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()
        human_count = conn.execute(
            "SELECT COUNT(*) FROM task_approvals WHERE task_id = ? AND approver_type = 'human'",
            (task_id,),
        ).fetchone()[0]

    assert aggregate_status == "approval"
    assert task is not None
    assert task.status == "approval"
    assert human_count == 1
    assert refreshed_agent is not None
    assert refreshed_agent.status == "escalated"
    assert refreshed_agent.comment_id == agent_comment_id
    assert refreshed_agent.current_run_id is None
    assert refreshed_agent.consecutive_failures == 0
    assert refreshed_agent.last_failure_error is None
    assert refreshed_human is not None
    assert refreshed_human.id == human.id
    assert refreshed_human.status == "requested"
    assert refreshed_human.comment_id is None
    assert refreshed_human.claim_lock is None
    assert refreshed_human.current_run_id is None
    assert refreshed_human.consecutive_failures == 0
    assert refreshed_human.last_failure_error is None
    assert refreshed_human.updated_at == 9_000
    assert run_row is not None
    assert run_row["status"] == "escalated"
    assert run_row["ended_at"] == 9_000
    assert run_row["outcome"] == "escalated"
    assert run_row["comment_id"] == agent_comment_id
    assert run_row["error"] is None


def test_record_task_approval_decision_escalated_creates_human_gate_when_missing(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'running', current_run_id = ?, updated_at = ? WHERE id = ?",
            (run.id, 7_000, approval.id),
        )

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="escalated",
            now=9_000,
        )

        task = kb.get_task(conn, task_id)
        approvals = approvals_db.list_task_approvals(conn, task_id)

    assert aggregate_status == "approval"
    assert task is not None
    assert task.status == "approval"
    assert {(row.approver_type, row.status) for row in approvals} == {
        ("agent", "escalated"),
        ("human", "requested"),
    }


def test_finalize_task_approval_row_if_owned_applies_terminal_status(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval_comment_id = kb.add_comment(conn, task_id, "reviewer", "looks good")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                   SET status = 'running',
                       claim_lock = ?,
                       claim_expires = ?,
                       worker_pid = ?,
                       last_heartbeat_at = ?,
                       current_run_id = ?,
                       consecutive_failures = ?,
                       last_failure_error = ?
                 WHERE id = ?
                """,
                ("lease-1", 123, 456, 789, run.id, 2, "old failure", approval.id),
            )

            finalized = approvals_db.finalize_task_approval_row_if_owned_in_txn(
                conn,
                approval_id=approval.id,
                expected_run_id=run.id,
                status="approved",
                comment_id=approval_comment_id,
                now=9_000,
            )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert finalized is True
    assert refreshed is not None
    assert refreshed.status == "approved"
    assert refreshed.comment_id == approval_comment_id
    assert refreshed.claim_lock is None
    assert refreshed.claim_expires is None
    assert refreshed.worker_pid is None
    assert refreshed.last_heartbeat_at is None
    assert refreshed.current_run_id is None
    assert refreshed.consecutive_failures == 0
    assert refreshed.last_failure_error is None
    assert refreshed.updated_at == 9_000
    assert task is not None
    assert task.status == "approval"


def test_finalize_task_approval_row_if_owned_discards_non_running_rows(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                   SET status = 'failed',
                       claim_lock = ?,
                       current_run_id = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                ("lease-1", run.id, 6_000, approval.id),
            )

            finalized = approvals_db.finalize_task_approval_row_if_owned_in_txn(
                conn,
                approval_id=approval.id,
                expected_run_id=run.id,
                status="approved",
                now=9_000,
            )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert finalized is False
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.claim_lock == "lease-1"
    assert refreshed.current_run_id == run.id
    assert refreshed.updated_at == 6_000
    assert task is not None
    assert task.status == "approval"


def test_finalize_task_approval_row_if_owned_discards_run_ownership_mismatches(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        stale_run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                   SET status = 'running',
                       claim_lock = ?,
                       current_run_id = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                ("lease-1", run.id, 6_500, approval.id),
            )

            finalized = approvals_db.finalize_task_approval_row_if_owned_in_txn(
                conn,
                approval_id=approval.id,
                expected_run_id=stale_run.id,
                status="rejected",
                now=9_000,
            )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert finalized is False
    assert refreshed is not None
    assert refreshed.status == "running"
    assert refreshed.claim_lock == "lease-1"
    assert refreshed.current_run_id == run.id
    assert refreshed.updated_at == 6_500
    assert task is not None
    assert task.status == "approval"


def test_record_task_approval_decision_keeps_task_approval_while_other_approval_is_running(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        rejected_comment_id = kb.add_comment(conn, task_id, "reviewer", "needs revision")
        rejected = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        other = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer-2",
            status="requested",
        )
        rejected_run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=rejected.id, status="running")
        other_run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=other.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE task_approvals
                   SET status = 'running',
                       claim_lock = ?,
                       claim_expires = ?,
                       worker_pid = ?,
                       last_heartbeat_at = ?,
                       current_run_id = ?,
                       consecutive_failures = ?,
                       last_failure_error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                ("lease-1", 111, 222, 333, rejected_run.id, 2, "old failure", 7_000, rejected.id),
            )
            conn.execute(
                """
                UPDATE task_approvals
                   SET status = 'running',
                       claim_lock = ?,
                       claim_expires = ?,
                       worker_pid = ?,
                       last_heartbeat_at = ?,
                       current_run_id = ?,
                       consecutive_failures = ?,
                       last_failure_error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                ("lease-2", 444, 555, 666, other_run.id, 4, "keep me", 7_100, other.id),
            )

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=rejected.id,
            expected_run_id=rejected_run.id,
            status="rejected",
            comment_id=rejected_comment_id,
            now=9_000,
        )

        task = kb.get_task(conn, task_id)
        refreshed_rejected = approvals_db.get_task_approval(conn, rejected.id)
        refreshed_other = approvals_db.get_task_approval(conn, other.id)
        rejected_run_row = conn.execute(
            "SELECT status, ended_at, outcome, comment_id, error FROM task_approval_runs WHERE id = ?",
            (rejected_run.id,),
        ).fetchone()

    assert aggregate_status == "approval"

    assert task is not None
    assert task.status == "approval"

    assert refreshed_rejected is not None
    assert refreshed_rejected.status == "rejected"
    assert refreshed_rejected.comment_id == rejected_comment_id
    assert refreshed_rejected.claim_lock is None
    assert refreshed_rejected.claim_expires is None
    assert refreshed_rejected.worker_pid is None
    assert refreshed_rejected.last_heartbeat_at is None
    assert refreshed_rejected.current_run_id is None
    assert refreshed_rejected.consecutive_failures == 0
    assert refreshed_rejected.last_failure_error is None
    assert refreshed_rejected.updated_at == 9_000

    assert refreshed_other is not None
    assert refreshed_other.status == "running"
    assert refreshed_other.comment_id is None
    assert refreshed_other.claim_lock == "lease-2"
    assert refreshed_other.claim_expires == 444
    assert refreshed_other.worker_pid == 555
    assert refreshed_other.last_heartbeat_at == 666
    assert refreshed_other.current_run_id == other_run.id
    assert refreshed_other.consecutive_failures == 4
    assert refreshed_other.last_failure_error == "keep me"
    assert refreshed_other.updated_at == 7_100
    assert rejected_run_row is not None
    assert rejected_run_row["status"] == "rejected"
    assert rejected_run_row["ended_at"] == 9_000
    assert rejected_run_row["outcome"] == "rejected"
    assert rejected_run_row["comment_id"] == rejected_comment_id
    assert rejected_run_row["error"] is None


def test_record_task_approval_decision_moves_task_to_todo_after_last_running_approval_finishes(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        rejected_comment_id = kb.add_comment(conn, task_id, "reviewer", "needs revision")
        final_comment_id = kb.add_comment(conn, task_id, "reviewer-2", "late approval")
        rejected = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        other = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer-2",
            status="requested",
        )
        rejected_run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=rejected.id, status="running")
        other_run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=other.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_approvals SET status = 'running', current_run_id = ?, updated_at = ? WHERE id = ?",
                (rejected_run.id, 7_000, rejected.id),
            )
            conn.execute(
                "UPDATE task_approvals SET status = 'running', current_run_id = ?, updated_at = ? WHERE id = ?",
                (other_run.id, 7_100, other.id),
            )

        first_aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=rejected.id,
            expected_run_id=rejected_run.id,
            status="rejected",
            comment_id=rejected_comment_id,
            now=9_000,
        )
        assert first_aggregate_status == "approval"

        final_aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=other.id,
            expected_run_id=other_run.id,
            status="approved",
            comment_id=final_comment_id,
            now=9_100,
        )

        task = kb.get_task(conn, task_id)
        refreshed_rejected = approvals_db.get_task_approval(conn, rejected.id)
        refreshed_other = approvals_db.get_task_approval(conn, other.id)

    assert final_aggregate_status == "todo"
    assert task is not None
    assert task.status == "todo"
    assert refreshed_rejected is not None
    assert refreshed_rejected.status == "requested"
    assert refreshed_rejected.comment_id is None
    assert refreshed_rejected.current_run_id is None
    assert refreshed_rejected.updated_at == 9_100
    assert refreshed_other is not None
    assert refreshed_other.status == "requested"
    assert refreshed_other.comment_id is None
    assert refreshed_other.current_run_id is None
    assert refreshed_other.updated_at == 9_100


def test_record_task_approval_decision_discards_results_after_task_leaves_approval(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'running', current_run_id = ?, claim_lock = ?, updated_at = ? WHERE id = ?",
            (run.id, "lease-1", 7_000, approval.id),
        )

        aggregate_status = approvals_db.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="approved",
            now=9_000,
        )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        run_row = conn.execute(
            "SELECT status, ended_at, outcome FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()
        task = kb.get_task(conn, task_id)

    assert aggregate_status is None
    assert task is not None
    assert task.status == "todo"
    assert refreshed is not None
    assert refreshed.status == "running"
    assert refreshed.claim_lock == "lease-1"
    assert refreshed.current_run_id == run.id
    assert refreshed.updated_at == 7_000
    assert run_row is not None
    assert run_row["status"] == "running"
    assert run_row["ended_at"] is None
    assert run_row["outcome"] is None


def test_record_task_approval_failure_requeues_requested_under_limit(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   worker_pid = ?,
                   last_heartbeat_at = ?,
                   current_run_id = ?,
                   consecutive_failures = ?,
                   last_failure_error = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            ("lease-1", 123, 456, 789, run.id, 1, "older failure", 7_000, approval.id),
        )

        row_status = approvals_db.record_task_approval_failure(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            outcome="crashed",
            error="review worker crashed",
            now=9_000,
        )

        refreshed = approvals_db.get_task_approval(conn, approval.id)
        run_row = conn.execute(
            "SELECT status, ended_at, outcome, error FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()
        task = kb.get_task(conn, task_id)

    assert row_status == "requested"
    assert task is not None
    assert task.status == "approval"
    assert refreshed is not None
    assert refreshed.status == "requested"
    assert refreshed.claim_lock is None
    assert refreshed.current_run_id is None
    assert refreshed.consecutive_failures == 2
    assert refreshed.last_failure_error == "review worker crashed"
    assert refreshed.updated_at == 9_000
    assert run_row is not None
    assert run_row["status"] == "crashed"
    assert run_row["ended_at"] == 9_000
    assert run_row["outcome"] == "crashed"
    assert run_row["error"] == "review worker crashed"


def test_record_task_approval_failure_marks_failed_and_resets_human_gate_at_limit(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        human_comment_id = kb.add_comment(conn, task_id, "approver", "stale human approval")
        approval = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        human = approvals_db.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
            comment_id=human_comment_id,
        )
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   worker_pid = ?,
                   last_heartbeat_at = ?,
                   current_run_id = ?,
                   consecutive_failures = ?,
                   last_failure_error = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            ("lease-1", 123, 456, 789, run.id, 2, "older failure", 7_000, approval.id),
        )
        conn.execute(
            """
            UPDATE task_approvals
               SET comment_id = ?,
                   consecutive_failures = ?,
                   last_failure_error = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (human_comment_id, 1, "stale human state", 7_100, human.id),
        )

        row_status = approvals_db.record_task_approval_failure(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            outcome="timed_out",
            error="review worker timed out",
            now=9_000,
        )

        refreshed_agent = approvals_db.get_task_approval(conn, approval.id)
        refreshed_human = approvals_db.get_task_approval(conn, human.id)
        run_row = conn.execute(
            "SELECT status, ended_at, outcome, error FROM task_approval_runs WHERE id = ?",
            (run.id,),
        ).fetchone()
        task = kb.get_task(conn, task_id)

    assert row_status == "failed"
    assert task is not None
    assert task.status == "approval"
    assert refreshed_agent is not None
    assert refreshed_agent.status == "failed"
    assert refreshed_agent.current_run_id is None
    assert refreshed_agent.consecutive_failures == 3
    assert refreshed_agent.last_failure_error == "review worker timed out"
    assert refreshed_human is not None
    assert refreshed_human.id == human.id
    assert refreshed_human.status == "requested"
    assert refreshed_human.comment_id is None
    assert refreshed_human.current_run_id is None
    assert refreshed_human.consecutive_failures == 0
    assert refreshed_human.last_failure_error is None
    assert refreshed_human.updated_at == 9_000
    assert run_row is not None
    assert run_row["status"] == "timed_out"
    assert run_row["ended_at"] == 9_000
    assert run_row["outcome"] == "timed_out"
    assert run_row["error"] == "review worker timed out"


def test_approval_from_row_parses_nullable_and_identity_fields():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                7 AS id,
                't_approval' AS task_id,
                'agent' AS approver_type,
                'reviewer' AS approver_profile,
                'github-code-review' AS approver_skill,
                'requested' AS status,
                13 AS comment_id,
                'lease-123' AS claim_lock,
                1700000100 AS claim_expires,
                4242 AS worker_pid,
                1700000200 AS last_heartbeat_at,
                9 AS current_run_id,
                2 AS consecutive_failures,
                'spawn failed' AS last_failure_error,
                1700000000 AS created_at,
                1700000300 AS updated_at
            """
        ).fetchone()

        approval = approvals_db.Approval.from_row(row)

    assert approval == approvals_db.Approval(
        id=7,
        task_id="t_approval",
        approver_type="agent",
        approver_profile="reviewer",
        approver_skill="github-code-review",
        status="requested",
        comment_id=13,
        claim_lock="lease-123",
        claim_expires=1700000100,
        worker_pid=4242,
        last_heartbeat_at=1700000200,
        current_run_id=9,
        consecutive_failures=2,
        last_failure_error="spawn failed",
        created_at=1700000000,
        updated_at=1700000300,
    )


def test_approval_run_from_row_parses_optional_fields():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                11 AS id,
                7 AS approval_id,
                't_approval' AS task_id,
                'reviewer' AS profile,
                'approved' AS status,
                NULL AS claim_lock,
                NULL AS claim_expires,
                NULL AS worker_pid,
                NULL AS last_heartbeat_at,
                1700000000 AS started_at,
                1700000400 AS ended_at,
                'approved' AS outcome,
                15 AS comment_id,
                NULL AS error
            """
        ).fetchone()

        approval_run = approvals_db.ApprovalRun.from_row(row)

    assert approval_run == approvals_db.ApprovalRun(
        id=11,
        approval_id=7,
        task_id="t_approval",
        profile="reviewer",
        status="approved",
        claim_lock=None,
        claim_expires=None,
        worker_pid=None,
        last_heartbeat_at=None,
        started_at=1700000000,
        ended_at=1700000400,
        outcome="approved",
        comment_id=15,
        error=None,
    )
