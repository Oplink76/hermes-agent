# Plan: Make `/project-create` and `/project-import` use Kanban V2/handoff product workflow

Date: 2026-07-08
Scope: Hermes Agent repo `/Users/cloudadvisor/.hermes/hermes-agent`
Mode: inspection + plan only. No implementation in this pass.

## Review addendum — Claude, 2026-07-08 (two REQUIRED additions)

Direction approved. This plan correctly fixes the creation-time gaps — especially the
`handoff_v2` opt-in default (Phase 1/2) and the defensive product default (Phase 6),
which is the crux fix (it kills the root cause of the legacy/plain-card mess seen on the
live `agentic-os-cockpit` board on 2026-07-08). Two additions are REQUIRED, both learned
from that live cleanup:

### Addition A — Worktree isolation for work cards + `.worktrees/` gitignore (HIGH priority)

Phase 2 step 4 (line ~119) bakes `--workspace dir:<project-folder>` into card creation.
A shared `dir` workspace is the EXACT shared-checkout pattern that made concurrent cards
collide on the cockpit board — dirty-tree `transient` blocks, cross-contaminated commits,
hours of thrash. The engine already auto-isolates a project-linked `scratch` card to a
per-card worktree (`kanban_db.py`: `if workspace_kind == "scratch" and
project_obj.primary_path: workspace_kind = "worktree"`), but an explicit `--workspace dir:`
BYPASSES that.

Required:
1. The single backlog PO-interview card MAY stay `dir` (no concurrency at that stage).
2. Decomposed / child WORK cards on a product board MUST NOT default to a shared
   `--workspace dir:<folder>`. Prefer `scratch` (so the project link auto-promotes to a
   per-card worktree) or set `workspace_kind='worktree'` explicitly. Fold this into
   Phase 5 (create surfaces) and Phase 6 (project-bound defaults): a project-bound
   product WORK card should default to worktree isolation, not shared dir.
3. `ensure_product_board_defaults(...)` (Phase 1) or the create flow (Phase 2) MUST ensure
   the project repo's `.gitignore` contains `.worktrees/`. Its absence broke the
   epic-merge clean-check on the cockpit repo. Add a Phase 7 test for it.

### Addition B — Scope caveat: this fixes CREATION, not COMPLETION (context, not code)

This plan makes new projects START on v2 correctly; it does NOT address the "back half"
(commit / handoff / terminalize / merge / integrate) — where the 2026-07-08 cockpit work
actually got stuck. OUT OF SCOPE here but track separately:
- **Integration-back:** per-card `dev/<task>` branches never merge back to feature/main
  (no epic ⇒ `integrate_story_to_epic` never fires); completed work strands on isolated
  branches.
- **Worker instability:** reviewer/tester workers crash (exit 1) / hit iteration-budget
  timeouts under load, tripping the failure-breaker.
- Already fixed (do not re-open): the evidence-only handoff commit-gate bug (`c9a413fa5`,
  live on `hermes-integration`).

Net: clean creation via this plan ≠ clean end-to-end completion until the back half is done.

## Objective

New projects created or imported through Hermes project workflows must start on the Kanban V2 product engine instead of creating plain/generic boards or cards.

Target invariant:

1. Every product project gets a product board, not a generic board.
2. Every project-created/imported user-story or PO interview card is explicitly associated with the product workflow:
   - `workflow_template_id = "product"`
   - `current_step_key = "backlog"` initially, unless a later approved flow intentionally sets another product step.
3. The Hermes Project record is bound to that board.
4. Project-linked task creation does not silently fall back to the default/current board or plain cards.
5. Desktop/TUI slash execution, CLI prompt seeding, dashboard API, and kanban tool creation all preserve the same product-workflow contract.

## Current-state findings

### Existing V2 engine pieces are present

- `hermes_cli/kanban_db.py` defines product board columns and transitions:
  - `PRODUCT_BOARD_COLUMNS`: `backlog -> architecture -> development -> test -> review -> release_measure -> done`, plus `blocked`.
  - `BOARD_PRESETS` includes `product`.
  - `PRODUCT_WORKFLOW_TRANSITIONS` routes product steps and roles.
- `hermes kanban boards create --preset product` exists in `hermes_cli/kanban.py`.
- `hermes kanban create --workflow-template-id ... --step-key ...` exists in `hermes_cli/kanban.py` and `kanban_db.create_task(...)` accepts `workflow_template_id` and `current_step_key`.
- Runtime lifecycle paths are V2-aware:
  - `claim_task(...)` applies V2 running/blocked flags on handoff_v2 product boards.
  - `complete_task(...)` routes non-terminal product steps through `handoff(...)` and terminal steps through done.

### `/project-import` is closer to correct already

- `/project-import` seeds an agent prompt via `hermes_cli/project_workflows.py`.
- The helper script `/Users/cloudadvisor/.hermes/scripts/import_product_board.py` already builds an apply command plan with:
  - `hermes kanban boards create <slug> --preset product --default-workdir <repo>`
  - `hermes project create <name> --slug <slug> --primary <repo> --board <slug> --use`
  - `hermes kanban --board <slug> create ... --workflow-template-id product --step-key backlog --json`
- `test_import_product_board.py` already asserts `--preset product` and `--workflow-template-id product --step-key backlog` for importer command plans.

### `/project-create` has the main contract gap

- `/project-create` is not a direct implementation; it builds an agent seed in `hermes_cli/project_workflows.py`.
- The current create prompt says to create a Hermes Kanban board and initial PO interview card, but does **not** explicitly require:
  - `hermes kanban boards create ... --preset product`
  - product workflow metadata on the board, including `product_workflow.handoff_v2 = true` if needed by the V2 runtime
  - `hermes kanban --board <slug> create ... --workflow-template-id product --step-key backlog`
  - verification that the Project record is bound to a product-preset board
- Because the actual work is done by a normal agent turn, the prompt must be exact enough that a generic agent cannot create plain/generic cards by accident.

### Project binding exists but does not yet enforce workflow governance

- `hermes project create --board <slug>` records `board_slug` in `projects.db`.
- `projects_cmd._sync_board_default_workdir(...)` best-effort syncs the bound board default workdir to the project primary path.
- Current `projects_db.py` inspected in this branch does **not** expose governance helpers such as:
  - `is_kanban_governed(project)`
  - `governance_for_path(conn, path)`
- `kanban_db.create_task(...)` only stores workflow metadata when the caller passes it. Project linkage alone does not guarantee product workflow metadata.

### Dashboard/API/tool surfaces have drift risk

- Dashboard API `POST /tasks` currently accepts normal task fields but not workflow fields in `CreateTaskBody`; workflow fields exist on `PATCH /tasks/{id}` only.
- Dashboard `PATCH` can set workflow fields after creation, but that is a two-step path and can leave cards temporarily/plainly created.
- `tools/kanban_tools.py::kanban_create` schema/handler currently has no `workflow_template_id` or `current_step_key` fields, so agent/tool-created child cards cannot explicitly preserve product workflow metadata.

## Proposed implementation plan

### Phase 1 — Define one canonical project-product defaults helper

Add one small shared helper in `kanban_db.py` or a new focused module, e.g.:

- `product_workflow_defaults_for_board(board) -> dict`
- `ensure_product_board_defaults(slug, *, name, default_workdir, switch=False) -> metadata`

Responsibilities:

1. Normalize and validate board slug.
2. Create/write metadata with:
   - `preset: "product"`
   - `columns: PRODUCT_BOARD_COLUMNS`
   - `default_workdir: <project primary path>` when known
   - `product_workflow.handoff_v2: true`
   - default role assignees, either from current constants/config or explicit board metadata
3. Return an idempotent metadata shape that tests can assert.

Reason: prevent `/project-create`, importer, future Cockpit UI, and tests from copy/pasting subtly different product-board metadata.

### Phase 2 — Strengthen `/project-create` seed contract

Update `build_project_create_prompt(...)` in `hermes_cli/project_workflows.py` so the required flow says, explicitly:

1. Create the board with:
   ```bash
   hermes kanban boards create <slug> \
     --name <project-name> \
     --description "Hermes-native product board" \
     --default-workdir <project-folder> \
     --preset product
   ```
2. Ensure/verify V2 handoff metadata exists on the board:
   - `preset == "product"`
   - `product_workflow.handoff_v2 == true`
   - expected product columns exist.
3. Create the Project record with:
   ```bash
   hermes project create <project-name> \
     --slug <slug> \
     --primary <project-folder> \
     --board <slug> \
     --use
   ```
4. Create the PO interview card with:
   ```bash
   hermes kanban --board <slug> create "Product Owner interview: <project-name>" \
     --project <slug> \
     --workspace dir:<project-folder> \
     --assignee productowner \
     --workflow-template-id product \
     --step-key backlog \
     --json
   ```
   > ⚠️ **See Review addendum A:** `--workspace dir:<project-folder>` is acceptable for
   > this single PO/backlog card, but decomposed WORK cards MUST NOT reuse the shared
   > `dir` workspace — they need per-card worktree isolation (`scratch` → auto-worktree,
   > or explicit `workspace_kind='worktree'`), plus `.worktrees/` in the repo `.gitignore`.
5. Verify and report that the created card has:
   - `project_id` set
   - `workflow_template_id == "product"`
   - `current_step_key == "backlog"`
   - board is the project board, not default/current board.

Keep the existing process constraints: PO interview only, no coding/architecture until Ole explicitly approves.

### Phase 3 — Make `/project-import` use the same helper/contract

The external importer already emits the correct command shape. Still align it with the canonical helper by:

1. Keeping its safe default: dry-run unless explicitly approved.
2. Ensuring its board creation includes V2 handoff metadata, not only `--preset product`.
3. Keeping task creation as user-story-only cards with:
   - `--workflow-template-id product`
   - `--step-key backlog`
   - `--board <slug>` before `create`
4. Add a verification summary after apply that reads the board/project/task rows and reports counts:
   - number of imported cards
   - number missing `workflow_template_id='product'`
   - number missing `current_step_key`
   - project `board_slug` match.

### Phase 4 — Add governance helpers in `projects_db.py`

Add read-only helpers so other subsystems can infer product governance from path/project/board binding:

- `is_kanban_governed(project) -> bool`
  - true when project has a non-empty `board_slug`.
- `governance_for_path(conn, path) -> dict | None`
  - resolve `project_for_path(...)`
  - return project id/slug/primary path/board slug
  - include whether the board is product-preset if cheaply checkable without circular imports.

Use this to support future automatic defaults without duplicating project lookup code in random task-creation paths.

### Phase 5 — Make task creation surfaces able to carry workflow metadata

Unify CLI/tool/API surfaces so V2 metadata can be supplied at creation time everywhere.

1. `tools/kanban_tools.py`
   - Add schema fields:
     - `workflow_template_id`
     - `current_step_key` or `step_key`
   - Pass both into `kb.create_task(...)`.
   - When creating a child from a product-workflow parent on the same product board, default missing values to product/backlog or inherit deliberately according to the chosen product policy.

2. `plugins/kanban/dashboard/plugin_api.py`
   - Add `workflow_template_id` and `current_step_key` to `CreateTaskBody`.
   - Pass `board=board`, `workflow_template_id=...`, and `current_step_key=...` directly into `kanban_db.create_task(...)`.
   - Also pass `board=board` to lifecycle calls like `complete_task(...)` in update/bulk paths where missing, so board-scoped V2 policy never falls back to current/default board.

3. CLI already has the relevant flags, but verify the dispatch path passes:
   - `workflow_template_id=args.workflow_template_id`
   - `current_step_key=args.current_step_key`
   - `board=args.board`

### Phase 6 — Add guardrails so project-bound product tasks cannot silently become plain cards

Implement a small defensive default in `kanban_db.create_task(...)`:

- If `project_id` resolves to a Project with a product board and caller did not provide workflow metadata, set:
  - `workflow_template_id='product'`
  - `current_step_key='backlog'`
  - and use/verify the bound board context where the caller provided `board`.

Important: do this carefully to avoid surprising legacy/default boards:

- Only auto-default when:
  - the task is linked to a project with a board slug, and
  - that board metadata has `preset == 'product'` and handoff_v2 enabled, and
  - caller did not explicitly provide non-product workflow metadata.
- Emit an audit event such as `workflow_defaulted` or include fields in the `created` event payload.
- Do not mutate tasks on generic/default boards.

### Phase 7 — Tests

Add or update tests in the Hermes repo:

1. `tests/hermes_cli/test_project_workflow_slash.py`
   - `/project-create` seeded prompt includes:
     - `--preset product`
     - `--workflow-template-id product`
     - `--step-key backlog`
     - `--board <slug>` guidance
     - handoff_v2 verification language
   - `/project-import` seeded prompt still says dry-run only and no apply.

2. `tests/hermes_cli/test_projects_db.py`
   - `is_kanban_governed(...)` true for project with `board_slug`.
   - `governance_for_path(...)` resolves longest-prefix project and returns bound board.
   - archived projects are skipped by default.

3. `tests/hermes_cli/test_kanban_db.py`
   - product board task created with explicit metadata lands in backlog.
   - project-bound task on a product board defaults missing metadata to product/backlog.
   - generic board task remains plain when metadata omitted.
   - non-product explicit metadata is not overwritten.
   - handoff_v2 board keeps canonical state invariants after claim/complete.

4. `tests/plugins/test_kanban_dashboard_plugin.py`
   - `POST /tasks?board=<product>` accepts workflow fields and stores them at creation.
   - dashboard create does not require a later PATCH to become a product card.
   - lifecycle update calls use the selected `board` context.

5. `tests/tools/test_kanban_tools.py` or equivalent
   - `kanban_create` schema exposes workflow fields.
   - handler passes them into `create_task`.
   - child card from product board can preserve product metadata.

6. `/Users/cloudadvisor/.hermes/scripts/test_import_product_board.py`
   - Keep existing assertions for `--preset product` and `--workflow-template-id product --step-key backlog`.
   - Add assertion/check for handoff_v2 metadata once the canonical helper/command exists.

### Phase 8 — Verification commands before PR

Run at minimum:

```bash
cd /Users/cloudadvisor/.hermes/hermes-agent
python -m py_compile hermes_cli/project_workflows.py hermes_cli/projects_db.py hermes_cli/kanban_db.py hermes_cli/kanban.py tools/kanban_tools.py plugins/kanban/dashboard/plugin_api.py
pytest -q \
  tests/hermes_cli/test_project_workflow_slash.py \
  tests/hermes_cli/test_projects_db.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/tools/test_kanban_tools.py
python /Users/cloudadvisor/.hermes/scripts/test_import_product_board.py
```

Then run the broader Kanban/project slices if touched areas are broad:

```bash
pytest -q tests/hermes_cli/test_kanban_cli.py tests/hermes_cli/test_kanban_project_link.py tests/hermes_cli/test_project_workflow_prompts.py
```

### Phase 9 — Rollout

1. Keep compatibility: existing generic boards/tasks must remain unchanged.
2. Add a migration/diagnostic command later, not in the first patch, to report project-bound product boards with legacy/plain cards.
3. Do not auto-migrate old cards silently in the initial PR.
4. After merge, verify with two manual smokes:
   - `/project-create Example Product --path /tmp/example-product` in a temp repo/profile, then inspect board metadata and the PO card row.
   - `/project-import /tmp/existing-product --name Existing Product` dry-run, then approved apply in an isolated temp profile, then inspect board/project/task rows.

## Acceptance criteria

A future implementation is complete only when:

- `/project-create` creates a product-preset board and a V2 backlog PO interview card.
- `/project-import` dry-run remains safe and live apply creates only V2 backlog user-story cards.
- Project record `board_slug` is set and verified.
- CLI, dashboard API, and `kanban_create` can create workflow-tagged cards in one step.
- Project-bound product board tasks cannot accidentally become plain/default-board cards.
- Tests prove generic boards retain old behavior.
- No source changes are merged without green targeted tests and a clean diff/secret scan.
