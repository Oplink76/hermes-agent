"""Claude Code CLI profile for MoA-only advisory completions."""

from cli_emulated_routes import CLI_EMULATED_ROUTES
from providers import ProviderProfile, register_provider

register_provider(
    ProviderProfile(
        name="claude-cli",
        display_name="Claude Code CLI (MoA only)",
        description="Local Claude Code CLI advisor/aggregator; not a primary acting model",
        auth_type="external_process",
        api_mode="chat_completions",
        base_url=CLI_EMULATED_ROUTES["claude-cli"],
        supports_health_check=False,
        fallback_models=("default",),
    )
)
