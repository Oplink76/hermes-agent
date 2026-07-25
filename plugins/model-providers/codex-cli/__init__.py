"""Codex CLI profile for primary acting and explicitly opted-in MoA use."""

from cli_emulated_routes import CLI_EMULATED_ROUTES
from providers import ProviderProfile, register_provider

register_provider(
    ProviderProfile(
        name="codex-cli",
        display_name="Codex CLI (local agent)",
        description=(
            "Runs Codex's native workspace-write agent loop in the active project. "
            "Its actions use the Codex sandbox outside Hermes per-tool approvals. "
            "MoA use remains read-only and requires explicit agentic consent."
        ),
        auth_type="external_process",
        api_mode="chat_completions",
        base_url=CLI_EMULATED_ROUTES["codex-cli"],
        supports_health_check=False,
        fallback_models=("default",),
    )
)
