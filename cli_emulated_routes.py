"""Reserved Claude/Codex CLI routes shared by primary and MoA execution."""

from typing import Final

CLI_EMULATED_ROUTES: Final[dict[str, str]] = {
    "claude-cli": "cli://claude",
    "codex-cli": "cli://codex",
}
