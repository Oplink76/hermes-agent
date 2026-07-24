from __future__ import annotations

from queue import Queue

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class DummyCLI(CLICommandsMixin):
    def __init__(self):
        self._pending_input = Queue()


def test_project_create_slash_queues_agent_prompt(capsys):
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-create Acme Intake --private", "project-create")

    out = capsys.readouterr().out
    assert "Project creation workflow queued" in out
    queued = cli._pending_input.get_nowait()
    assert "/project-create workflow request" in queued
    assert "Acme Intake" in queued
    assert "Do not do project work that is not represented on the Kanban board" in queued


def test_project_import_slash_queues_agent_prompt(capsys):
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-import /tmp/acme --name Acme", "project-import")

    out = capsys.readouterr().out
    assert "Project import workflow queued" in out
    queued = cli._pending_input.get_nowait()
    assert "/project-import workflow request" in queued
    assert "/tmp/acme" in queued
    assert "session 20260702_185339_fa62d5" in queued
    assert "DRY RUN ONLY" in queued


def test_project_workflow_slash_without_args_prints_usage(capsys):
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-import", "project-import")

    out = capsys.readouterr().out
    assert "/project-create" in out
    assert "/project-import" in out
    assert cli._pending_input.empty()
