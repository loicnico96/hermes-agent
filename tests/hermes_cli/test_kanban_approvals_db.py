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
            status="approved",
        )

        fetched = kb.get_task_approval(conn, human.id)
        approvals = kb.list_task_approvals(conn, task_id)
        approved = kb.list_task_approvals(conn, task_id, status="approved")
        agents = kb.list_task_approvals(conn, task_id, approver_type="agent")

    assert fetched == human
    assert [approval.id for approval in approvals] == [human.id, agent.id]
    assert [approval.id for approval in approved] == [agent.id]
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
