from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _process_pending_qualification_intakes,
    _qualify_then_dispatch,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake


class _Kb:
    @staticmethod
    def list_qualification_intakes(conn, *, status=None):
        assert status == "pending"
        return list(conn.pending)

    @staticmethod
    def dispatch_once(conn, *, board, **kwargs):
        conn.events.append(("dispatch", board))
        return SimpleNamespace(spawned=[])

    @staticmethod
    def product_board_metadata(board):
        return {}

    @staticmethod
    def _handoff_v2_enabled(metadata):
        return False


class _Conn:
    def __init__(self, count=0):
        self.pending = [{"id": f"qi_{index:016x}"} for index in range(count)]
        self.events = []


def test_pending_intake_is_qualified_before_dispatch_and_work_is_bounded():
    conn = _Conn(count=5)

    def qualify(_conn, *, board, intake_id):
        conn.events.append(("qualify", intake_id))
        return {"status": "qualified", "intake_id": intake_id}

    _qualify_then_dispatch(
        _Kb,
        conn,
        "strict",
        qualification_per_tick=2,
        qualify=qualify,
    )

    assert conn.events == [
        ("qualify", "qi_0000000000000000"),
        ("qualify", "qi_0000000000000001"),
        ("dispatch", "strict"),
    ]


def test_next_sweep_recovers_pending_intake_after_infrastructure_failure():
    conn = _Conn(count=1)
    attempts = 0

    def qualify(_conn, *, board, intake_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("provider unavailable")
        conn.pending.clear()
        return {"status": "qualified", "intake_id": intake_id, "task_id": "t_1"}

    first = _process_pending_qualification_intakes(
        _Kb, conn, board="strict", per_tick=3, qualify=qualify
    )
    second = _process_pending_qualification_intakes(
        _Kb, conn, board="strict", per_tick=3, qualify=qualify
    )

    assert first == {"attempted": 1, "qualified": 0, "rejected": 0, "failed": 1}
    assert second == {"attempted": 1, "qualified": 1, "rejected": 0, "failed": 0}
    assert attempts == 2


def test_rejected_intake_is_counted_without_human_notification_side_effects():
    conn = _Conn(count=1)

    def qualify(_conn, *, board, intake_id):
        return {"status": "rejected", "intake_id": intake_id}

    result = _process_pending_qualification_intakes(
        _Kb, conn, board="strict", per_tick=3, qualify=qualify
    )

    assert result == {"attempted": 1, "qualified": 0, "rejected": 1, "failed": 0}
    assert conn.events == []


def test_new_intake_emits_process_local_wake_after_durable_write(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    observed = []

    def wake():
        observed.append(kb.list_qualification_intakes(conn, status="pending"))

    kanban_intake._register_intake_waker(wake)
    try:
        receipt = kanban_intake.submit_intake(
            conn,
            request={"title": "Wake qualification"},
            source="gateway",
        )
    finally:
        kanban_intake._unregister_intake_waker(wake)
        conn.close()

    assert observed[0][0]["id"] == receipt["intake_id"]


class _OverrideRunner(GatewayKanbanWatchersMixin):
    pass


@pytest.mark.asyncio
async def test_override_requires_authenticated_home_channel_direct_message(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_qualifier as qualifier

    runner = _OverrideRunner()
    platform = SimpleNamespace(value="telegram")
    runner.config = SimpleNamespace(
        get_home_channel=lambda selected: SimpleNamespace(
            chat_id="ole-home", thread_id=None
        )
    )
    runner.session_store = SimpleNamespace(
        peek_session_id=lambda key: "session-1"
    )
    event = SimpleNamespace(
        text="Override qi_0123456789abcdef because Ole approved recovery",
        internal=False,
        message_id="message-7",
        source=SimpleNamespace(
            platform=platform,
            chat_id="someone-else",
            chat_type="dm",
            thread_id=None,
            message_id="message-7",
        ),
    )
    called = False

    def should_not_run(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(qualifier, "override_intake", should_not_run)

    assert await runner._kanban_override_instruction(event, "session-key") is None
    assert called is False


@pytest.mark.asyncio
async def test_authenticated_ole_gateway_instruction_reaches_private_override(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_qualifier as qualifier

    runner = _OverrideRunner()
    platform = SimpleNamespace(value="telegram")
    runner.config = SimpleNamespace(
        get_home_channel=lambda selected: SimpleNamespace(
            chat_id="ole-home", thread_id=None
        )
    )
    runner.session_store = SimpleNamespace(
        peek_session_id=lambda key: "session-1"
    )
    event = SimpleNamespace(
        text="Override qi_0123456789abcdef because Ole approved recovery",
        internal=False,
        message_id="message-7",
        source=SimpleNamespace(
            platform=platform,
            chat_id="ole-home",
            chat_type="dm",
            thread_id=None,
            message_id="message-7",
        ),
    )
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(kb, "list_boards", lambda include_archived: [{"slug": "strict"}])
    monkeypatch.setattr(kb, "connect", lambda board: conn)
    monkeypatch.setattr(
        kb,
        "get_qualification_intake",
        lambda selected, intake_id: {"id": intake_id, "status": "pending"},
    )
    captured = {}

    def authority(**kwargs):
        captured.update(kwargs)
        return "authority"

    monkeypatch.setattr(qualifier, "_new_gateway_override_authority", authority)
    monkeypatch.setattr(
        qualifier,
        "override_intake",
        lambda selected, **kwargs: {
            "status": "overridden",
            "intake_id": kwargs["intake_id"],
            "task_id": "t_governed",
        },
    )

    result = await runner._kanban_override_instruction(event, "session-key")

    assert result == (
        "Override applied to qi_0123456789abcdef. "
        "Governed task t_governed was materialized."
    )
    assert captured["reason"] == "Ole approved recovery"
    assert captured["source_session"] == "session-1"
    assert captured["instruction_ref"] == "message-7"
