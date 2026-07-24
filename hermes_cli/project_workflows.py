"""Prompt builders for project-create and project-import slash workflows.

These commands intentionally seed the normal agent loop instead of adding new
model tools.  The agent already has the file/terminal/github/kanban/clarify
surfaces it needs; this module supplies a strict process contract so project
work starts from a visible board and every action leaves trace evidence.
"""

from __future__ import annotations

import textwrap


PROJECT_CREATE_USAGE = (
    "/project-create <name> [--path PATH] [--owner GITHUB_OWNER] "
    "[--repo REPO_NAME] [--public|--private]"
)
PROJECT_IMPORT_USAGE = "/project-import <path> [--name PROJECT_NAME] [--dry-run|--apply-after-approval]"


def _clean_args(raw_args: str | None) -> str:
    return (raw_args or "").strip()


def project_workflow_usage() -> str:
    """Return concise usage for the two must-have project workflows."""

    return textwrap.dedent(
        f"""
        Project workflow commands:
        - {PROJECT_CREATE_USAGE}
          Creates the folder, GitHub repo, Kanban board, and PO interview,
          then binds the Hermes Project record to the folder/board.
        - {PROJECT_IMPORT_USAGE}
          Runs the existing-project import flow: full markdown understanding,
          AI synthesis, dry-run importer, and user-story-only Kanban proposal.
        """
    ).strip()


def build_project_create_prompt(raw_args: str | None) -> str:
    """Build the agent seed for ``/project-create``.

    The seed is intentionally explicit: the live agent must use tools to create
    artifacts, ask PO questions through clarify, and record traceability on the
    Kanban board rather than doing invisible side work.
    """

    args = _clean_args(raw_args)
    return textwrap.dedent(
        f"""
        /project-create workflow request

        Raw user arguments:
        {args or "(none provided)"}

        Goal:
        Create a new Hermes-native product project. This command is expected to
        create the project folder, create the GitHub repository, create the Hermes Kanban board,
        create the Hermes Project record, and create the initial Product Owner interview card.

        Hard process rules:
        - Do not do project work that is not represented on the Kanban board,
          unless Ole explicitly overrides that rule for a good reason.
        - If an override is necessary, state the reason before doing the work and
          add it to the Traceability log.
        - Every meaningful action must be traceable: add a Kanban comment or
          event with who did it, what was done, exact command/tool/artifact, and
          why. Use the active Hermes profile/user as `created_by` where the
          Kanban CLI supports it.
        - Do not silently create implementation/development work before the
          Product Owner interview establishes product intent and user stories.
        - Product/project Kanban boards must contain product work as valid user
          stories only. Internal setup/ops notes may be comments/log entries,
          not product-story cards.

        Required flow:
        1. Parse the requested project name/options from the raw arguments. If a
           required value is missing, ask with the `clarify` tool using 2-4 clickable choices
           and an `Other` free-text option.
        2. Derive a safe slug and target folder path. Confirm scope before any
           destructive overwrite. Prefer a new empty folder.
        3. Create the project folder.
        4. Initialize git if needed.
        5. Create the GitHub repository using authenticated GitHub tooling. If
           GitHub auth is missing, block and explain the exact missing auth step;
           do not fake a repo URL.
        6. Create the Hermes Kanban board for the project without switching the
           user's active/default board unless explicitly requested.
        7. Create/bind the Hermes Project record to the folder and board.
        8. Create the initial Product Owner interview card on the project board.
           The card must ask for product intent, target user, problem, outcome,
           constraints, non-goals, and first story candidates.
        9. Add a Traceability log comment to the PO interview card summarizing:
           actor, folder path, GitHub repo, board slug, project slug/id, commands
           run, and any deviations/overrides.
        10. Stop after the PO interview is created unless Ole explicitly asks to
            proceed. Do not start architecture/coding before the interview.

        Product Owner question rule:
        When asking Ole/product-holder questions, use `clarify` with 2-4 clickable choices
        and an `Other` option. Questions must be about product
        intent, user outcomes, scope, personas, or acceptance criteria — not
        generic process trivia.

        Final response:
        Report created artifacts with IDs/paths/URLs, the PO interview task id,
        and the Traceability log entries. If anything was blocked, report the
        blocker and the exact safe next step.
        """
    ).strip()


def build_project_import_prompt(raw_args: str | None) -> str:
    """Build the agent seed for ``/project-import``.

    This mirrors the verified dry-run behavior from session
    20260702_185339_fa62d5: read all markdown, synthesize product intent, write a
    temporary synthesis file, and run the importer dry-run before any apply.
    """

    args = _clean_args(raw_args)
    return textwrap.dedent(
        f"""
        /project-import workflow request

        Raw user arguments:
        {args or "(none provided)"}

        This must follow the corrected import flow tested in session 20260702_185339_fa62d5.

        Hard safety rules:
        - DRY RUN ONLY unless Ole explicitly approves a later live apply.
        - Do not run --apply during this initial import workflow.
        - Do not create a board, project, Kanban cards, or repo docs during the
          dry run.
        - Do not do project work that is not represented on the Kanban board,
          unless Ole explicitly overrides that rule for a good reason.
        - Maintain a Traceability log in the session output: who did what, which
          files were read, which commands/tools ran, artifacts created under
          /tmp, and why each step happened. If live apply is later approved,
          mirror that log into Kanban comments/events using `created_by` where
          available.

        Required import flow:
        1. Parse the project path and optional name from the raw arguments. If a
           required value is missing, ask with `clarify` using 2-4 clickable
           choices and an `Other` option.
        2. Discover every non-excluded markdown file: README, design docs, PRDs,
           architecture notes, roadmaps, TODOs, decisions, AGENTS.md,
           CLAUDE.md, product docs, etc. Exclude .git, node_modules, venv,
           .venv, dist, build, cache/coverage/temp/runtime folders, and binary
           README-prefixed artifacts.
        3. You must read and understand all non-excluded markdown files before asking any
           Product Owner questions or proposing cards.
        4. Produce a Markdown Understanding Ledger listing every markdown file
           path and the product intent/design/constraint/decision/state evidence
           it contributed. Do not just count files.
        5. Analyze code only as code current-state-vs-product-goal evidence:
           implemented user-facing capabilities, tests, partial/stub/TODO
           signals, missing or uncertain capabilities. Do not perform a code
           quality review and do not turn TODOs/stubs into Kanban cards.
        6. Draft a Product Brief: name, personas, problem, intended outcome,
           current state, non-goals, assumptions, evidence.
        7. Ask Product Owner questions only if concrete user stories, product
           outcomes, acceptance criteria, scope, or personas are blocked.
           Questions must use `clarify` with 2-4 clickable choices and an
           `Other` free-text option. Do not ask generic scanner questions.
        8. Propose only valid user-story cards. Each must start with "User
           story:", use a specific persona, include "As a..., I want..., so
           that...", acceptance criteria, out-of-scope notes, and evidence.
           No Import Analysis cards, Product Brief cards, PO Interview cards,
           PO Input Needed cards, raw TODO/FIXME/stub cards, verification
           chores, refactor tasks, or internal Hermes operations.
        9. Write one temporary synthesis JSON file under /tmp containing only:
           `product_brief`, `po_questions`, and `user_stories`.
        10. Run the importer in dry-run mode with --synthesis-file, for example:
            python /Users/cloudadvisor/.hermes/scripts/import_product_board.py \\
              --repo "<PROJECT_PATH>" \\
              --name "<PROJECT_NAME>" \\
              --dry-run \\
              --synthesis-file "/tmp/<SYNTHESIS>.json"
        11. Report whether apply is safe or blocked. Apply is blocked if Product
            Owner questions remain, markdown understanding is incomplete, or no
            valid user-story cards exist.

        Final response:
        Show markdown count, Markdown Understanding Ledger, code
        current-state-vs-goal summary, Product Brief draft, Product Owner
        questions if any, proposed only valid user-story cards if any, importer
        dry-run output, Traceability log, and explicit apply-safe/apply-blocked
        status.
        """
    ).strip()
