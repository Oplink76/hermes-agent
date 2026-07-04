from __future__ import annotations

from queue import Queue

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class DummyCLI(CLICommandsMixin):
    def __init__(self):
        self._pending_input = Queue()
        self._pending_agent_seed = None


class FallbackDummyCLI(CLICommandsMixin):
    def __init__(self):
        self._pending_input = Queue()


def test_project_create_slash_sets_agent_seed_and_informative_ack(capsys):
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-create Acme Intake --private", "project-create")

    out = capsys.readouterr().out
    assert "Project creation workflow queued" in out
    assert "Starting now as the next agent turn" in out
    assert "not a background job" in out
    assert "Expected visible steps" in out
    assert "Product Owner interview card" in out
    assert cli._pending_input.empty()
    queued = cli._pending_agent_seed
    assert queued is not None
    assert "/project-create workflow request" in queued
    assert "Acme Intake" in queued
    assert "Do not do project work that is not represented on the Kanban board" in queued
    assert "Product Brief" in queued
    assert "MVP Brief" in queued
    assert "do not create a story card" in queued
    assert "ask breakdown questions" in queued
    assert "branch -> commit -> review -> merge" in queued
    assert "different AI reviewer" in queued
    assert "Hermes must inspect the diff" in queued


def test_project_import_slash_sets_agent_seed_and_informative_ack(capsys):
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-import /tmp/acme --name Acme", "project-import")

    out = capsys.readouterr().out
    assert "Project import workflow queued" in out
    assert "Starting now as the next agent turn" in out
    assert "not a background job" in out
    assert "markdown discovery/reads" in out
    assert "synthesis JSON under /tmp" in out
    assert "dry-run importer output" in out
    assert cli._pending_input.empty()
    queued = cli._pending_agent_seed
    assert queued is not None
    assert "/project-import workflow request" in queued
    assert "/tmp/acme" in queued
    assert "session 20260702_185339_fa62d5" in queued
    assert "DRY RUN ONLY" in queued
    assert "initial import is read-only/dry-run" in queued
    assert "MVP/Product Briefs" in queued
    assert "story_contract" in queued
    assert "broad_product_surface" in queued
    assert "branch -> commit -> review -> merge" in queued
    assert "different AI reviewer" in queued
    assert "Hermes must inspect the diff" in queued


def test_project_workflow_queue_fallback_prefixes_seed_so_it_is_not_slash_redispatched(capsys):
    cli = FallbackDummyCLI()

    cli._handle_project_workflow_command("/project-import /tmp/acme --name Acme", "project-import")

    out = capsys.readouterr().out
    assert "Project import workflow queued" in out
    queued = cli._pending_input.get_nowait()
    assert queued.startswith("Run this project workflow as an agent task:")
    assert not queued.startswith("/project-import")
    assert "/project-import workflow request" in queued


def test_project_workflow_slash_without_args_prints_usage(capsys):
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-import", "project-import")

    out = capsys.readouterr().out
    assert "/project-create" in out
    assert "/project-import" in out
    assert cli._pending_input.empty()
    assert cli._pending_agent_seed is None
