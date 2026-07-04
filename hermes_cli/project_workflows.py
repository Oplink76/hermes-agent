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
PROJECT_IMPORT_USAGE = "/project-import <path>|--path PATH [--name PROJECT_NAME] [--dry-run|--apply-after-approval]"


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
        - Role routing: the slash-command receiver may scaffold/orchestrate the
          workflow, but Product Owner work must run through the `productowner`
          Hermes profile. Do not impersonate Product Owner from the default
          session. Product Owner interview, Product Brief/MVP Brief capture,
          story breakdown, story refinement, and story-card creation/review
          should be assigned to or launched as `productowner`; record that actor
          in Kanban traceability where supported.
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
        - Product Brief, MVP Brief, PO Input Needed, Import Analysis, broad
          dashboard/cockpit/control-plane slices, and "first version" briefs are
          not user-story cards. Keep them in the PO interview/product brief
          trace and ask Product Owner breakdown questions instead of creating
          product-story cards.

        Engineering operating rules for any later coding/review:
        - This workflow does not authorize architecture/coding by itself. After
          the PO interview and valid user-story readiness, coding starts only
          when Ole explicitly asks to proceed.
        - All coding work must follow Ole's branch -> commit -> review -> merge
          rule: start from updated main, create a dedicated branch, and prefer a
          separate git worktree for dirty repos or parallel work.
        - Prefer real local coding-agent delegation for implementation, notably
          Claude Code and Codex when available; split bounded work across
          parallel agents when useful. If delegation is impossible or unsafe,
          state why and use the smallest directly verified path.
        - Keep writer and reviewer separate: one coding agent implements, a
          different AI reviewer inspects the actual diff against main, and
          reviewer self-reports remain advisory until Hermes verifies them.
        - Hermes must inspect the diff, run the relevant tests/lints/builds,
          merge only after green verification, and record branch/commit/PR,
          reviewer result, commands, outputs, and blockers in the Kanban
          Traceability log.
        - Agents must follow project memory and repo instructions such as
          AGENTS.md, CLAUDE.md, and LLM Wiki context when available, while
          staying within the active Kanban ticket scope.

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
           Assign it to the `productowner` Hermes profile so the Relay/Hermes
           role worker picks it up. The current/default session should only
           scaffold the card and record traceability, not conduct the PO
           interview itself unless Ole explicitly overrides role routing.
           The card must ask for product intent, target user, problem, outcome,
           constraints, non-goals, and first story candidates. It must also
           instruct the future PO interviewer to produce/record Product Brief or
           MVP Brief material first, then create user-story cards only after each
           candidate passes the story contract: specific persona, real outcome,
           one When/Then scenario, testable Then, user-visible value,
           independently shippable scope, explicit Out of scope, and likely
           less than about two days of work. If a candidate describes a whole
           cockpit/dashboard/control layer, selector plus detail view, end-to-end
           lifecycle, or "first version", do not create a story card; keep it in
           the brief and ask breakdown questions.
        9. Add a Traceability log comment to the PO interview card summarizing:
           actor, folder path, GitHub repo, board slug, project slug/id, commands
           run, and any deviations/overrides.
        10. Stop after the PO interview is created unless Ole explicitly asks to
            proceed. Do not start architecture/coding before the interview.
            Later PO interview output may be only a product brief; that is valid
            progress and must not be forced into a broad user-story card.

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
        - Role routing: the slash-command receiver may orchestrate discovery and
          dry-run mechanics, but Product Owner interpretation must run through
          the `productowner` Hermes profile. Product Brief/MVP Brief synthesis,
          PO questions, story breakdown, story refinement, and valid story-card
          proposal/application should be assigned to or launched as
          `productowner`; do not impersonate PO from the default session.
        - Do not do project work that is not represented on the Kanban board,
          unless Ole explicitly overrides that rule for a good reason.
        - Maintain a Traceability log in the session output: who did what, which
          files were read, which commands/tools ran, artifacts created under
          /tmp, and why each step happened. If live apply is later approved,
          mirror that log into Kanban comments/events using `created_by` where
          available.

        Engineering operating rules for any later coding/review:
        - The initial import is read-only/dry-run and does not authorize code
          edits. If Ole later approves implementation of an imported story,
          all coding work must follow Ole's branch -> commit -> review -> merge
          rule: start from updated main, create a dedicated branch, and prefer a
          separate git worktree for dirty repos or parallel work.
        - Prefer real local coding-agent delegation for implementation, notably
          Claude Code and Codex when available; split bounded work across
          parallel agents when useful. If delegation is impossible or unsafe,
          state why and use the smallest directly verified path.
        - Keep writer and reviewer separate: one coding agent implements, a
          different AI reviewer inspects the actual diff against main, and
          reviewer self-reports remain advisory until Hermes verifies them.
        - Hermes must inspect the diff, run the relevant tests/lints/builds,
          merge only after green verification, and record branch/commit/PR,
          reviewer result, commands, outputs, and blockers in the Kanban
          Traceability log.
        - Agents must follow project memory and repo instructions such as
          AGENTS.md, CLAUDE.md, and LLM Wiki context when available, while
          staying within the active Kanban ticket scope.

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
           current state, non-goals, assumptions, evidence. This Product Owner
           synthesis must be performed by the `productowner` profile or handed
           off to a `productowner` Kanban/import task when the workflow creates
           durable board state.
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
           MVP/Product Briefs, broad dashboard/cockpit/control-plane surfaces,
           "first version" slices, selector-plus-detail surfaces, or end-to-end
           lifecycle descriptions are not valid user stories. Keep those in
           `product_brief` and ask story-breakdown PO questions instead.
           Split before proposing cards when there are multiple personas,
           multiple When/Then pairs, broad product-surface scope, or more than
           about two days of likely work. Prefer vertical slices by workflow
           step, business-rule variation, data variation, acceptance-criteria
           complexity, external dependency, ops/deploy step, or spike.
           Every entry in `user_stories` must include a `story_contract` object
           proving: `not_product_brief`, `single_when_then`,
           `independently_shippable`, `user_visible_value`, and
           `estimated_effort_days <= 2`; set `broad_product_surface` to false.
        9. Write one temporary synthesis JSON file under /tmp containing only:
           `product_brief`, `po_questions`, and `user_stories`.
        10. Run the importer in dry-run mode with --synthesis-file, for example:
            python /Users/cloudadvisor/.hermes/scripts/import_product_board.py \
              --repo "<PROJECT_PATH>" \
              --name "<PROJECT_NAME>" \
              --dry-run \
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
