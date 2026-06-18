"""Focused CLI tests for Kanban approval commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.kanban_approvals as approvals_cli
from hermes_cli import kanban_approvals_db as approvals_db

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


def test_cli_approval_request_list_and_show(kanban_home):
    task_data = json.loads(run_slash("create 'approval target' --assignee worker --json"))
    task_id = task_data["id"]

    human_approval = json.loads(run_slash(f"approval request {task_id} --human --json"))
    agent_approval = json.loads(
        run_slash(f"approval request {task_id} --agent reviewer --skill security-review --json")
    )

    with kb.connect() as conn:
        conn.execute(
            "UPDATE task_approvals SET status = 'approved', updated_at = ? WHERE id = ?",
            (7_050, agent_approval["id"]),
        )

    board_rows = json.loads(run_slash("approval list --flat --json"))
    task_rows = json.loads(run_slash(f"approval list {task_id} --json"))
    approved_rows = json.loads(run_slash("approval list --flat --status approved --all --json"))
    human_rows = json.loads(run_slash("approval list --flat --human --json"))
    shown = run_slash(f"show {task_id}")
    shown_json = json.loads(run_slash(f"show {task_id} --json"))

    assert [row["approval_id"] for row in board_rows] == [human_approval["id"], agent_approval["id"]]
    assert [row["approval_id"] for row in task_rows] == [human_approval["id"], agent_approval["id"]]
    assert [row["approval_id"] for row in approved_rows] == [agent_approval["id"]]
    assert [row["approval_id"] for row in human_rows] == [human_approval["id"]]
    assert "Approvals (2):" in shown
    assert "human" in shown
    assert "agent @reviewer:security-review" in shown
    assert [row["id"] for row in shown_json["approvals"]] == [human_approval["id"], agent_approval["id"]]
    assert shown_json["approval_runs"] == []



def test_cli_approval_ls_groups_by_task_and_filters_parent_status(kanban_home):
    active_task = json.loads(run_slash("create 'active approval target' --assignee worker --json"))
    done_task = json.loads(run_slash("create 'done approval target' --assignee worker --json"))
    archived_task = json.loads(run_slash("create 'archived approval target' --assignee worker --json"))

    active_human = json.loads(run_slash(f"approval request {active_task['id']} --human --json"))
    active_agent = json.loads(
        run_slash(f"approval request {active_task['id']} --agent reviewer --skill security-review --json")
    )
    done_agent = json.loads(run_slash(f"approval request {done_task['id']} --agent reviewer --json"))
    json.loads(run_slash(f"approval request {archived_task['id']} --human --json"))

    with kb.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'approval', priority = 7, created_at = 100 WHERE id = ?",
            (active_task["id"],),
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', priority = 9, created_at = 50 WHERE id = ?",
            (done_task["id"],),
        )
        conn.execute(
            "UPDATE tasks SET status = 'archived', priority = 8, created_at = 75 WHERE id = ?",
            (archived_task["id"],),
        )

    grouped_text = run_slash("approval ls")
    grouped_json = json.loads(run_slash("approval ls --json"))
    all_grouped_json = json.loads(run_slash("approval ls --all --json"))
    active_grouped_json = json.loads(run_slash("approval ls --active --json"))
    task_rows = json.loads(run_slash(f"approval ls {done_task['id']} --json"))
    missing_task = run_slash("approval ls t_missing")

    assert active_task["id"] in grouped_text
    assert done_task["id"] not in grouped_text
    assert archived_task["id"] not in grouped_text
    assert f"{active_task['id'].ljust(12)}{'approval'.ljust(12)}{'agent @worker'.ljust(26)}active approval target" in grouped_text
    assert f"  #{active_human['id']}" in grouped_text
    assert f"agent @reviewer:security-review" in grouped_text

    assert [group["task"]["id"] for group in grouped_json] == [active_task["id"]]
    assert [row["id"] for row in grouped_json[0]["approvals"]] == [active_human["id"], active_agent["id"]]

    assert [group["task"]["id"] for group in all_grouped_json] == [
        done_task["id"],
        archived_task["id"],
        active_task["id"],
    ]
    assert [group["task"]["id"] for group in active_grouped_json] == [active_task["id"]]
    assert [row["approval_id"] for row in task_rows] == [done_agent["id"]]
    assert missing_task == "no such task: t_missing"



def test_cli_approval_ls_flat_filters_and_aliases(kanban_home):
    first_task = json.loads(run_slash("create 'first approval target' --assignee worker --json"))
    second_task = json.loads(run_slash("create 'second approval target' --assignee worker --json"))

    first_human = json.loads(run_slash(f"approval request {first_task['id']} --human --json"))
    second_agent = json.loads(run_slash(f"approval request {second_task['id']} --agent reviewer --skill approver-skill --json"))
    second_human = json.loads(run_slash(f"approval request {second_task['id']} --human --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (first_task["id"],))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (second_task["id"],))
        conn.execute(
            "UPDATE task_approvals SET status = 'approved', updated_at = ? WHERE id = ?",
            (7_050, second_agent["id"]),
        )

    flat_text = run_slash("approval ls --flat")
    task_text = run_slash(f"approval ls {second_task['id']}")
    flat_json = json.loads(run_slash("approval ls --flat --all --json"))
    human_rows = json.loads(run_slash("approval ls --flat --human --all --json"))
    agent_rows = json.loads(run_slash("approval ls --flat --agent --all --json"))
    approved_rows = json.loads(run_slash("approval ls --flat --status approved --all --json"))
    task_rows = json.loads(run_slash(f"approval ls {second_task['id']} --json"))
    conflict = run_slash("approval ls --all --active")

    assert first_task["id"] in flat_text
    assert second_task["id"] not in flat_text
    assert f"#{first_human['id']}" in flat_text
    assert f"{first_task['id'].ljust(12)}{'requested'.ljust(12)}human" in flat_text
    assert second_task["id"] not in task_text
    assert f"  #{second_agent['id']}" in task_text
    assert f"agent @reviewer:approver-skill" in task_text
    assert f"  #{second_human['id']}" in task_text

    assert [row["approval_id"] for row in flat_json] == [first_human["id"], second_agent["id"], second_human["id"]]
    assert [row["approval_id"] for row in human_rows] == [first_human["id"], second_human["id"]]
    assert [row["approval_id"] for row in agent_rows] == [second_agent["id"]]
    assert [row["approval_id"] for row in approved_rows] == [second_agent["id"]]
    assert [row["approval_id"] for row in task_rows] == [second_agent["id"], second_human["id"]]
    assert task_rows[0]["task_id"] == second_task["id"]
    assert conflict.startswith("⚠ /kanban usage error")



def test_cli_show_aligns_approval_rows_with_grouped_ls(kanban_home):
    task = json.loads(run_slash("create 'show approval target' --assignee worker --json"))
    approval_payload = json.loads(
        run_slash(f"approval request {task['id']} --agent reviewer --skill approver-skill --json")
    )

    with kb.connect() as conn:
        comment_id = kb.add_comment(conn, task["id"], "reviewer", "please revise the approval rationale")
        run = approvals_db._create_task_approval_run_for_tests(
            conn,
            approval_id=approval_payload["id"],
            profile="reviewer",
            status="running",
            started_at=120,
            worker_pid=4242,
        )
        conn.execute(
            """
            UPDATE task_approvals
               SET status = 'running',
                   current_run_id = ?,
                   worker_pid = ?,
                   comment_id = ?
             WHERE id = ?
            """,
            (run.id, 4242, comment_id, approval_payload["id"]),
        )
        approval = approvals_db.get_task_approval(conn, approval_payload["id"])
        assert approval is not None
        expected_line = approvals_cli.format_approval_line(approval, include_task_id=False)

    show_text = run_slash(f"show {task['id']}")

    assert "Approvals (1):" in show_text
    assert expected_line in show_text
    assert ":approver-skill" in show_text
    assert "skill=approver-skill" not in show_text
    assert f"[run #{run.id}]" in show_text
    assert "[pid: 4242]" in show_text
    assert f"[comment #{comment_id}]" in show_text



def test_cli_approval_runs(kanban_home, monkeypatch):
    monkeypatch.setattr(approvals_cli.time, "time", lambda: 567)

    task = json.loads(run_slash("create 'approval run target' --assignee worker --json"))
    approval = json.loads(run_slash(f"approval request {task['id']} --agent coder --json"))

    with kb.connect() as conn:
        comment_id = kb.add_comment(
            conn,
            task["id"],
            "coder",
            "Escalated to human after a fairly detailed review comment that should truncate cleanly once it gets past the shared kanban show width and keeps going with extra rationale for the operator to inspect in json mode.",
        )
        failed_run = approvals_db._create_task_approval_run_for_tests(
            conn,
            approval_id=approval["id"],
            profile="default",
            status="failed",
            started_at=700,
            ended_at=1000,
            outcome="failed",
            error="very long failure reason that should be truncated in the text output for readability and operator scanning while still preserving the complete single-line text in json output for downstream tooling and debugging",
        )
        running_run = approvals_db._create_task_approval_run_for_tests(
            conn,
            approval_id=approval["id"],
            profile="coder",
            status="running",
            started_at=500,
            worker_pid=81895,
        )
        escalated_run = approvals_db._create_task_approval_run_for_tests(
            conn,
            approval_id=approval["id"],
            profile="coder",
            status="escalated",
            started_at=100,
            ended_at=223,
            outcome="escalated",
            comment_id=comment_id,
        )

    runs_text = run_slash(f"approval runs {approval['id']}")
    runs_json = json.loads(run_slash(f"approval runs {approval['id']} --json"))
    missing = run_slash("approval runs 99999")

    assert f"#{failed_run.id}" in runs_text
    assert f"#{running_run.id}" in runs_text
    assert f"#{escalated_run.id}" in runs_text
    assert "failed" in runs_text
    assert "300s" in runs_text
    assert "@default" in runs_text
    assert "[pid: 81895]" in runs_text
    assert "[comment #" in runs_text
    assert "    ! very long failure reason" in runs_text
    assert "    → Escalated to human" in runs_text
    assert "json output for downstream tooling and debugging" not in runs_text
    assert "operator to inspect in json mode." not in runs_text

    assert [row["id"] for row in runs_json] == [failed_run.id, running_run.id, escalated_run.id]
    assert runs_json[0]["display_status"] == "failed"
    assert runs_json[0]["elapsed_seconds"] == 300
    assert runs_json[0]["error_preview"].endswith("json output for downstream tooling and debugging")
    assert runs_json[1]["worker_pid"] == 81895
    assert runs_json[1]["elapsed_seconds"] == 67
    assert runs_json[2]["comment_id"] == comment_id
    assert runs_json[2]["comment_body"].startswith("Escalated to human")
    assert runs_json[2]["comment_preview"].endswith("operator to inspect in json mode.")
    assert missing == "unknown approval 99999"



def test_cli_approval_remove_and_reset(kanban_home):
    task_data = json.loads(run_slash("create 'approval target' --assignee worker --json"))
    task_id = task_data["id"]

    removed = json.loads(run_slash(f"approval request {task_id} --agent reviewer --json"))
    survivor = json.loads(run_slash(f"approval request {task_id} --human --json"))

    with kb.connect() as conn:
        removed_run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=removed["id"], status="running")
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
    reset_target = json.loads(run_slash(f"approval request {second_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (second_task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'failed', comment_id = 123, consecutive_failures = 2, last_failure_error = 'boom' WHERE id = ?",
            (reset_target["id"],),
        )

    reset_payload = json.loads(run_slash(f"approval reset {reset_target['id']} --json"))
    shown_after_reset = json.loads(run_slash(f"show {second_task_id} --json"))

    assert reset_payload["approval"]["id"] == reset_target["id"]
    assert reset_payload["task_status"] == "approval"
    assert shown_after_reset["task"]["status"] == "approval"
    assert shown_after_reset["approvals"][0]["status"] == "requested"
    assert shown_after_reset["approvals"][0]["consecutive_failures"] == 0
    assert shown_after_reset["approvals"][0]["last_failure_error"] is None

    requested_payload = json.loads(run_slash(f"approval reset {reset_target['id']} --json"))
    shown_after_requested_reset = json.loads(run_slash(f"show {second_task_id} --json"))

    assert requested_payload["approval"]["id"] == reset_target["id"]
    assert requested_payload["approval"]["status"] == "requested"
    assert requested_payload["task_status"] == "approval"
    assert shown_after_requested_reset["approvals"][0]["status"] == "requested"



def test_cli_approval_reclaim(kanban_home):
    task = json.loads(run_slash("create 'reclaim target' --assignee worker --json"))
    task_id = task["id"]
    approval = json.loads(run_slash(f"approval request {task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        run = approvals_db._create_task_approval_run_for_tests(conn, approval_id=approval["id"], status="running")
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
            ("lease-2", 321, 654, 987, run.id, 7_500, approval["id"]),
        )

    reclaim_payload = json.loads(run_slash(f"approval reclaim {approval['id']} --json"))
    reclaim_show = json.loads(run_slash(f"show {task_id} --json"))

    assert reclaim_payload["approval"]["id"] == approval["id"]
    assert reclaim_payload["approval"]["status"] == "cancelled"
    assert reclaim_payload["task_status"] == "done"
    assert reclaim_show["task"]["status"] == "done"
    assert reclaim_show["approvals"][0]["status"] == "cancelled"
    assert reclaim_show["approval_runs"][0]["status"] == "reclaimed"
    assert reclaim_show["approval_runs"][0]["outcome"] == "reclaimed"



def test_cli_approval_approve_and_reject(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_NAME", "approver")

    approve_task = json.loads(run_slash("create 'approve target' --assignee worker --json"))
    approve_task_id = approve_task["id"]
    approve_gate = json.loads(run_slash(f"approval request {approve_task_id} --human --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (approve_task_id,))

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
    change_human = json.loads(run_slash(f"approval request {change_task_id} --human --json"))
    json.loads(run_slash(f"approval request {change_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (change_task_id,))

    change_approve_payload = json.loads(
        run_slash(f"approval approve {change_human['id']} --comment 'initial yes' --json")
    )
    change_show = json.loads(run_slash(f"show {change_task_id} --json"))

    assert change_approve_payload["approval"]["status"] == "approved"
    assert change_approve_payload["task_status"] == "done"
    assert change_approve_payload["aggregate_status"] == "done"
    assert change_show["task"]["status"] == "done"
    assert change_show["comments"][-1]["body"] == "initial yes"

    reject_task = json.loads(run_slash("create 'reject target' --assignee worker --json"))

    reaffirm_task = json.loads(run_slash("create 'reaffirm target' --assignee worker --json"))
    reaffirm_task_id = reaffirm_task["id"]
    reaffirm_human = json.loads(run_slash(f"approval request {reaffirm_task_id} --human --json"))
    json.loads(run_slash(f"approval request {reaffirm_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (reaffirm_task_id,))
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
    assert reaffirm_payload["task_status"] == "done"
    assert reaffirm_payload["aggregate_status"] == "done"
    assert reaffirm_show["task"]["status"] == "done"
    assert reaffirm_show["approvals"][0]["comment_id"] is not None
    assert reaffirm_show["comments"][-1]["body"] == "still approved"

    reject_task = json.loads(run_slash("create 'reject target' --assignee worker --json"))
    reject_task_id = reject_task["id"]
    reject_human = json.loads(run_slash(f"approval request {reject_task_id} --human --json"))
    reject_agent = json.loads(run_slash(f"approval request {reject_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (reject_task_id,))
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
    reopen_human = json.loads(run_slash(f"approval request {reopen_task_id} --human --json"))
    json.loads(run_slash(f"approval request {reopen_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (reopen_task_id,))
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
    assert reopen_payload["task_status"] == "done"
    assert reopen_payload["aggregate_status"] == "done"
    assert reopen_show["task"]["status"] == "done"
    assert reopen_show["comments"][-1]["body"] == "re-opened and approved"



def test_cli_approval_decisions_accept_task_id_and_create_missing_human_gate(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_NAME", "approver")

    approve_task = json.loads(run_slash("create 'approve via task' --assignee worker --json"))
    approve_task_id = approve_task["id"]
    approve_gate = json.loads(run_slash(f"approval request {approve_task_id} --human --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (approve_task_id,))

    approve_payload = json.loads(
        run_slash(f"approval approve {approve_task_id} --comment 'approved by task' --json")
    )
    approve_show = json.loads(run_slash(f"show {approve_task_id} --json"))

    assert approve_payload["approval"]["id"] == approve_gate["id"]
    assert approve_payload["approval"]["status"] == "approved"
    assert approve_payload["task_status"] == "done"
    assert approve_payload["aggregate_status"] == "done"
    assert approve_show["comments"][-1]["body"] == "approved by task"

    reject_task = json.loads(run_slash("create 'reject via task' --assignee worker --json"))
    reject_task_id = reject_task["id"]
    reject_agent = json.loads(run_slash(f"approval request {reject_task_id} --agent reviewer --json"))

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (reject_task_id,))
        conn.execute(
            "UPDATE task_approvals SET status = 'approved', updated_at = ? WHERE id = ?",
            (7_300, reject_agent["id"]),
        )

    reject_payload = json.loads(
        run_slash(f"approval reject {reject_task_id} --comment 'rejected by task' --json")
    )
    reject_show = json.loads(run_slash(f"show {reject_task_id} --json"))

    assert reject_payload["approval"]["task_id"] == reject_task_id
    assert reject_payload["approval"]["approver_type"] == "human"
    assert reject_payload["approval"]["status"] == "requested"
    assert reject_payload["task_status"] == "todo"
    assert reject_show["task"]["status"] == "todo"
    assert [row["approver_type"] for row in reject_show["approvals"]] == ["agent", "human"]
    assert reject_show["comments"][-1]["body"] == "rejected by task"

    not_ready_task = json.loads(run_slash("create 'not ready' --assignee worker --json"))
    not_ready_output = run_slash(f"approval approve {not_ready_task['id']} --json")
    unknown_task_output = run_slash("approval approve t_missing --json")
    malformed_target_output = run_slash("approval approve nope --json")

    assert not_ready_output == "kanban: parent task must be in approval status"
    assert unknown_task_output == "kanban: unknown task t_missing"
    assert malformed_target_output == "kanban: unknown approval target 'nope'"
