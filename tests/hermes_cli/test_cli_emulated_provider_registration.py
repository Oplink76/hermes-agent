"""Registration and runtime routing contracts for local agent providers."""

from __future__ import annotations

import pytest

from hermes_cli.auth import resolve_provider
from hermes_cli.runtime_provider import resolve_runtime_provider
from providers import get_provider_profile


@pytest.mark.parametrize(
    ("provider", "route"),
    [
        ("claude-cli", "cli://claude"),
        ("codex-cli", "cli://codex"),
    ],
)
def test_cli_provider_profiles_support_primary_and_moa_runtime(
    provider: str, route: str
) -> None:
    profile = get_provider_profile(provider)

    assert profile is not None
    assert profile.name == provider
    assert profile.auth_type == "external_process"
    assert profile.base_url == route
    assert profile.supports_health_check is False
    assert resolve_provider(provider) == provider

    primary = resolve_runtime_provider(requested=provider)
    assert primary["provider"] == provider
    assert primary["base_url"] == route

    from agent.moa_loop import _slot_runtime

    assert _slot_runtime({"provider": provider, "model": "default"}) == {
        "provider": provider,
        "model": "default",
        "api_mode": "chat_completions",
        "base_url": route,
        "api_key": "",
    }


def test_local_agents_appear_in_primary_model_picker() -> None:
    from hermes_cli.models import CANONICAL_PROVIDERS

    slugs = {entry.slug for entry in CANONICAL_PROVIDERS}
    assert {"claude-cli", "codex-cli", "cowork"} <= slugs


def test_cowork_provider_profile_and_primary_runtime() -> None:
    profile = get_provider_profile("cowork")
    assert profile is not None
    assert profile.auth_type == "external_process"
    assert profile.base_url == "cowork://local"
    runtime = resolve_runtime_provider(requested="cowork")
    assert runtime["provider"] == "cowork"
    assert runtime["base_url"] == "cowork://local"


@pytest.mark.parametrize(
    ("provider", "command"),
    [("claude-cli", "claude"), ("codex-cli", "codex")],
)
def test_cli_local_agent_auth_status_is_no_key_and_executable_backed(
    monkeypatch, provider, command
):
    from hermes_cli.auth import get_auth_status

    monkeypatch.setattr(
        "hermes_cli.auth.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == command else None,
    )
    status = get_auth_status(provider)

    assert status["logged_in"] is True
    assert status["requires_api_key"] is False
    assert status["resolved_command"] == f"/usr/bin/{command}"


def test_cowork_auth_status_requires_exact_normalized_registered_tool(monkeypatch):
    from hermes_cli.auth import get_auth_status

    monkeypatch.setattr(
        "tools.registry.registry.get_definitions",
        lambda names, **_kwargs: (
            [{"function": {"name": "mcp__cowork_mcp__cowork_run"}}]
            if names == {"mcp__cowork_mcp__cowork_run"}
            else []
        ),
    )

    status = get_auth_status("cowork")
    assert status["logged_in"] is True
    assert status["requires_api_key"] is False
    assert status["tool"] == "mcp__cowork_mcp__cowork_run"


def test_existing_claude_code_and_codex_aliases_remain_unchanged() -> None:
    assert resolve_provider("claude-code") == "anthropic"
    assert resolve_provider("codex") == "openai-codex"
    assert resolve_provider("openai-codex") == "openai-codex"


def test_codex_agentic_advisor_acknowledgement_is_valid_provider_config() -> None:
    from hermes_cli.config import validate_config_structure

    issues = validate_config_structure(
        {"providers": {"codex-cli": {"allow_agentic_advisor": True}}}
    )
    assert not any("allow_agentic_advisor" in issue.message for issue in issues)


def test_local_agent_provider_settings_are_valid_config() -> None:
    from hermes_cli.config import validate_config_structure

    issues = validate_config_structure(
        {
            "providers": {
                "claude-cli": {"enabled": True, "timeout": 600},
                "codex-cli": {
                    "enabled": True,
                    "timeout": 900,
                    "allow_agentic_advisor": True,
                },
                "cowork": {"enabled": True, "timeout": 900},
            }
        }
    )
    messages = [issue.message for issue in issues]
    assert not any(
        setting in message
        for setting in ("claude-cli", "codex-cli", "cowork", "timeout")
        for message in messages
    )
