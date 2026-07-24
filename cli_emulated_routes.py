"""Reserved provider identities for MoA-only CLI completions."""

from typing import Final

CLI_EMULATED_ROUTES: Final[dict[str, str]] = {
    "claude-cli": "cli://claude",
    "codex-cli": "cli://codex",
}
