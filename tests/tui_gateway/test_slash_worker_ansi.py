"""The slash worker feeds desktop chat bubbles, which render plain text — so
any ANSI a worker-routed command emits (e.g. /journey's own Rich Console) must
be stripped from the worker's return value."""

from __future__ import annotations


class _FakeCLI:
    console = None

    def process_command(self, cmd: str) -> None:
        import sys

        sys.stdout.write("\x1b[38;2;1;2;3mcolored\x1b[0m plain")


class _SeedCLI:
    console = None

    def __init__(self):
        self._pending_agent_seed = None

    def process_command(self, cmd: str) -> None:
        self._pending_agent_seed = "/project-import workflow request\n\nrun me"
        print("Project import workflow queued")


def test_run_strips_ansi_from_output():
    from tui_gateway import slash_worker

    out = slash_worker._run(_FakeCLI(), "/anything")

    assert "\x1b[" not in out
    assert out == "colored plain"


def test_run_result_returns_pending_agent_seed_and_clears_it():
    from tui_gateway import slash_worker

    cli = _SeedCLI()

    result = slash_worker._run_result(cli, "/project-import /tmp/acme")

    assert result["output"] == "Project import workflow queued"
    assert result["agent_seed"].startswith("/project-import workflow request")
    assert cli._pending_agent_seed is None
