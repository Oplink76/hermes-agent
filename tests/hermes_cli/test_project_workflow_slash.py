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
    assert "Wayfinder discovery card" in out
    assert cli._pending_input.empty()
    queued = cli._pending_agent_seed
    assert queued is not None
    assert "/project-create workflow request" in queued
    assert "Acme Intake" in queued
    assert "Submit all executable work through qualification intake" in queued
    assert "Do not submit trusted phase" in queued
    assert "Product Brief" in queued
    assert "MVP Brief" in queued
    assert "Product Owner work must run through the `productowner`" in queued
    assert "Hermes profile" in queued
    assert "Assign it to the `productowner` Hermes profile" in queued
    assert "Wayfinder discovery" in queued
    assert "`wayfinder` skill" in queued
    assert "not" in queued
    assert "grill-me" in queued
    assert "grill-with-docs" in queued
    assert "ad-hoc grilling" in queued
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
    assert "Wayfinder product discovery" in out
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
    assert "Product Owner interpretation must run through" in queued
    assert "`productowner` Hermes profile" in queued
    assert "using the `wayfinder` skill" in queued
    assert "grill-me" in queued
    assert "grill-with-docs" in queued
    assert "ad-hoc grilling" in queued
    assert "Wayfinder" in queued
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

def test_project_create_seed_requires_product_v2_board_and_backlog_po_card():
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-create Acme Intake --path /tmp/acme", "project-create")

    queued = cli._pending_agent_seed
    assert queued is not None
    assert "hermes kanban boards create <slug>" in queued
    assert "--preset product" in queued
    assert "product_workflow.handoff_v2 == true" in queued
    assert "expected product columns" in queued
    assert "hermes project create <project-name>" in queued
    assert "--board <slug>" in queued
    assert "Submit the initial Wayfinder discovery request through qualification" in queued
    assert "not through direct task/routing writes" in queued
    assert "signed Work Contract" in queued
    assert "backlog card" in queued
    assert "hermes kanban --board <slug> create" not in queued
    assert ".worktrees/" in queued
    assert "decomposed WORK cards" in queued
    assert "CREATION" in queued
    assert "not COMPLETION" in queued


def test_project_import_seed_stays_dry_run_and_mentions_v2_apply_contract():
    cli = DummyCLI()

    cli._handle_project_workflow_command("/project-import /tmp/acme --name Acme", "project-import")

    queued = cli._pending_agent_seed
    assert queued is not None
    assert "DRY RUN ONLY" in queued
    assert "Do not run --apply" in queued
    assert "--preset product" in queued
    assert "qualification intake with Product Owner evidence" in queued
    assert "must not write workflow/assignee routing directly" in queued
    assert "--workflow-template-id product" not in queued
    assert "--step-key backlog" not in queued
    assert "handoff_v2" in queued
