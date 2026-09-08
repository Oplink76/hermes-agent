"""Fork CLI failures must not become repeated HTTP/API work."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_cli_invocation_error_ends_the_first_outer_loop_attempt():
    from agent.cli_emulated_provider import CliInvocationError
    from agent.turn_loop_errors import handle_outer_loop_error

    verdict = handle_outer_loop_error(
        SimpleNamespace(max_iterations=100, suppress_status_output=True),
        e=CliInvocationError("CLI unavailable"), _outer_error_count=0, api_call_count=1,
        messages=[], conversation_history=[], _turn_exit_reason=None, failed=False,
        final_response=None,
    )
    assert verdict.action == "break"
    assert verdict._outer_error_count == 1
    assert verdict._turn_exit_reason.startswith("cli_invocation_error(")
    assert verdict.final_response == "CLI-backed MoA invocation failed: CLI unavailable"


def test_config_resolved_cli_cannot_run_a_non_moa_auxiliary_task(monkeypatch):
    from agent import auxiliary_client

    monkeypatch.setattr(auxiliary_client, "_resolve_task_provider_model", lambda *args: (
        "claude-cli", "default", "cli://claude", "", "chat_completions",
    ))
    http_client = Mock(side_effect=AssertionError("CLI route reached HTTP client construction"))
    monkeypatch.setattr(auxiliary_client, "_resolve_call_client", http_client)
    with pytest.raises(RuntimeError, match="available only to MoA"):
        auxiliary_client.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}])
    http_client.assert_not_called()
