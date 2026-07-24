"""Registration and runtime routing contracts for MoA-only CLI providers."""

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
def test_cli_provider_profiles_are_static_but_primary_runtime_is_rejected(
    provider: str, route: str
) -> None:
    profile = get_provider_profile(provider)

    assert profile is not None
    assert profile.name == provider
    assert profile.auth_type == "external_process"
    assert profile.base_url == route
    assert profile.supports_health_check is False
    assert resolve_provider(provider) == provider

    with pytest.raises(ValueError, match="MoA-only"):
        resolve_runtime_provider(requested=provider)

    from agent.moa_loop import _slot_runtime

    assert _slot_runtime({"provider": provider, "model": "default"}) == {
        "provider": provider,
        "model": "default",
        "api_mode": "chat_completions",
        "base_url": route,
        "api_key": "",
    }


def test_cli_providers_do_not_appear_in_primary_model_picker() -> None:
    from hermes_cli.models import CANONICAL_PROVIDERS

    slugs = {entry.slug for entry in CANONICAL_PROVIDERS}
    assert "claude-cli" not in slugs
    assert "codex-cli" not in slugs


def test_existing_claude_code_and_codex_aliases_remain_unchanged() -> None:
    assert resolve_provider("claude-code") == "anthropic"
    assert resolve_provider("codex") == "openai-codex"


def test_codex_agentic_advisor_acknowledgement_is_valid_provider_config() -> None:
    from hermes_cli.config import validate_config_structure

    issues = validate_config_structure(
        {"providers": {"codex-cli": {"allow_agentic_advisor": True}}}
    )
    assert not any("allow_agentic_advisor" in issue.message for issue in issues)
