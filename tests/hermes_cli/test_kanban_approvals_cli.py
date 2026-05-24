"""Focused CLI tests for Kanban approval commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_cli_approval_add_list_and_show(kanban_home):
    task_data = json.loads(run_slash("create 'approval target' --assignee worker --json"))
    task_id = task_data["id"]

    human_approval = json.loads(run_slash(f"approval add {task_id} --human --json"))
    agent_approval = json.loads(
        run_slash(f"approval add {task_id} --agent reviewer --skill security-review --json")
    )

    with kb.connect() as conn:
        conn.execute(
            "UPDATE task_approvals SET status = 'approved', updated_at = ? WHERE id = ?",
            (7_050, agent_approval["id"]),
        )

    board_rows = json.loads(run_slash("approval list --json"))
    task_rows = json.loads(run_slash(f"approval list --task {task_id} --json"))
    approved_rows = json.loads(run_slash("approval list --status approved --json"))
    human_rows = json.loads(run_slash("approval list --type human --json"))
    shown = run_slash(f"show {task_id}")
    shown_json = json.loads(run_slash(f"show {task_id} --json"))

    assert [row["id"] for row in board_rows] == [human_approval["id"], agent_approval["id"]]
    assert [row["id"] for row in task_rows] == [human_approval["id"], agent_approval["id"]]
    assert [row["id"] for row in approved_rows] == [agent_approval["id"]]
    assert [row["id"] for row in human_rows] == [human_approval["id"]]
    assert "Approvals (2):" in shown
    assert "human" in shown
    assert "agent @reviewer skill=security-review" in shown
    assert [row["id"] for row in shown_json["approvals"]] == [human_approval["id"], agent_approval["id"]]
    assert "approval_runs" not in shown_json



def test_cli_approval_remove_and_reset(kanban_home):
    task_data = json.loads(run_slash("create 'approval target' --assignee worker --json"))
    task_id = task_data["id"]

    removed = json.loads(run_slash(f"approval add {task_id} --agent reviewer --json"))
    survivor = json.loads(run_slash(f"approval add {task_id} --human --json"))

    with kb.connect() as conn:
        removed_run = kb.create_task_approval_run(conn, approval_id=removed["id"], status="running")
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (task_id,))
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   current_run_id = ?,
                   claim_lock = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (removed_run.id, "lease-1", 6_900, removed["id"]),
        )
        conn.execute(
            "UPDATE task_approvals SET status = 'approved', updated_at = ? WHERE id = ?",
            (7_000, survivor["id"]),
        )

    removed_payload = json.loads(run_slash(f"approval remove {removed['id']} --json"))
    shown_after_remove = json.loads(run_slash(f"show {task_id} --json"))

    assert removed_payload["approval"]["id"] == removed["id"]
    assert removed_payload["task_status"] == "done"
    assert [row["id"] for row in shown_after_remove["approvals"]] == [survivor["id"]]
    assert shown_after_remove["task"]["status"] == "done"
    with kb.connect() as conn:
        removed_run_count = conn.execute(
            "SELECT COUNT(*) FROM task_approval_runs WHERE approval_id = ?",
            (removed["id"],),
        ).fetchone()[0]
    assert removed_run_count == 0

    second_task = json.loads(run_slash("create 'reset target' --assignee worker --json"))
    second_task_id = second_task["id"]
    reset_target = json.loads(run_slash(f"approval add {second_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (second_task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'failed', comment_id = 123, consecutive_failures = 2, last_failure_error = 'boom' WHERE id = ?",
            (reset_target["id"],),
        )

    reset_payload = json.loads(run_slash(f"approval reset {reset_target['id']} --json"))
    shown_after_reset = json.loads(run_slash(f"show {second_task_id} --json"))

    assert reset_payload["approval"]["id"] == reset_target["id"]
    assert reset_payload["task_status"] == "approving"
    assert shown_after_reset["task"]["status"] == "approving"
    assert shown_after_reset["approvals"][0]["status"] == "requested"
    assert shown_after_reset["approvals"][0]["consecutive_failures"] == 0
    assert shown_after_reset["approvals"][0]["last_failure_error"] is None

    requested_payload = json.loads(run_slash(f"approval reset {reset_target['id']} --json"))
    shown_after_requested_reset = json.loads(run_slash(f"show {second_task_id} --json"))

    assert requested_payload["approval"]["id"] == reset_target["id"]
    assert requested_payload["approval"]["status"] == "requested"
    assert requested_payload["task_status"] == "approving"
    assert shown_after_requested_reset["approvals"][0]["status"] == "requested"



def test_cli_approval_approve_and_reject(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_NAME", "approver")

    approve_task = json.loads(run_slash("create 'approve target' --assignee worker --json"))
    approve_task_id = approve_task["id"]
    approve_gate = json.loads(run_slash(f"approval add {approve_task_id} --human --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (approve_task_id,))

    approve_payload = json.loads(
        run_slash(f"approval approve {approve_gate['id']} --comment 'looks good' --json")
    )
    approve_show = json.loads(run_slash(f"show {approve_task_id} --json"))

    assert approve_payload["approval"]["id"] == approve_gate["id"]
    assert approve_payload["approval"]["status"] == "approved"
    assert approve_payload["task_status"] == "done"
    assert approve_payload["aggregate_status"] == "done"
    assert approve_show["task"]["status"] == "done"
    assert approve_show["approvals"][0]["comment_id"] is not None
    assert approve_show["comments"][-1]["author"] == "approver"
    assert approve_show["comments"][-1]["body"] == "looks good"

    change_task = json.loads(run_slash("create 'change target' --assignee worker --json"))
    change_task_id = change_task["id"]
    change_human = json.loads(run_slash(f"approval add {change_task_id} --human --json"))
    json.loads(run_slash(f"approval add {change_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (change_task_id,))

    change_approve_payload = json.loads(
        run_slash(f"approval approve {change_human['id']} --comment 'initial yes' --json")
    )
    change_reject_payload = json.loads(
        run_slash(f"approval reject {change_human['id']} --comment 'actually no' --json")
    )
    change_show = json.loads(run_slash(f"show {change_task_id} --json"))

    assert change_approve_payload["approval"]["status"] == "approved"
    assert change_approve_payload["task_status"] == "approving"
    assert change_approve_payload["aggregate_status"] == "approving"
    assert change_reject_payload["approval"]["id"] == change_human["id"]
    assert change_reject_payload["approval"]["status"] == "requested"
    assert change_reject_payload["task_status"] == "todo"
    assert change_reject_payload["aggregate_status"] == "todo"
    assert change_show["task"]["status"] == "todo"
    assert [row["status"] for row in change_show["approvals"]] == ["requested", "requested"]
    assert [comment["body"] for comment in change_show["comments"][-2:]] == ["initial yes", "actually no"]

    reaffirm_task = json.loads(run_slash("create 'reaffirm target' --assignee worker --json"))
    reaffirm_task_id = reaffirm_task["id"]
    reaffirm_human = json.loads(run_slash(f"approval add {reaffirm_task_id} --human --json"))
    json.loads(run_slash(f"approval add {reaffirm_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (reaffirm_task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'approved', updated_at = ? WHERE id = ?",
            (7_150, reaffirm_human["id"]),
        )

    reaffirm_payload = json.loads(
        run_slash(f"approval approve {reaffirm_human['id']} --comment 'still approved' --json")
    )
    reaffirm_show = json.loads(run_slash(f"show {reaffirm_task_id} --json"))

    assert reaffirm_payload["approval"]["id"] == reaffirm_human["id"]
    assert reaffirm_payload["approval"]["status"] == "approved"
    assert reaffirm_payload["task_status"] == "approving"
    assert reaffirm_payload["aggregate_status"] == "approving"
    assert reaffirm_show["task"]["status"] == "approving"
    assert reaffirm_show["approvals"][0]["comment_id"] is not None
    assert reaffirm_show["comments"][-1]["body"] == "still approved"

    reject_task = json.loads(run_slash("create 'reject target' --assignee worker --json"))
    reject_task_id = reject_task["id"]
    reject_human = json.loads(run_slash(f"approval add {reject_task_id} --human --json"))
    reject_agent = json.loads(run_slash(f"approval add {reject_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (reject_task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'approved', updated_at = ? WHERE id = ?",
            (7_200, reject_agent["id"]),
        )

    reject_payload = json.loads(
        run_slash(f"approval reject {reject_human['id']} --comment 'needs changes' --json")
    )
    reject_show = json.loads(run_slash(f"show {reject_task_id} --json"))

    assert reject_payload["approval"]["id"] == reject_human["id"]
    assert reject_payload["approval"]["status"] == "requested"
    assert reject_payload["approval"]["comment_id"] is None
    assert reject_payload["task_status"] == "todo"
    assert reject_payload["aggregate_status"] == "todo"
    assert reject_show["task"]["status"] == "todo"
    assert [row["status"] for row in reject_show["approvals"]] == ["requested", "requested"]
    assert reject_show["comments"][-1]["author"] == "approver"
    assert reject_show["comments"][-1]["body"] == "needs changes"

    reopen_task = json.loads(run_slash("create 'reopen target' --assignee worker --json"))
    reopen_task_id = reopen_task["id"]
    reopen_human = json.loads(run_slash(f"approval add {reopen_task_id} --human --json"))
    json.loads(run_slash(f"approval add {reopen_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approving' WHERE id = ?", (reopen_task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'rejected', updated_at = ? WHERE id = ?",
            (7_250, reopen_human["id"]),
        )

    reopen_payload = json.loads(
        run_slash(f"approval approve {reopen_human['id']} --comment 're-opened and approved' --json")
    )
    reopen_show = json.loads(run_slash(f"show {reopen_task_id} --json"))

    assert reopen_payload["approval"]["id"] == reopen_human["id"]
    assert reopen_payload["approval"]["status"] == "approved"
    assert reopen_payload["task_status"] == "approving"
    assert reopen_payload["aggregate_status"] == "approving"
    assert reopen_show["task"]["status"] == "approving"
    assert reopen_show["comments"][-1]["body"] == "re-opened and approved"
