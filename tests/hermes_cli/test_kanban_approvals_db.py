"""Focused tests for Kanban approval-row helpers and invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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

        human = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="requested",
            comment_id=comment_id,
        )
        agent = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="Reviewer",
            approver_skill="security-review",
            status="running",
        )

        fetched = kb.get_task_approval(conn, human.id)
        approvals = kb.list_task_approvals(conn, task_id)
        running = kb.list_task_approvals(conn, task_id, status="running")
        agents = kb.list_task_approvals(conn, task_id, approver_type="agent")

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
            kb.create_task_approval(conn, task_id=task_id, **kwargs)


def test_create_task_approval_rejects_unknown_task_and_cross_task_comment(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        other_task_id = kb.create_task(conn, title="other")
        other_comment_id = kb.add_comment(conn, other_task_id, "user", "foreign")

        with pytest.raises(ValueError, match="unknown task"):
            kb.create_task_approval(
                conn,
                task_id="t_missing",
                approver_type="human",
            )

        with pytest.raises(ValueError, match="comment_id must reference a comment on the same task"):
            kb.create_task_approval(
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
            kb.create_task_approval(conn, task_id=task_id, approver_type="human")


def test_create_task_approval_enforces_human_and_agent_uniqueness(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        kb.create_task_approval(conn, task_id=task_id, approver_type="human")
        kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="Reviewer",
        )
        kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            approver_skill="security-review",
        )

        with pytest.raises(ValueError, match="already has a human approval"):
            kb.create_task_approval(conn, task_id=task_id, approver_type="human")

        with pytest.raises(ValueError, match="already has an agent approval"):
            kb.create_task_approval(
                conn,
                task_id=task_id,
                approver_type="agent",
                approver_profile="reviewer",
                approver_skill="",
            )

        with pytest.raises(ValueError, match="already has an agent approval"):
            kb.create_task_approval(
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

        first_human = kb.create_task_approval(conn, task_id=first_task_id, approver_type="human")
        first_agent = kb.create_task_approval(
            conn,
            task_id=first_task_id,
            approver_type="agent",
            approver_profile="reviewer-a",
            status="running",
        )
        second_agent = kb.create_task_approval(
            conn,
            task_id=second_task_id,
            approver_type="agent",
            approver_profile="reviewer-b",
            status="approved",
        )

        all_rows = kb.list_approvals(conn)
        agent_rows = kb.list_approvals(conn, approver_type="agent")
        running_rows = kb.list_approvals(conn, status="running")

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


def test_create_task_approval_run_inserts_agent_runs(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        comment_id = kb.add_comment(conn, task_id, "reviewer", "approved")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="Reviewer",
        )

        run = kb.create_task_approval_run(
            conn,
            approval_id=approval.id,
            status="approved",
            claim_lock=" lease-1 ",
            claim_expires=55,
            worker_pid=77,
            last_heartbeat_at=88,
            started_at=33,
            ended_at=44,
            outcome=" approved ",
            comment_id=comment_id,
        )

    assert run.approval_id == approval.id
    assert run.task_id == task_id
    assert run.profile == "reviewer"
    assert run.status == "approved"
    assert run.claim_lock == "lease-1"
    assert run.claim_expires == 55
    assert run.worker_pid == 77
    assert run.last_heartbeat_at == 88
    assert run.started_at == 33
    assert run.ended_at == 44
    assert run.outcome == "approved"
    assert run.comment_id == comment_id


@pytest.mark.parametrize(
    ("setup", "kwargs", "message"),
    [
        (
            "human",
            {"status": "running"},
            "approval runs require an agent approval",
        ),
        (
            "agent",
            {"status": "queued"},
            "approval run status must be one of",
        ),
    ],
)
def test_create_task_approval_run_rejects_invalid_rows(kanban_home, setup, kwargs, message):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type=setup,
            approver_profile=("reviewer" if setup == "agent" else None),
        )

        with pytest.raises(ValueError, match=message):
            kb.create_task_approval_run(conn, approval_id=approval.id, **kwargs)

        with pytest.raises(ValueError, match="unknown approval"):
            kb.create_task_approval_run(conn, approval_id=999999)


def test_create_task_approval_run_rejects_cross_task_comment(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        other_task_id = kb.create_task(conn, title="other")
        other_comment_id = kb.add_comment(conn, other_task_id, "user", "foreign")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
        )

        with pytest.raises(ValueError, match="comment_id must reference a comment on the same task"):
            kb.create_task_approval_run(
                conn,
                approval_id=approval.id,
                comment_id=other_comment_id,
            )


def test_record_task_approval_decision_treats_missing_approval_as_stale_noop(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")

        conn.execute("DELETE FROM task_approvals WHERE id = ?", (approval.id,))

        aggregate_status = kb.record_task_approval_decision(
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


def test_record_task_approval_decision_rejects_live_or_retry_statuses(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="running",
        )

        with pytest.raises(ValueError, match="approval decision status must be one of"):
            kb.record_task_approval_decision(
                conn,
                approval_id=approval.id,
                expected_run_id=1,
                status="running",
            )

        with pytest.raises(ValueError, match="approval decision status must be one of"):
            kb.record_task_approval_decision(
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
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            approver_skill="security-review",
            status="running",
        )
        run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")

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
            conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        reset = kb.reset_task_approval(conn, approval.id)
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
    assert run_row["status"] == "running"
    assert run_row["ended_at"] is None
    assert run_row["outcome"] is None


def test_reset_task_approval_requires_parent_task_to_be_approving(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="approved",
        )

        with pytest.raises(ValueError, match="parent task must be approving"):
            kb.reset_task_approval(conn, approval.id)


@pytest.mark.parametrize("status", ["running", "approved", "rejected", "escalated", "failed"])
def test_reset_task_approval_accepts_only_live_operator_reset_statuses(kanban_home, monkeypatch, status):
    monkeypatch.setattr(kb.time, "time", lambda: 8_000)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status=status,
        )
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        reset = kb.reset_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert reset.status == "requested"
    assert reset.updated_at == 8_000
    assert task is not None
    assert task.status == "approving"



def test_reset_task_approval_rejects_requested_rows(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        with pytest.raises(ValueError, match="reset only supports statuses"):
            kb.reset_task_approval(conn, approval.id)



def test_reset_task_approval_rejects_unknown_approval(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="unknown approval"):
            kb.reset_task_approval(conn, 999999)



def test_remove_task_approval_recomputes_approving_parent_state(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        removed = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        survivor = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
        )
        removed_run = kb.create_task_approval_run(conn, approval_id=removed.id, status="running")
        conn.execute(
            "UPDATE task_approvals SET current_run_id = ?, claim_lock = ?, updated_at = ? WHERE id = ?",
            (removed_run.id, "lease-1", 7_500, removed.id),
        )
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        deleted = kb.remove_task_approval(conn, removed.id)
        task = kb.get_task(conn, task_id)
        survivor_after = kb.get_task_approval(conn, survivor.id)
        removed_after = kb.get_task_approval(conn, removed.id)
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



def test_remove_task_approval_leaves_non_approving_parent_status_unchanged(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )

        deleted = kb.remove_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert deleted.id == approval.id
    assert task is not None
    assert task.status == "ready"



def test_remove_task_approval_rejects_unknown_approval(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="unknown approval"):
            kb.remove_task_approval(conn, 999999)



def test_record_manual_task_approval_decision_approves_human_gate_and_keeps_comment(kanban_home, monkeypatch):
    monkeypatch.setattr(kb.time, "time", lambda: 8_000)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = kb.create_task_approval(conn, task_id=task_id, approver_type="human")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        aggregate_status = kb.record_manual_task_approval_decision(
            conn,
            approval_id=approval.id,
            status="approved",
            comment="ship it",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed = kb.get_task_approval(conn, approval.id)
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
        human = kb.create_task_approval(conn, task_id=task_id, approver_type="human")
        agent = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="approved",
        )
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        aggregate_status = kb.record_manual_task_approval_decision(
            conn,
            approval_id=human.id,
            status="rejected",
            comment="needs changes",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed_human = kb.get_task_approval(conn, human.id)
        refreshed_agent = kb.get_task_approval(conn, agent.id)
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


def test_record_manual_task_approval_decision_allows_human_change_of_mind_while_approving(
    kanban_home,
    monkeypatch,
):
    monkeypatch.setattr(kb.time, "time", lambda: 8_750)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        human = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
        )
        agent = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        aggregate_status = kb.record_manual_task_approval_decision(
            conn,
            approval_id=human.id,
            status="rejected",
            comment="changed my mind",
            comment_author="reviewer",
        )

        task = kb.get_task(conn, task_id)
        refreshed_human = kb.get_task_approval(conn, human.id)
        refreshed_agent = kb.get_task_approval(conn, agent.id)
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


@pytest.mark.parametrize(
    ("approval_kwargs", "task_status", "approval_status", "message"),
    [
        (
            {"approver_type": "agent", "approver_profile": "reviewer"},
            "approving",
            "requested",
            "manual approval decisions require a human approval",
        ),
        ({"approver_type": "human"}, "done", "requested", "parent task must be approving"),
        (
            {"approver_type": "human"},
            "approving",
            "failed",
            "manual approval decisions require approval status in approved, requested",
        ),
    ],
)
def test_record_manual_task_approval_decision_enforces_human_allowed_status_and_approving_gate(
    kanban_home,
    approval_kwargs,
    task_status,
    approval_status,
    message,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = kb.create_task_approval(conn, task_id=task_id, status=approval_status, **approval_kwargs)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (task_status, task_id))

        with pytest.raises(ValueError, match=message):
            kb.record_manual_task_approval_decision(
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

        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="failed",
        )
        other = kb.create_task_approval(
            conn,
            task_id=other_task_id,
            approver_type="agent",
            approver_profile="security",
            status="approved",
        )
        run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")

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

            reset_count = kb._reset_task_approvals_for_task(conn, task_id)

        reset = kb.get_task_approval(conn, approval.id)
        untouched = kb.get_task_approval(conn, other.id)

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
        (["requested"], "approving"),
        (["running"], "approving"),
        (["approved"], "done"),
        (["approved", "approved"], "done"),
        (["approved", "requested"], "approving"),
        (["approved", "running"], "approving"),
        (["requested", "rejected"], "todo"),
        (["running", "rejected"], "approving"),
        (["approved", "rejected"], "todo"),
        (["escalated", "approved"], "done"),
        (["failed", "approved"], "done"),
        (["failed", "requested"], "approving"),
        (["failed", "running"], "approving"),
    ],
)
def test_compute_task_approval_aggregate_status(kanban_home, statuses, expected_status):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="aggregate target")
        approvals = []
        for index, status in enumerate(statuses):
            if index == 0:
                approvals.append(
                    kb.create_task_approval(
                        conn,
                        task_id=task_id,
                        approver_type="human",
                        status=status,
                    )
                )
            else:
                approvals.append(
                    kb.create_task_approval(
                        conn,
                        task_id=task_id,
                        approver_type="agent",
                        approver_profile=f"reviewer-{index}",
                        status=status,
                    )
                )

    assert kb._compute_task_approval_aggregate_status(approvals) == expected_status


def test_apply_task_approval_aggregate_transition_marks_approving_task_done_when_all_gates_are_satisfied(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="aggregate target", assignee="ops")
        agent = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="escalated",
        )
        human = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="human",
            status="approved",
        )
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        with kb.write_txn(conn):
            aggregate_status = kb._apply_task_approval_aggregate_transition(
                conn,
                task_id,
                approvals=[agent, human],
                now=9_000,
            )

        task = kb.get_task(conn, task_id)
        refreshed_agent = kb.get_task_approval(conn, agent.id)
        refreshed_human = kb.get_task_approval(conn, human.id)

    assert aggregate_status == "done"
    assert task is not None
    assert task.status == "done"
    assert refreshed_agent is not None
    assert refreshed_agent.status == "escalated"
    assert refreshed_human is not None
    assert refreshed_human.status == "approved"



def test_apply_task_approval_aggregate_transition_marks_approving_task_done_when_last_gate_is_removed(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="aggregate target", assignee="ops")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        with kb.write_txn(conn):
            aggregate_status = kb._apply_task_approval_aggregate_transition(
                conn,
                task_id,
                approvals=[],
                now=9_000,
            )

        task = kb.get_task(conn, task_id)

    assert aggregate_status == "done"
    assert task is not None
    assert task.status == "done"



def test_record_task_approval_decision_marks_approving_task_done_when_live_gate_is_satisfied(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval_comment_id = kb.add_comment(conn, task_id, "reviewer", "looks good")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

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

        aggregate_status = kb.record_task_approval_decision(
            conn,
            approval_id=approval.id,
            expected_run_id=run.id,
            status="approved",
            comment_id=approval_comment_id,
            now=9_000,
        )

        refreshed = kb.get_task_approval(conn, approval.id)
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



def test_finalize_task_approval_row_if_owned_applies_terminal_status(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval_comment_id = kb.add_comment(conn, task_id, "reviewer", "looks good")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))
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

            finalized = kb._finalize_task_approval_row_if_owned(
                conn,
                approval_id=approval.id,
                expected_run_id=run.id,
                status="approved",
                comment_id=approval_comment_id,
                now=9_000,
            )

        refreshed = kb.get_task_approval(conn, approval.id)
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
    assert task.status == "approving"


def test_finalize_task_approval_row_if_owned_discards_non_running_rows(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))
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

            finalized = kb._finalize_task_approval_row_if_owned(
                conn,
                approval_id=approval.id,
                expected_run_id=run.id,
                status="approved",
                now=9_000,
            )

        refreshed = kb.get_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert finalized is False
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.claim_lock == "lease-1"
    assert refreshed.current_run_id == run.id
    assert refreshed.updated_at == 6_000
    assert task is not None
    assert task.status == "approving"


def test_finalize_task_approval_row_if_owned_discards_run_ownership_mismatches(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        approval = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")
        stale_run = kb.create_task_approval_run(conn, approval_id=approval.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))
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

            finalized = kb._finalize_task_approval_row_if_owned(
                conn,
                approval_id=approval.id,
                expected_run_id=stale_run.id,
                status="rejected",
                now=9_000,
            )

        refreshed = kb.get_task_approval(conn, approval.id)
        task = kb.get_task(conn, task_id)

    assert finalized is False
    assert refreshed is not None
    assert refreshed.status == "running"
    assert refreshed.claim_lock == "lease-1"
    assert refreshed.current_run_id == run.id
    assert refreshed.updated_at == 6_500
    assert task is not None
    assert task.status == "approving"


def test_record_task_approval_decision_keeps_task_approving_while_other_approval_is_running(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval target", assignee="ops")
        rejected_comment_id = kb.add_comment(conn, task_id, "reviewer", "needs revision")
        rejected = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        other = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer-2",
            status="requested",
        )
        rejected_run = kb.create_task_approval_run(conn, approval_id=rejected.id, status="running")
        other_run = kb.create_task_approval_run(conn, approval_id=other.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

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

        aggregate_status = kb.record_task_approval_decision(
            conn,
            approval_id=rejected.id,
            expected_run_id=rejected_run.id,
            status="rejected",
            comment_id=rejected_comment_id,
            now=9_000,
        )

        task = kb.get_task(conn, task_id)
        refreshed_rejected = kb.get_task_approval(conn, rejected.id)
        refreshed_other = kb.get_task_approval(conn, other.id)
        rejected_run_row = conn.execute(
            "SELECT status, ended_at, outcome, comment_id, error FROM task_approval_runs WHERE id = ?",
            (rejected_run.id,),
        ).fetchone()

    assert aggregate_status == "approving"

    assert task is not None
    assert task.status == "approving"

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
        rejected = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer",
            status="requested",
        )
        other = kb.create_task_approval(
            conn,
            task_id=task_id,
            approver_type="agent",
            approver_profile="reviewer-2",
            status="requested",
        )
        rejected_run = kb.create_task_approval_run(conn, approval_id=rejected.id, status="running")
        other_run = kb.create_task_approval_run(conn, approval_id=other.id, status="running")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_approvals SET status = 'running', current_run_id = ?, updated_at = ? WHERE id = ?",
                (rejected_run.id, 7_000, rejected.id),
            )
            conn.execute(
                "UPDATE task_approvals SET status = 'running', current_run_id = ?, updated_at = ? WHERE id = ?",
                (other_run.id, 7_100, other.id),
            )

        first_aggregate_status = kb.record_task_approval_decision(
            conn,
            approval_id=rejected.id,
            expected_run_id=rejected_run.id,
            status="rejected",
            comment_id=rejected_comment_id,
            now=9_000,
        )
        assert first_aggregate_status == "approving"

        final_aggregate_status = kb.record_task_approval_decision(
            conn,
            approval_id=other.id,
            expected_run_id=other_run.id,
            status="approved",
            comment_id=final_comment_id,
            now=9_100,
        )

        task = kb.get_task(conn, task_id)
        refreshed_rejected = kb.get_task_approval(conn, rejected.id)
        refreshed_other = kb.get_task_approval(conn, other.id)

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
