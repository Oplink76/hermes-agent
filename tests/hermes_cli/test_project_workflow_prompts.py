from __future__ import annotations

from hermes_cli.project_workflows import (
    build_project_create_prompt,
    build_project_import_prompt,
    project_workflow_usage,
)


def assert_engineering_operating_rules(prompt: str):
    assert "Engineering operating rules" in prompt
    assert "branch -> commit -> review -> merge" in prompt
    assert "updated main" in prompt
    assert "dedicated branch" in prompt
    assert "separate git worktree" in prompt
    assert "local coding-agent delegation" in prompt
    assert "Claude Code and Codex" in prompt
    assert "parallel agents" in prompt
    assert "different AI reviewer" in prompt
    assert "reviewer self-reports remain advisory" in prompt
    assert "Hermes must inspect the diff" in prompt
    assert "tests/lints/builds" in prompt
    assert "branch/commit/PR" in prompt
    assert "AGENTS.md" in prompt
    assert "CLAUDE.md" in prompt
    assert "LLM Wiki" in prompt
    assert "active Kanban ticket scope" in prompt
    assert "qualification intake" in prompt
    assert "Hermes qualification path" in prompt
    assert "do not submit trusted phase or assignee routing" in prompt
    assert "Break-glass" in prompt
    assert "direct authenticated instruction to Hermes" in prompt


def test_project_create_prompt_enforces_kanban_traceability_and_clickable_po_questions():
    prompt = build_project_create_prompt("Acme Intake --path ~/work/acme --private")

    assert "/project-create" in prompt
    assert "Acme Intake" in prompt
    assert "create the project folder" in prompt
    assert "create the GitHub repository" in prompt
    assert "create the Hermes Kanban board" in prompt
    assert "create the Hermes Project record" in prompt
    assert "Wayfinder discovery" in prompt
    assert "Product Owner work must run through the `productowner`" in prompt
    assert "`wayfinder` skill" in prompt
    assert "grill-me" in prompt
    assert "grill-with-docs" in prompt
    assert "ad-hoc grilling" in prompt
    assert "Hermes profile" in prompt
    assert "Do not impersonate Product Owner from the default" in prompt
    assert "session" in prompt
    assert "Assign it to the `productowner` Hermes profile" in prompt
    assert "role worker picks it up" in prompt
    assert "unless Ole explicitly overrides that rule" not in prompt
    assert "Traceability log" in prompt
    assert "Kanban comment" in prompt
    assert "created_by" in prompt
    assert "clarify" in prompt
    assert "2-4 clickable choices" in prompt
    assert "Other" in prompt
    assert "Product Brief" in prompt
    assert "MVP Brief" in prompt
    assert "not user-story cards" in prompt
    assert "cockpit/dashboard/control layer" in prompt
    assert "do not create a story card" in prompt
    assert "ask breakdown questions" in prompt
    assert "one When/Then scenario" in prompt
    assert "less than about two days" in prompt
    assert "Product Owner run/artifact evidence" in prompt
    assert_engineering_operating_rules(prompt)


def test_project_import_prompt_matches_verified_ai_synthesis_flow_and_blocks_apply():
    prompt = build_project_import_prompt("/tmp/acme --name 'Acme Intake'")

    assert "/project-import" in prompt
    assert "/tmp/acme" in prompt
    assert "session 20260702_185339_fa62d5" in prompt
    assert "DRY RUN ONLY" in prompt
    assert "Do not run --apply" in prompt
    assert "Product Owner interpretation must run through" in prompt
    assert "`productowner` Hermes profile" in prompt
    assert "using the `wayfinder` skill" in prompt
    assert "grill-with-docs" in prompt
    assert "ad-hoc grilling" in prompt
    assert "do not impersonate PO from the default session" in prompt
    assert "Product Owner" in prompt
    assert "synthesis must be performed by the `productowner` profile using" in prompt
    assert "`wayfinder`" in prompt
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
    assert "MVP/Product Briefs" in prompt
    assert "broad dashboard/cockpit/control-plane surfaces" in prompt
    assert "not valid user stories" in prompt
    assert "ask story-breakdown PO questions" in prompt
    assert "Split before proposing cards" in prompt
    assert "story_contract" in prompt
    assert "not_product_brief" in prompt
    assert "single_when_then" in prompt
    assert "independently_shippable" in prompt
    assert "estimated_effort_days <= 2" in prompt
    assert "broad_product_surface" in prompt
    assert "Traceability log" in prompt
    assert "initial import is read-only/dry-run" in prompt
    assert "accepted stories through qualification intake" in prompt
    assert_engineering_operating_rules(prompt)


def test_project_workflow_usage_lists_both_must_have_commands():
    usage = project_workflow_usage()

    assert "/project-create" in usage
    assert "/project-import" in usage
    assert "folder" in usage
    assert "GitHub repo" in usage
    assert "Kanban board" in usage
    assert "Wayfinder discovery" in usage
