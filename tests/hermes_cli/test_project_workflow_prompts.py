from __future__ import annotations

from hermes_cli.project_workflows import (
    build_project_create_prompt,
    build_project_import_prompt,
    project_workflow_usage,
)


def test_project_create_prompt_enforces_kanban_traceability_and_clickable_po_questions():
    prompt = build_project_create_prompt("Acme Intake --path ~/work/acme --private")

    assert "/project-create" in prompt
    assert "Acme Intake" in prompt
    assert "create the project folder" in prompt
    assert "create the GitHub repository" in prompt
    assert "create the Hermes Kanban board" in prompt
    assert "create the Hermes Project record" in prompt
    assert "Product Owner interview" in prompt
    assert "Do not do project work that is not represented on the Kanban board" in prompt
    assert "unless Ole explicitly overrides that rule" in prompt
    assert "good reason" in prompt
    assert "Traceability log" in prompt
    assert "Kanban comment" in prompt
    assert "created_by" in prompt
    assert "clarify" in prompt
    assert "2-4 clickable choices" in prompt
    assert "Other" in prompt


def test_project_import_prompt_matches_verified_ai_synthesis_flow_and_blocks_apply():
    prompt = build_project_import_prompt("/tmp/acme --name 'Acme Intake'")

    assert "/project-import" in prompt
    assert "/tmp/acme" in prompt
    assert "session 20260702_185339_fa62d5" in prompt
    assert "DRY RUN ONLY" in prompt
    assert "Do not run --apply" in prompt
    assert "Markdown Understanding Ledger" in prompt
    assert "read and understand all non-excluded markdown files" in prompt
    assert "code current-state-vs-product-goal" in prompt
    assert "temporary synthesis JSON" in prompt
    assert "--synthesis-file" in prompt
    assert "Product Owner questions" in prompt
    assert "clarify" in prompt
    assert "2-4 clickable choices" in prompt
    assert "Other" in prompt
    assert "only valid user-story cards" in prompt
    assert "No Import Analysis cards" in prompt
    assert "Traceability log" in prompt


def test_project_workflow_usage_lists_both_must_have_commands():
    usage = project_workflow_usage()

    assert "/project-create" in usage
    assert "/project-import" in usage
    assert "folder" in usage
    assert "GitHub repo" in usage
    assert "Kanban board" in usage
    assert "PO interview" in usage
