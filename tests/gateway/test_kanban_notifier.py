import asyncio
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_approvals_db as approvals_db
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=[
                "completed",
                "blocked",
                "gave_up",
                "crashed",
                "timed_out",
                "awaiting_approval",
                "approval_decided",
                "approval_failed",
                "approval_cancelled",
            ],
        )
        return events
    finally:
        conn.close()


def _create_approval_subscription(*, title="approval task", assignee="worker"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee=assignee)
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        return tid
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()

def test_notifier_formats_awaiting_human_approval(tmp_path, monkeypatch):
    db_path = tmp_path / "awaiting-human.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Needs human eyes")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        approval = approvals_db.create_task_approval(conn, task_id=tid, approver_type="human")
        kb._append_event(
            conn,
            tid,
            kind="awaiting_approval",
            payload={"task_status": "approval", "summary": "Worker asked for a quick human pass.\nExtra detail ignored."},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [d["text"] for d in adapter.sent] == [
        f"🛑 [default] @worker Kanban {tid} awaiting human approval (#{approval.id}) — Needs human eyes\n"
        "Worker asked for a quick human pass."
    ]


def test_notifier_skips_awaiting_approval_if_task_is_no_longer_in_approval(tmp_path, monkeypatch):
    db_path = tmp_path / "awaiting-stale-status.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Late processed human approval")

    conn = kb.connect()
    try:
        approval = approvals_db.create_task_approval(conn, task_id=tid, approver_type="human")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        conn.execute("UPDATE task_approvals SET status = 'approved' WHERE id = ?", (approval.id,))
        kb._append_event(conn, tid, kind="awaiting_approval", payload={"task_status": "approval"})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []


def test_notifier_skips_awaiting_approval_if_no_requested_approvals_remain(tmp_path, monkeypatch):
    db_path = tmp_path / "awaiting-no-requested.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Late processed stale approvals")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        approval = approvals_db.create_task_approval(
            conn,
            task_id=tid,
            approver_type="agent",
            approver_profile="coder",
        )
        conn.execute("UPDATE task_approvals SET status = 'cancelled' WHERE id = ?", (approval.id,))
        kb._append_event(conn, tid, kind="awaiting_approval", payload={"task_status": "approval"})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []


def test_notifier_formats_awaiting_agent_approval(tmp_path, monkeypatch):
    db_path = tmp_path / "awaiting-agent.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Needs agent eyes")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        approval_1 = approvals_db.create_task_approval(
            conn,
            task_id=tid,
            approver_type="agent",
            approver_profile="coder",
        )
        approval_2 = approvals_db.create_task_approval(
            conn,
            task_id=tid,
            approver_type="agent",
            approver_profile="default",
        )
        kb._append_event(
            conn,
            tid,
            kind="awaiting_approval",
            payload={"task_status": "approval", "summary": "Worker queued both agent reviewers.\nExtra detail ignored."},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [d["text"] for d in adapter.sent] == [
        f"⚖️ [default] @worker Kanban {tid} awaiting agent approval (#{approval_1.id}, #{approval_2.id}) — Needs agent eyes\n"
        "Worker queued both agent reviewers."
    ]


def test_notifier_formats_approval_decided_with_comment_and_transition(tmp_path, monkeypatch):
    db_path = tmp_path / "approval-decided.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Approval outcome")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        comment_id = kb.add_comment(conn, tid, "coder", "Looks good from agent review.\nExtra detail ignored.")
        kb._append_event(
            conn,
            tid,
            kind="approval_decided",
            payload={
                "approval_id": 7,
                "approver_type": "agent",
                "approver_profile": "coder",
                "decision": "approved",
                "comment_id": comment_id,
                "next_status": "done",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [d["text"] for d in adapter.sent] == [
        f"✅ [default] @coder Kanban {tid} approved (#7) — Approval outcome; task moved to done\n"
        "Looks good from agent review."
    ]


def test_notifier_uses_blue_checkmark_when_approval_does_not_move_task_to_done(tmp_path, monkeypatch):
    db_path = tmp_path / "approval-decided-nonterminal.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Approval stays in review loop")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        comment_id = kb.add_comment(conn, tid, "reviewer", "Approved, but another approval is still needed.")
        kb._append_event(
            conn,
            tid,
            kind="approval_decided",
            payload={
                "approval_id": 9,
                "approver_type": "human",
                "decision": "approved",
                "comment_id": comment_id,
                "next_status": None,
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [d["text"] for d in adapter.sent] == [
        f"☑️ [default] Kanban {tid} approved (#9) — Approval stays in review loop\n"
        "Approved, but another approval is still needed."
    ]


def test_notifier_formats_human_escalation_with_requested_human_id(tmp_path, monkeypatch):
    db_path = tmp_path / "approval-escalated.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Escalated approval")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        human_approval = approvals_db.create_task_approval(conn, task_id=tid, approver_type="human")
        kb._append_event(
            conn,
            tid,
            kind="approval_decided",
            payload={
                "approval_id": 1,
                "approver_type": "agent",
                "approver_profile": "default",
                "decision": "escalated",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [d["text"] for d in adapter.sent] == [
        f"🛑 [default] @default Kanban {tid} requested human approval (#1 -> #{human_approval.id}) — Escalated approval"
    ]


def test_notifier_formats_human_approved_with_green_check(tmp_path, monkeypatch):
    db_path = tmp_path / "approval-decided-human.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Human approval outcome")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        comment_id = kb.add_comment(conn, tid, "reviewer", "Human approved after a quick pass.")
        kb._append_event(
            conn,
            tid,
            kind="approval_decided",
            payload={
                "approval_id": 8,
                "approver_type": "human",
                "decision": "approved",
                "comment_id": comment_id,
                "next_status": "done",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [d["text"] for d in adapter.sent] == [
        f"✅ [default] Kanban {tid} approved (#8) — Human approval outcome; task moved to done\n"
        "Human approved after a quick pass."
    ]


def test_notifier_formats_approval_failed_and_cancelled(tmp_path, monkeypatch):
    db_path = tmp_path / "approval-failed-cancelled.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_approval_subscription(title="Approval failure lane")

    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET status = 'approval' WHERE id = ?", (tid,))
        kb._append_event(
            conn,
            tid,
            kind="approval_failed",
            payload={
                "approval_id": 11,
                "approver_type": "agent",
                "approver_profile": "coder",
                "outcome": "spawn_failed",
                "error": "boom",
                "failures": 1,
                "status": "requested",
                "failure_limit": 3,
            },
        )
        kb._append_event(
            conn,
            tid,
            kind="approval_cancelled",
            payload={
                "approval_id": 12,
                "approver_type": "agent",
                "approver_profile": "coder",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [d["text"] for d in adapter.sent] == [
        f"✖ [default] @coder Kanban {tid} approval failed (#11) — Approval failure lane",
        f"⏹ [default] @coder Kanban {tid} approval cancelled (#12) — Approval failure lane",
    ]
