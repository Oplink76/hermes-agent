"""The fork leaves Kanban task completion to its role-specific workflow."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import turn_stop_gates


@pytest.mark.parametrize("terminal_tool", [None, "kanban_resolve"])
def test_dispatched_worker_can_finish_without_ordinary_worker_terminal_tool(monkeypatch, terminal_tool):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "fork-stop-regression")
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    monkeypatch.setattr(turn_stop_gates, "_verify_on_stop_nudge", lambda agent: None)
    monkeypatch.setattr(turn_stop_gates, "_pre_verify_nudge", lambda agent, response, attempt: None)
    messages = []
    if terminal_tool:
        messages.append({"role": "assistant", "tool_calls": [{"function": {"name": terminal_tool}}]})
    original_messages = list(messages)
    verdict = turn_stop_gates.apply_stop_gates(
        SimpleNamespace(_interim_content_was_streamed=Mock(return_value=False), _emit_status=Mock()),
        {"role": "assistant", "content": "Finished"},
        final_response="Finished", messages=messages, conversation_history=[],
        pending_verification_response=None, pending_verification_response_previewed=False,
    )
    assert not verdict.continue_turn
    assert verdict.final_response == "Finished"
    assert messages == original_messages
