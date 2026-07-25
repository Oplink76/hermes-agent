"""Claude Code CLI profile for primary acting and MoA advisory use."""

from cli_emulated_routes import CLI_EMULATED_ROUTES
from providers import ProviderProfile, register_provider

register_provider(
    ProviderProfile(
        name="claude-cli",
        display_name="Claude Code CLI (local agent)",
        description=(
            "Runs Claude Code's native agent loop in the active project. "
            "Its actions use Claude permissions outside Hermes per-tool approvals. "
            "MoA use remains advisory-safe with tools disabled."
        ),
        auth_type="external_process",
        api_mode="chat_completions",
        base_url=CLI_EMULATED_ROUTES["claude-cli"],
        supports_health_check=False,
        fallback_models=("default",),
    )
)
