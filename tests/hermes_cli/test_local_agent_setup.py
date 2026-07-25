"""No-key local-agent setup persistence contracts."""

from __future__ import annotations

import pytest

from hermes_cli.model_setup_flows import _model_flow_local_agent_provider


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli", "cowork"])
def test_local_agent_setup_preserves_provider_settings(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    cfg = {
        "model": {
            "default": "old",
            "provider": "openai-api",
            "api_key": "stale",
            "base_url": "https://example.test/v1",
            "api_mode": "chat_completions",
        },
        "providers": {
            provider: {
                "enabled": True,
                "timeout": 42,
                "allow_agentic_advisor": True,
            }
        },
    }
    saved: list[dict] = []
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda value: saved.append(value))
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda _model: None)
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)

    _model_flow_local_agent_provider(cfg, provider)

    assert saved[-1]["model"] == {"default": "default", "provider": provider}
    assert saved[-1]["providers"][provider] == {
        "enabled": True,
        "timeout": 42,
        "allow_agentic_advisor": True,
    }
