"""Cowork primary provider backed by the configured generic MCP registry."""

from providers import ProviderProfile, register_provider

register_provider(
    ProviderProfile(
        name="cowork",
        display_name="Cowork (local agent)",
        description=(
            "Runs the configured Cowork MCP agent and installed skills in the "
            "active project. Native actions are outside Hermes per-tool approvals. "
            "Cowork is not available in Mixture of Agents."
        ),
        auth_type="external_process",
        api_mode="chat_completions",
        base_url="cowork://local",
        supports_health_check=False,
        fallback_models=("default",),
    )
)
