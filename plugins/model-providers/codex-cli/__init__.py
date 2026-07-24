"""Codex CLI profile for explicitly opted-in MoA advisory completions."""

from cli_emulated_routes import CLI_EMULATED_ROUTES
from providers import ProviderProfile, register_provider

register_provider(
    ProviderProfile(
        name="codex-cli",
        display_name="Codex CLI (MoA only)",
        description=(
            "Local read-only Codex agent advisor/aggregator; requires explicit opt-in "
            "and is not a primary acting model"
        ),
        auth_type="external_process",
        api_mode="chat_completions",
        base_url=CLI_EMULATED_ROUTES["codex-cli"],
        supports_health_check=False,
        fallback_models=("default",),
    )
)
