"""Real AIAgent run-conversation seam for primary local agents."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from run_agent import AIAgent


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli", "cowork"])
def test_local_agent_initialization_bypasses_http_client(provider: str) -> None:
    route = {
        "claude-cli": "cli://claude",
        "codex-cli": "cli://codex",
        "cowork": "cli://cowork",
    }[provider]
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI") as openai,
    ):
        agent = AIAgent(
            provider=provider,
            model="default",
            api_key="local-agent-virtual-provider",
            base_url=route,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    openai.assert_not_called()
    assert agent.client is None
    assert agent.base_url == route


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli", "cowork"])
def test_live_session_switches_to_local_agent_without_http_client(provider: str) -> None:
    route = {
        "claude-cli": "cli://claude",
        "codex-cli": "cli://codex",
        "cowork": "cli://cowork",
    }[provider]
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI") as openai,
    ):
        agent = AIAgent(
            provider="claude-cli",
            model="default",
            api_key="local-agent-virtual-provider",
            base_url="cli://claude",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.switch_model(
            new_model="default",
            new_provider=provider,
            api_key="local-agent-virtual-provider",
            base_url=route,
            api_mode="chat_completions",
        )

    openai.assert_not_called()
    assert agent.provider == provider
    assert agent.model == "default"
    assert agent.base_url == route
    assert agent.client is None


def test_primary_local_turn_runs_before_http_and_finalizes_one_message(
    tmp_path,
) -> None:
    from agent.runtime_cwd import clear_session_cwd, set_session_cwd
    from agent.turn_finalizer import finalize_turn as real_finalize_turn

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI") as openai,
    ):
        agent = AIAgent(
            provider="claude-cli",
            model="default",
            api_key="local-agent-virtual-provider",
            base_url="cli://claude",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._cached_system_prompt = "System contract."
    agent.compression_enabled = False
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return "Native final text"

    def observing_finalize(*args, **kwargs):
        captured["provider_response_before_finalize"] = (
            agent._turn_received_provider_response
        )
        return real_finalize_turn(*args, **kwargs)

    set_session_cwd(str(tmp_path))
    try:
        with (
            patch("agent.local_agent_provider.run_cli_acting", side_effect=fake_run),
            patch(
                "agent.turn_finalizer.finalize_turn",
                side_effect=observing_finalize,
            ),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation(
                "Current request",
                conversation_history=[
                    {"role": "user", "content": "Earlier question"},
                    {"role": "assistant", "content": "Earlier answer"},
                ],
            )
    finally:
        clear_session_cwd()

    openai.assert_not_called()
    assert result["completed"] is True
    assert result["final_response"] == "Native final text"
    assert captured["cwd"] == str(tmp_path)
    assert captured["provider_response_before_finalize"] is True
    assert agent._turn_received_provider_response is False
    assert not hasattr(agent, "_provider_response_received_this_turn")
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert result["messages"][-1]["content"] == "Native final text"
    rendered = captured["messages"]
    assert isinstance(rendered, list)
    assert rendered[0] == {"role": "system", "content": "System contract."}
    assert [message["content"] for message in rendered if message["role"] == "user"] == [
        "Earlier question",
        "Current request",
    ]


def test_primary_local_turn_surfaces_cli_error_without_http_retry() -> None:
    from agent.cli_emulated_provider import CliCapabilityError

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI") as openai,
    ):
        agent = AIAgent(
            provider="codex-cli",
            model="default",
            api_key="local-agent-virtual-provider",
            base_url="cli://codex",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._cached_system_prompt = "System contract."
    agent.compression_enabled = False

    with (
        patch(
            "agent.local_agent_provider.run_cli_acting",
            side_effect=CliCapabilityError("codex-cli command is not installed"),
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("Do the work")

    openai.assert_not_called()
    assert result["completed"] is False
    assert "codex-cli command is not installed" in result["final_response"]
    assert agent._turn_received_provider_response is False
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
    ]
