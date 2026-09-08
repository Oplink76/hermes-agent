"""Tool schemas for tools.kanban_tools (model-facing; strings are byte-frozen)."""
from __future__ import annotations

from typing import Any

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _prop(type_: str, description: str) -> dict[str, str]:
    return {"type": type_, "description": description}


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter (one place to tweak)."""
    return _prop("string", _DESC_BOARD)


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build a tool schema; every kanban tool takes an optional trailing ``board``."""
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {**properties, "board": _board_schema_prop()},
            "required": required,
        },
    }


KANBAN_SHOW_SCHEMA = _schema(
    "kanban_show",
    (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
    },
    [],
)

KANBAN_LIST_SCHEMA = _schema(
    "kanban_list",
    (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    {
        "assignee": _prop("string", "Optional assignee/profile filter."),
        "status": {
            "type": "string",
            "enum": [
                "triage", "todo", "ready", "running",
                "blocked", "done", "archived",
            ],
            "description": "Optional task status filter.",
        },
        "tenant": _prop("string", "Optional tenant/project namespace filter."),
        "include_archived": _prop("boolean", "Include archived tasks. Defaults to false."),
        "limit": _prop("integer", "Optional maximum rows to return (default 50, max 200)."),
    },
    [],
)

KANBAN_COMPLETE_SCHEMA = {'name': 'kanban_complete',
 'description': 'Mark your current task done with a structured handoff for downstream workers and '
                'humans. Prefer ``summary`` for a human-readable 1-3 sentence description of what you '
                'did; put machine-readable facts in ``metadata`` (changed_files, tests_run, decisions, '
                'findings, etc). At least one of ``summary`` or ``result`` is required. If you created '
                'new tasks via ``kanban_create`` during this run, list their ids in ``created_cards`` — '
                'the kernel verifies them so phantom references are caught before they leak into '
                'downstream automation. If you produced deliverable files (charts, PDFs, spreadsheets, '
                'generated images), list their absolute paths in ``artifacts`` — the gateway notifier '
                'will upload them as native attachments to the human who subscribed to the task, so the '
                'deliverable lands in their chat alongside the summary instead of being a path they '
                'have to fetch by hand.',
 'parameters': {'type': 'object',
                'properties': {'task_id': {'type': 'string',
                                           'description': 'Task id. If omitted, defaults to '
                                                          'HERMES_KANBAN_TASK from the env (the task '
                                                          'the dispatcher spawned you to work on).'},
                               'summary': {'type': 'string',
                                           'description': 'Human-readable handoff, 1-3 sentences. '
                                                          'Appears in Run History on the dashboard and '
                                                          "in downstream workers' context."},
                               'metadata': {'type': 'object',
                                            'description': 'Free-form dict of structured facts about '
                                                           'this attempt — {"changed_files": [...], '
                                                           '"tests_run": 12, "findings": [...]}. '
                                                           'Surfaced to downstream workers alongside '
                                                           '``summary``. On product boards, '
                                                           'Development/Test/Review completions must '
                                                           'include ``ai_provenance``: Development '
                                                           'needs {"writer": {"agent": "claude-code"}}; '
                                                           'Test needs {"tester": {"agent": "hermes", '
                                                           '"result": "passed"}}; Review needs '
                                                           '{"reviewer": {"agent": "codex"}, "writer": '
                                                           '{"agent": "claude-code"}} and reviewer must '
                                                           'differ from writer. Include '
                                                           'branch/worktree/commit when available.'},
                               'result': {'type': 'string',
                                          'description': 'Short result log line (legacy field, maps to '
                                                         'task.result). Use ``summary`` instead when '
                                                         'possible; this exists for compatibility with '
                                                         'callers that still set --result on the CLI.'},
                               'created_cards': {'type': 'array',
                                                 'items': {'type': 'string'},
                                                 'description': 'Optional structured manifest of task '
                                                                'ids you created via ``kanban_create`` '
                                                                'during this run. The kernel verifies '
                                                                'each id exists and was created by this '
                                                                "worker's profile; any phantom id "
                                                                'blocks the completion with an error '
                                                                'listing what went wrong (auditable in '
                                                                "the task's events). Only list ids you "
                                                                'got back from a successful '
                                                                '``kanban_create`` call — do not invent '
                                                                'or remember ids from prose. Omit the '
                                                                'field if you did not create any '
                                                                'cards.'},
                               'artifacts': {'type': 'array',
                                             'items': {'type': 'string'},
                                             'description': 'Optional list of absolute paths to '
                                                            'deliverable files you produced during this '
                                                            'run — generated charts, PDFs, '
                                                            'spreadsheets, images, archives. Examples: '
                                                            '["/tmp/q3-revenue.png", '
                                                            '"/tmp/report.pdf"]. The gateway notifier '
                                                            'uploads each path as a native attachment '
                                                            'to the subscribed chat (images embed '
                                                            'inline, everything else uploads as a file) '
                                                            'so the deliverable lands with the '
                                                            'completion notification. Skip intermediate '
                                                            'scratch files and references that are not '
                                                            'the deliverable. The path must exist on '
                                                            'disk at completion. Files inside a managed '
                                                            'scratch workspace are copied to durable '
                                                            'task attachments before cleanup; a missing '
                                                            'declared scratch artifact keeps the task '
                                                            'in-flight so you can fix the path and '
                                                            'retry.'},
                               'board': {'type': 'string',
                                         'description': 'Kanban board slug to target. When omitted, the '
                                                        'call resolves the active board the usual way: '
                                                        'HERMES_KANBAN_DB env → HERMES_KANBAN_BOARD env '
                                                        "→ the 'current' symlink under the kanban home "
                                                        "→ 'default'. Pass an explicit slug only when "
                                                        'the caller (e.g. a Telegram routing layer) '
                                                        'needs to override the env-pinned active board '
                                                        'for this one call.'},
                               'workflow_outcome': {'type': 'object',
                                                    'description': 'Structured product test/review '
                                                                   'outcome. Rejections require '
                                                                   'verdict, target_step, and non-empty '
                                                                   'findings.',
                                                    'properties': {'verdict': {'type': 'string',
                                                                               'enum': ['passed',
                                                                                        'approved',
                                                                                        'changes_requested',
                                                                                        'architecture_invalid']},
                                                                   'target_step': {'type': 'string',
                                                                                   'enum': ['architecture',
                                                                                            'development']},
                                                                   'findings': {'type': 'array',
                                                                                'items': {'type': 'string'}}},
                                                    'required': ['verdict'],
                                                    'additionalProperties': False,
                                                    'allOf': [{'if': {'properties': {'verdict': {'enum': ['changes_requested',
                                                                                                          'architecture_invalid']}}},
                                                               'then': {'required': ['target_step',
                                                                                     'findings']}},
                                                              {'if': {'properties': {'verdict': {'enum': ['passed',
                                                                                                          'approved']}}},
                                                               'then': {'not': {'anyOf': [{'required': ['target_step']},
                                                                                          {'required': ['findings']}]}}}]}},
                'required': []}}

KANBAN_BLOCK_SCHEMA = {'name': 'kanban_block',
 'description': "Stop work on this task and route it according to WHY you're stuck. Set ``kind`` to say "
                "which: 'dependency' (waiting on another task — goes to todo and auto-resumes when that "
                "task finishes, no human needed), 'needs_input' (you need a human decision/answer), "
                "'capability' (a hard wall: no access, missing credentials, an action no agent can do), "
                "or 'transient' (a flaky failure that may clear). For product-board human/capability "
                'blocks, you must include ``attempted_resolutions`` describing the concrete '
                'alternatives you already tried; Hermes will take the first resolution pass before any '
                'Slack human escalation. ``reason`` is shown to the human on the board. If a task keeps '
                'getting unblocked and re-blocked for the same reason, it is auto-escalated to triage. '
                "Use for genuine blockers only — don't block on things you can resolve yourself.",
 'parameters': {'type': 'object',
                'properties': {'task_id': {'type': 'string',
                                           'description': 'Task id. If omitted, defaults to '
                                                          'HERMES_KANBAN_TASK from the env (the task '
                                                          'the dispatcher spawned you to work on).'},
                               'reason': {'type': 'string',
                                          'description': 'What you need answered or what stopped you, '
                                                         "in one or two sentences. Don't paste the "
                                                         'whole conversation; the human has the board '
                                                         'and can ask follow-ups via comments.'},
                               'kind': {'type': 'string',
                                        'enum': ['dependency', 'needs_input', 'capability', 'transient'],
                                        'description': "Why you're blocked. 'dependency' waits in todo "
                                                       'and resumes automatically; the others surface '
                                                       'to a human. Omit only if none apply.'},
                               'board': {'type': 'string',
                                         'description': 'Kanban board slug to target. When omitted, the '
                                                        'call resolves the active board the usual way: '
                                                        'HERMES_KANBAN_DB env → HERMES_KANBAN_BOARD env '
                                                        "→ the 'current' symlink under the kanban home "
                                                        "→ 'default'. Pass an explicit slug only when "
                                                        'the caller (e.g. a Telegram routing layer) '
                                                        'needs to override the env-pinned active board '
                                                        'for this one call.'},
                               'attempted_resolutions': {'type': 'array',
                                                         'items': {'type': 'string'},
                                                         'description': 'For product-board '
                                                                        'human-in-the-loop blocks: the '
                                                                        'concrete things you already '
                                                                        'tried before asking for help. '
                                                                        'Required for '
                                                                        'needs_input/capability/legacy '
                                                                        'human blocks on product '
                                                                        'boards. Examples: checked '
                                                                        'docs, searched repo, tried '
                                                                        'fallback API, asked another '
                                                                        'agent via comment.'}},
                'required': ['reason'],
                'additionalProperties': False}}

KANBAN_REQUEST_REVIEW_SCHEMA = _schema(
    "kanban_request_review",
    (
        "Hand the task off for review: implementation, self-review, and "
        "verification are complete and you want a human (or reviewer) to "
        "look before it is marked done. Moves the task to the 'review' "
        "column and notifies the subscriber. Unlike ``kanban_block`` this is "
        "NOT a blocker — it never counts toward unblock-loop detection, so a "
        "task can cycle through review across follow-ups without ever being "
        "falsely escalated to triage. Use this instead of blocking with a "
        "free-form 'review-required:' reason."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "summary": _prop("string", (
                "What was implemented and how it was verified, in one or "
                "two sentences — shown to the reviewer. Don't paste "
                "the whole diff; the reviewer has the board and the PR."
        )),
        "reviewer": _prop("string", (
                "Optional reviewer profile. When provided, the task is "
                "reassigned to that profile before review dispatch."
        )),
        "metadata": {
            "type": "object",
            "description": (
                "Optional structured handoff facts for the reviewer, such "
                "as changed_files, tests_run, commit, or decisions."
            ),
            "additionalProperties": True,
        },
    },
    ["summary"],
)

KANBAN_REQUEST_CHANGES_SCHEMA = _schema(
    "kanban_request_changes",
    (
        "Reviewer verdict: return the current review run to the original "
        "implementer with concrete required changes. This closes the review "
        "run, reapplies parent dependency gating, and requeues the task without "
        "using block-loop accounting. Only use from a task claimed from the "
        "review column; use kanban_block only for a genuine external blocker."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "reason": _prop("string", (
                "Specific, actionable changes the implementer must make "
                "before requesting another review."
        )),
    },
    ["reason"],
)

KANBAN_HEARTBEAT_SCHEMA = _schema(
    "kanban_heartbeat",
    (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "note": _prop("string", (
                "Optional short note describing current progress. "
                "Shown in the event log."
        )),
    },
    [],
)

KANBAN_COMMENT_SCHEMA = _schema(
    "kanban_comment",
    (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    {
        "task_id": _prop("string", (
                "Task id. Required (may be your own task or "
                "another's — comment threads are per-task)."
        )),
        "body": _prop("string", "Markdown-supported comment body."),
    },
    ["task_id", "body"],
)

KANBAN_ATTACH_SCHEMA = _schema(
    "kanban_attach",
    (
        "Attach a file to a task by passing its bytes inline (base64). "
        "Use for genuine file artifacts the next worker or a human should "
        "be able to download — generated reports, images, exports. The "
        "file is stored as a real attachment (not a comment link) under "
        "the task's attachments dir, capped at 25 MB. Prefer "
        "kanban_attach_url when you only have a URL."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "filename": _prop("string", (
                "File name to store it under (e.g. 'report.pdf'). "
                "Directory components are stripped; only the leaf is kept."
        )),
        "content_base64": {
            "type": "string",
            "description": "The file contents, base64-encoded. Max 25 MB decoded.",
        },
        "content_type": _prop("string", "Optional MIME type (e.g. 'application/pdf')."),
    },
    ["filename", "content_base64"],
)

KANBAN_ATTACH_URL_SCHEMA = _schema(
    "kanban_attach_url",
    (
        "Attach a file to a task by URL — Hermes downloads it server-side "
        "and stores it as a real attachment (capped at 25 MB). Use when "
        "you have a link rather than the bytes. Only http/https URLs are "
        "accepted."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "url": _prop("string", "http(s) URL to fetch and store."),
        "filename": _prop("string", (
                "Optional name to store it under. Defaults to the URL "
                "path's leaf component."
        )),
        "content_type": _prop("string", (
                "Optional MIME type override. Defaults to the "
                "Content-Type the server returns."
        )),
    },
    ["url"],
)

KANBAN_ATTACHMENTS_SCHEMA = _schema(
    "kanban_attachments",
    (
        "List the files attached to a task: id, filename, content_type, "
        "size, who uploaded it, and the absolute on-disk path you can read."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
    },
    [],
)

KANBAN_CREATE_SCHEMA = {'name': 'kanban_create',
 'description': 'Create a new kanban task, optionally as a child of the current one (pass the current '
                'task id in ``parents``). Used by orchestrator workers to fan out — decompose work into '
                'child tasks with specific assignees, link them into a pipeline, then complete your own '
                'task. The dispatcher picks up the new tasks on its next tick and spawns the assigned '
                'profiles.',
 'parameters': {'type': 'object',
                'properties': {'title': {'type': 'string',
                                         'description': 'Short task title (required).'},
                               'assignee': {'type': 'string',
                                            'description': 'Profile name that should execute this task '
                                                           "(e.g. 'researcher-a', 'reviewer', "
                                                           "'writer'). Required — tasks without an "
                                                           'assignee are never dispatched.'},
                               'body': {'type': 'string',
                                        'description': 'Opening post: full spec, acceptance criteria, '
                                                       'links. The assigned worker reads this as part '
                                                       'of its context.'},
                               'parents': {'type': 'array',
                                           'items': {'type': 'string'},
                                           'description': 'Parent task ids. The new task stays in '
                                                          "'todo' until every parent reaches 'done'; "
                                                          "then it auto-promotes to 'ready'. Typical "
                                                          'fan-in: list all the researcher task ids '
                                                          'when creating a synthesizer task.'},
                               'tenant': {'type': 'string',
                                          'description': 'Optional namespace for multi-project '
                                                         'isolation. Defaults to HERMES_TENANT env if '
                                                         'set.'},
                               'priority': {'type': 'integer',
                                            'description': 'Dispatcher tiebreaker. Higher = picked '
                                                           'sooner when multiple ready tasks share an '
                                                           'assignee.'},
                               'workspace_kind': {'type': 'string',
                                                  'enum': ['scratch', 'dir', 'worktree'],
                                                  'description': "Workspace flavor: 'scratch' (fresh "
                                                                 "tmp dir, default), 'dir' (shared "
                                                                 'directory, requires absolute '
                                                                 "workspace_path), 'worktree' (git "
                                                                 'worktree).'},
                               'workspace_path': {'type': 'string',
                                                  'description': "Absolute path for 'dir' or 'worktree' "
                                                                 'workspace. Relative paths are '
                                                                 'rejected at dispatch.'},
                               'project': {'type': 'string',
                                           'description': 'Optional project id or slug to link the task '
                                                          'to. When set, the task becomes a git '
                                                          "worktree under the project's primary repo "
                                                          'with a deterministic branch (project slug + '
                                                          'task id), instead of a random branch.'},
                               'triage': {'type': 'boolean',
                                          'description': "If true, task lands in 'triage' instead of "
                                                         "'todo' — a specifier profile is expected to "
                                                         'flesh out the body before work starts.'},
                               'idempotency_key': {'type': 'string',
                                                   'description': 'If a non-archived task with this key '
                                                                  "already exists, return that task's "
                                                                  'id instead of creating a duplicate. '
                                                                  'Useful for retry-safe automation.'},
                               'max_runtime_seconds': {'type': 'integer',
                                                       'description': 'Per-task runtime cap. When '
                                                                      'exceeded, the dispatcher '
                                                                      'SIGTERMs the worker and '
                                                                      're-queues the task with '
                                                                      "outcome='timed_out'."},
                               'initial_status': {'type': 'string',
                                                  'enum': ['running', 'blocked'],
                                                  'description': "Initial card status. Use 'blocked' "
                                                                 'for tasks that require immediate '
                                                                 'human ops (R3 gate) to skip the brief '
                                                                 'running-to-blocked transition. '
                                                                 "Defaults to 'running', which "
                                                                 'preserves the usual dispatch path.'},
                               'skills': {'type': 'array',
                                          'items': {'type': 'string'},
                                          'description': 'Skill names to force-load into the dispatched '
                                                         'worker. The kanban lifecycle is already '
                                                         'injected automatically; use this to pin a '
                                                         'task to a specialist context — e.g. '
                                                         "['translation'] for a translation task, "
                                                         "['github-code-review'] for a reviewer task. "
                                                         'The names must match skills installed on the '
                                                         "assignee's profile."},
                               'goal_mode': {'type': 'boolean',
                                             'description': 'Run the dispatched worker in a goal loop. '
                                                            'When true, after each turn an auxiliary '
                                                            "judge checks the worker's response against "
                                                            "this card's title/body; if the work isn't "
                                                            'done and budget remains, the worker keeps '
                                                            'going in the same session until the judge '
                                                            "agrees it's complete (or the goal-turn "
                                                            'budget is exhausted, which blocks the task '
                                                            'for human review). Use this for open-ended '
                                                            'cards where one shot rarely finishes the '
                                                            'work. Defaults to false (classic '
                                                            'single-shot worker).'},
                               'completion_contract': {'type': 'string',
                                                       'description': 'Declare at creation: local-only '
                                                                      '(default), OWNER/REPO for PR '
                                                                      'publication, or an exact GitHub '
                                                                      'PR URL. PR tasks cannot complete '
                                                                      'until repository-required '
                                                                      'exact-head CI passes. On '
                                                                      'publication pass '
                                                                      'metadata.published_pr.'},
                               'goal_max_turns': {'type': 'integer',
                                                  'description': 'Turn budget for goal_mode workers. '
                                                                 'Caps how many continuation turns the '
                                                                 'worker may take before the task is '
                                                                 'blocked for review. Ignored unless '
                                                                 'goal_mode is true. Defaults to the '
                                                                 'goal-engine default (20).'},
                               'model': {'type': 'string',
                                         'description': 'Pin the dispatched worker to this model '
                                                        "instead of the assignee profile's configured "
                                                        'model. Use the exact model name the target '
                                                        'provider expects. Omit to use the profile '
                                                        'default.'},
                               'provider': {'type': 'string',
                                            'description': "Provider the 'model' belongs to (e.g. "
                                                           "'openrouter', 'anthropic', 'nous'). Set "
                                                           'this whenever the model is not from the '
                                                           "assignee profile's configured provider — a "
                                                           'model name alone is resolved against the '
                                                           "profile's provider and will fail if it "
                                                           'belongs to a different one. Requires '
                                                           "'model'."},
                               'board': {'type': 'string',
                                         'description': 'Kanban board slug to target. When omitted, the '
                                                        'call resolves the active board the usual way: '
                                                        'HERMES_KANBAN_DB env → HERMES_KANBAN_BOARD env '
                                                        "→ the 'current' symlink under the kanban home "
                                                        "→ 'default'. Pass an explicit slug only when "
                                                        'the caller (e.g. a Telegram routing layer) '
                                                        'needs to override the env-pinned active board '
                                                        'for this one call.'},
                               'source_policy': {'type': 'string',
                                                 'enum': ['none', 'required', 'forbidden'],
                                                 'description': 'Default-board execution contract for '
                                                                'source commits.'},
                               'workflow_template_id': {'type': 'string',
                                                        'description': 'Optional workflow template id '
                                                                       'to stamp at creation time. Use '
                                                                       "'product' for Kanban V2 "
                                                                       'product-board cards.'},
                               'current_step_key': {'type': 'string',
                                                    'description': 'Optional workflow step key to stamp '
                                                                   "at creation time. Use 'backlog' for "
                                                                   'new product user-story/work cards '
                                                                   'unless a later approved flow '
                                                                   'intentionally targets another '
                                                                   'step.'}},
                'required': ['title', 'assignee']}}

KANBAN_UNBLOCK_SCHEMA = _schema(
    "kanban_unblock",
    (
        "Unblock a Kanban task. It moves to ready when all parents are done, "
        "or todo while any parent remains open. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    {
        "task_id": _prop("string", "Blocked task id to move to ready or parent-gated todo."),
    },
    ["task_id"],
)

KANBAN_LINK_SCHEMA = _schema(
    "kanban_link",
    (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    {
        "parent_id": {"type": "string", "description": "Parent task id."},
        "child_id":  {"type": "string", "description": "Child task id."},
    },
    ["parent_id", "child_id"],
)

WORK_INBOX_DECIDE_SCHEMA = {'name': 'work_inbox_decide',
 'description': 'Finish the Product Owner assessment. Accepted proposals are validated, signed, and '
                'materialized by Hermes; this tool does not grant direct card authority.',
 'parameters': {'type': 'object',
                'properties': {'disposition': {'type': 'string',
                                               'enum': ['accepted', 'needs_clarification', 'rejected']},
                               'reason': {'type': 'string'},
                               'question': {'type': 'string'},
                               'proposal': {'type': 'object',
                                            'description': 'Complete semantic Product Decision. Hermes '
                                                           'supplies trusted PO evidence, entry '
                                                           'routing, skipped-phase evidence, and issuer '
                                                           'identity.',
                                            'properties': {'work': {'type': 'object',
                                                                    'properties': {'item_kind': {'type': 'string',
                                                                                                 'enum': ['card',
                                                                                                          'epic']},
                                                                                   'work_type': {'type': 'string'},
                                                                                   'title': {'type': 'string'},
                                                                                   'outcome': {'type': 'string'},
                                                                                   'scope': {'type': 'array',
                                                                                             'items': {'type': 'string'}},
                                                                                   'out_of_scope': {'type': 'array',
                                                                                                    'items': {'type': 'string'}}},
                                                                    'required': ['item_kind',
                                                                                 'work_type',
                                                                                 'title',
                                                                                 'outcome',
                                                                                 'scope',
                                                                                 'out_of_scope'],
                                                                    'additionalProperties': False},
                                                           'routing': {'type': 'object',
                                                                       'properties': {'entry_phase': {'type': ['string',
                                                                                                               'null'],
                                                                                                      'description': 'Use '
                                                                                                                     'null '
                                                                                                                     'for '
                                                                                                                     'an '
                                                                                                                     'Epic; '
                                                                                                                     'Hermes '
                                                                                                                     'replaces '
                                                                                                                     'card '
                                                                                                                     'routing '
                                                                                                                     'with '
                                                                                                                     'the '
                                                                                                                     'board '
                                                                                                                     'Architecture '
                                                                                                                     'phase.'},
                                                                                      'assignee': {'type': ['string',
                                                                                                            'null'],
                                                                                                   'description': 'Use '
                                                                                                                  'null '
                                                                                                                  'for '
                                                                                                                  'an '
                                                                                                                  'Epic; '
                                                                                                                  'Hermes '
                                                                                                                  'supplies '
                                                                                                                  'the '
                                                                                                                  'card '
                                                                                                                  'assignee.'},
                                                                                      'epic_id': {'type': ['string',
                                                                                                           'null']},
                                                                                      'dependencies': {'type': 'array',
                                                                                                       'items': {'type': 'string'}}},
                                                                       'required': ['entry_phase',
                                                                                    'assignee',
                                                                                    'epic_id',
                                                                                    'dependencies'],
                                                                       'additionalProperties': False},
                                                           'handover': {'type': 'object',
                                                                        'properties': {'deliverables': {'type': 'array',
                                                                                                        'items': {'type': 'string'}},
                                                                                       'required_evidence': {'type': 'array',
                                                                                                             'items': {'type': 'string'}},
                                                                                       'done_when': {'type': 'array',
                                                                                                     'items': {'type': 'string'}},
                                                                                       'next_phase': {'type': ['string',
                                                                                                               'null']},
                                                                                       'next_role': {'type': ['string',
                                                                                                              'null']}},
                                                                        'required': ['deliverables',
                                                                                     'required_evidence',
                                                                                     'done_when',
                                                                                     'next_phase',
                                                                                     'next_role'],
                                                                        'additionalProperties': False},
                                                           'rules': {'type': 'object',
                                                                     'properties': {'allowed': {'type': 'array',
                                                                                                'items': {'type': 'string'}},
                                                                                    'forbidden': {'type': 'array',
                                                                                                  'items': {'type': 'string'}}},
                                                                     'required': ['allowed',
                                                                                  'forbidden'],
                                                                     'additionalProperties': False},
                                                           'sizing': {'type': 'object',
                                                                      'description': 'Independent '
                                                                                     'Product Owner '
                                                                                     'sizing. The '
                                                                                     'configured budget '
                                                                                     'is the '
                                                                                     'Development '
                                                                                     "profile's "
                                                                                     'agent.max_turns; '
                                                                                     'provide one '
                                                                                     'estimate for a '
                                                                                     'card or one per '
                                                                                     'Epic story.',
                                                                      'properties': {'rationale': {'type': 'string'},
                                                                                     'configured_iteration_budget': {'type': 'integer',
                                                                                                                     'minimum': 1},
                                                                                     'estimated_turns': {'type': 'integer',
                                                                                                         'minimum': 1},
                                                                                     'card_estimates': {'type': 'array',
                                                                                                        'items': {'type': 'integer',
                                                                                                                  'minimum': 1}},
                                                                                     'fits_budget': {'type': 'boolean'}},
                                                                      'required': ['rationale',
                                                                                   'configured_iteration_budget',
                                                                                   'estimated_turns',
                                                                                   'fits_budget'],
                                                                      'additionalProperties': False},
                                                           'requirement_feasibility': {'type': 'object',
                                                                                       'description': 'Auditable '
                                                                                                      'achievability '
                                                                                                      'gate. '
                                                                                                      'Every '
                                                                                                      'binding '
                                                                                                      'required-evidence '
                                                                                                      'or '
                                                                                                      'Epic '
                                                                                                      'story '
                                                                                                      'done-when '
                                                                                                      'item '
                                                                                                      'must '
                                                                                                      'appear '
                                                                                                      'exactly '
                                                                                                      'once '
                                                                                                      'under '
                                                                                                      'achievable_requirements '
                                                                                                      'with '
                                                                                                      'a '
                                                                                                      'concrete '
                                                                                                      'basis. '
                                                                                                      'Current-state '
                                                                                                      'Test '
                                                                                                      'findings '
                                                                                                      'that '
                                                                                                      'cannot '
                                                                                                      'yet '
                                                                                                      'be '
                                                                                                      'achieved '
                                                                                                      'belong '
                                                                                                      'in '
                                                                                                      'deferred_findings '
                                                                                                      'and '
                                                                                                      'must '
                                                                                                      'not '
                                                                                                      'remain '
                                                                                                      'binding.',
                                                                                       'properties': {'rationale': {'type': 'string'},
                                                                                                      'achievable_requirements': {'type': 'array',
                                                                                                                                  'items': {'type': 'object',
                                                                                                                                            'properties': {'requirement': {'type': 'string'},
                                                                                                                                                           'basis': {'type': 'array',
                                                                                                                                                                     'items': {'type': 'string'}}},
                                                                                                                                            'required': ['requirement',
                                                                                                                                                         'basis'],
                                                                                                                                            'additionalProperties': False}},
                                                                                                      'deferred_findings': {'type': 'array',
                                                                                                                            'items': {'type': 'object',
                                                                                                                                      'properties': {'finding': {'type': 'string'},
                                                                                                                                                     'reason': {'type': 'string'},
                                                                                                                                                     'enabling_dependency': {'type': 'string'}},
                                                                                                                                      'required': ['finding',
                                                                                                                                                   'reason',
                                                                                                                                                   'enabling_dependency'],
                                                                                                                                      'additionalProperties': False}}},
                                                                                       'required': ['rationale',
                                                                                                    'achievable_requirements',
                                                                                                    'deferred_findings'],
                                                                                       'additionalProperties': False},
                                                           'classification': {'type': 'array',
                                                                              'items': {'type': 'string'}},
                                                           'stories': {'type': 'array',
                                                                       'description': 'Empty for a '
                                                                                      'card; required '
                                                                                      'decomposition '
                                                                                      'for an Epic.',
                                                                       'items': {'type': 'object',
                                                                                 'properties': {'title': {'type': 'string'},
                                                                                                'outcome': {'type': 'string'},
                                                                                                'scope': {'type': 'array',
                                                                                                          'items': {'type': 'string'}},
                                                                                                'out_of_scope': {'type': 'array',
                                                                                                                 'items': {'type': 'string'}},
                                                                                                'done_when': {'type': 'array',
                                                                                                              'items': {'type': 'string'}},
                                                                                                'depends_on': {'type': 'array',
                                                                                                               'items': {'type': 'integer',
                                                                                                                         'minimum': 0}}},
                                                                                 'required': ['title',
                                                                                              'outcome',
                                                                                              'scope',
                                                                                              'out_of_scope',
                                                                                              'done_when',
                                                                                              'depends_on'],
                                                                                 'additionalProperties': False}}},
                                            'required': ['work',
                                                         'routing',
                                                         'handover',
                                                         'rules',
                                                         'sizing',
                                                         'requirement_feasibility',
                                                         'classification',
                                                         'stories'],
                                            'additionalProperties': False}},
                'required': ['disposition', 'reason']}}

WORK_INBOX_PROPOSAL_SCHEMA = {'type': 'object',
 'description': 'Complete semantic Product Decision. Hermes supplies trusted PO evidence, entry '
                'routing, skipped-phase evidence, and issuer identity.',
 'properties': {'work': {'type': 'object',
                         'properties': {'item_kind': {'type': 'string', 'enum': ['card', 'epic']},
                                        'work_type': {'type': 'string'},
                                        'title': {'type': 'string'},
                                        'outcome': {'type': 'string'},
                                        'scope': {'type': 'array', 'items': {'type': 'string'}},
                                        'out_of_scope': {'type': 'array', 'items': {'type': 'string'}}},
                         'required': ['item_kind',
                                      'work_type',
                                      'title',
                                      'outcome',
                                      'scope',
                                      'out_of_scope'],
                         'additionalProperties': False},
                'routing': {'type': 'object',
                            'properties': {'entry_phase': {'type': ['string', 'null'],
                                                           'description': 'Use null for an Epic; Hermes '
                                                                          'replaces card routing with '
                                                                          'the board Architecture '
                                                                          'phase.'},
                                           'assignee': {'type': ['string', 'null'],
                                                        'description': 'Use null for an Epic; Hermes '
                                                                       'supplies the card assignee.'},
                                           'epic_id': {'type': ['string', 'null']},
                                           'dependencies': {'type': 'array',
                                                            'items': {'type': 'string'}}},
                            'required': ['entry_phase', 'assignee', 'epic_id', 'dependencies'],
                            'additionalProperties': False},
                'handover': {'type': 'object',
                             'properties': {'deliverables': {'type': 'array',
                                                             'items': {'type': 'string'}},
                                            'required_evidence': {'type': 'array',
                                                                  'items': {'type': 'string'}},
                                            'done_when': {'type': 'array', 'items': {'type': 'string'}},
                                            'next_phase': {'type': ['string', 'null']},
                                            'next_role': {'type': ['string', 'null']}},
                             'required': ['deliverables',
                                          'required_evidence',
                                          'done_when',
                                          'next_phase',
                                          'next_role'],
                             'additionalProperties': False},
                'rules': {'type': 'object',
                          'properties': {'allowed': {'type': 'array', 'items': {'type': 'string'}},
                                         'forbidden': {'type': 'array', 'items': {'type': 'string'}}},
                          'required': ['allowed', 'forbidden'],
                          'additionalProperties': False},
                'sizing': {'type': 'object',
                           'description': 'Independent Product Owner sizing. The configured budget is '
                                          "the Development profile's agent.max_turns; provide one "
                                          'estimate for a card or one per Epic story.',
                           'properties': {'rationale': {'type': 'string'},
                                          'configured_iteration_budget': {'type': 'integer',
                                                                          'minimum': 1},
                                          'estimated_turns': {'type': 'integer', 'minimum': 1},
                                          'card_estimates': {'type': 'array',
                                                             'items': {'type': 'integer', 'minimum': 1}},
                                          'fits_budget': {'type': 'boolean'}},
                           'required': ['rationale',
                                        'configured_iteration_budget',
                                        'estimated_turns',
                                        'fits_budget'],
                           'additionalProperties': False},
                'requirement_feasibility': {'type': 'object',
                                            'description': 'Auditable achievability gate. Every binding '
                                                           'required-evidence or Epic story done-when '
                                                           'item must appear exactly once under '
                                                           'achievable_requirements with a concrete '
                                                           'basis. Current-state Test findings that '
                                                           'cannot yet be achieved belong in '
                                                           'deferred_findings and must not remain '
                                                           'binding.',
                                            'properties': {'rationale': {'type': 'string'},
                                                           'achievable_requirements': {'type': 'array',
                                                                                       'items': {'type': 'object',
                                                                                                 'properties': {'requirement': {'type': 'string'},
                                                                                                                'basis': {'type': 'array',
                                                                                                                          'items': {'type': 'string'}}},
                                                                                                 'required': ['requirement',
                                                                                                              'basis'],
                                                                                                 'additionalProperties': False}},
                                                           'deferred_findings': {'type': 'array',
                                                                                 'items': {'type': 'object',
                                                                                           'properties': {'finding': {'type': 'string'},
                                                                                                          'reason': {'type': 'string'},
                                                                                                          'enabling_dependency': {'type': 'string'}},
                                                                                           'required': ['finding',
                                                                                                        'reason',
                                                                                                        'enabling_dependency'],
                                                                                           'additionalProperties': False}}},
                                            'required': ['rationale',
                                                         'achievable_requirements',
                                                         'deferred_findings'],
                                            'additionalProperties': False},
                'classification': {'type': 'array', 'items': {'type': 'string'}},
                'stories': {'type': 'array',
                            'description': 'Empty for a card; required decomposition for an Epic.',
                            'items': {'type': 'object',
                                      'properties': {'title': {'type': 'string'},
                                                     'outcome': {'type': 'string'},
                                                     'scope': {'type': 'array',
                                                               'items': {'type': 'string'}},
                                                     'out_of_scope': {'type': 'array',
                                                                      'items': {'type': 'string'}},
                                                     'done_when': {'type': 'array',
                                                                   'items': {'type': 'string'}},
                                                     'depends_on': {'type': 'array',
                                                                    'items': {'type': 'integer',
                                                                              'minimum': 0}}},
                                      'required': ['title',
                                                   'outcome',
                                                   'scope',
                                                   'out_of_scope',
                                                   'done_when',
                                                   'depends_on'],
                                      'additionalProperties': False}}},
 'required': ['work',
              'routing',
              'handover',
              'rules',
              'sizing',
              'requirement_feasibility',
              'classification',
              'stories'],
 'additionalProperties': False}

_WORK_INBOX_STRING_LIST_SCHEMA = {'type': 'array', 'items': {'type': 'string'}}

WORK_INBOX_HEARTBEAT_SCHEMA = {'name': 'work_inbox_heartbeat',
 'description': 'Renew the exact Product Owner intake claim.',
 'parameters': {'type': 'object', 'properties': {'note': {'type': 'string'}}}}

WORK_INBOX_SHOW_SCHEMA = {'name': 'work_inbox_show',
 'description': 'Read the exact claimed Work Inbox intake and authoritative context.',
 'parameters': {'type': 'object', 'properties': {}}}

KANBAN_RESOLVE_SCHEMA = {'name': 'kanban_resolve',
 'description': 'Resolve the current Hermes product-workflow preflight using one audited, '
                'compare-and-swap decision. Resolver-only. Read the task with kanban_show immediately '
                'before calling and copy the complete task/preflight snapshot into expected. Conflicts '
                'never retry automatically.',
 'parameters': {'type': 'object',
                'properties': {'task_id': {'type': 'string',
                                           'description': 'Task id. If omitted, defaults to '
                                                          'HERMES_KANBAN_TASK from the env (the task '
                                                          'the dispatcher spawned you to work on).'},
                               'board': {'type': 'string',
                                         'description': 'Kanban board slug to target. When omitted, the '
                                                        'call resolves the active board the usual way: '
                                                        'HERMES_KANBAN_DB env → HERMES_KANBAN_BOARD env '
                                                        "→ the 'current' symlink under the kanban home "
                                                        "→ 'default'. Pass an explicit slug only when "
                                                        'the caller (e.g. a Telegram routing layer) '
                                                        'needs to override the env-pinned active board '
                                                        'for this one call.'},
                               'decision': {'type': 'string', 'enum': ['resume', 'repair', 'escalate']},
                               'fault_domain': {'type': 'string', 'enum': ['task_state', 'framework']},
                               'diagnosis': {'type': 'string'},
                               'reason': {'type': 'string'},
                               'expected': {'type': 'object',
                                            'properties': {'run_id': {'type': 'integer'},
                                                           'preflight_event_id': {'type': 'integer'},
                                                           'status': {'type': 'string'},
                                                           'phase': {'type': ['string', 'null']},
                                                           'assignee': {'type': ['string', 'null']},
                                                           'project_id': {'type': ['string', 'null']},
                                                           'workflow_template_id': {'type': ['string',
                                                                                             'null']},
                                                           'workspace_kind': {'type': 'string'},
                                                           'workspace_path': {'type': ['string',
                                                                                       'null']},
                                                           'branch_name': {'type': ['string', 'null']},
                                                           'running': {'type': 'boolean'},
                                                           'blocked': {'type': 'boolean'}},
                                            'required': ['run_id',
                                                         'preflight_event_id',
                                                         'status',
                                                         'phase',
                                                         'assignee',
                                                         'project_id',
                                                         'workflow_template_id',
                                                         'workspace_kind',
                                                         'workspace_path',
                                                         'branch_name',
                                                         'running',
                                                         'blocked'],
                                            'additionalProperties': False},
                               'repair': {'type': 'object',
                                          'properties': {'workflow': {'type': 'object',
                                                                      'properties': {'phase': {'type': 'string',
                                                                                               'enum': ['backlog',
                                                                                                        'architecture',
                                                                                                        'development',
                                                                                                        'test',
                                                                                                        'review']},
                                                                                     'assignee': {'type': 'string'},
                                                                                     'project_id': {'type': 'string'}},
                                                                      'additionalProperties': False},
                                                         'adopt_handoff_sha': {'type': 'string'}},
                                          'additionalProperties': False}},
                'required': ['task_id', 'decision', 'fault_domain', 'diagnosis', 'reason', 'expected'],
                'additionalProperties': False}}

KANBAN_CONFIGURE_SCHEMA = {'name': 'kanban_configure',
 'description': 'Atomically replace source_policy, max_retries, max_runtime_seconds, and goal_mode on '
                'one eligible existing Default-board card. Configured-orchestrator-only. Call '
                'kanban_show immediately first and copy the nine current lifecycle/execution fields '
                'into expected. Refuses stale, active, terminal, delegated, worker, and strict-board '
                'calls.',
 'parameters': {'type': 'object',
                'properties': {'task_id': {'type': 'string', 'description': 'Existing task id.'},
                               'source_policy': {'type': 'string',
                                                 'enum': ['none', 'required', 'forbidden']},
                               'max_retries': {'type': ['integer', 'null'], 'minimum': 1},
                               'max_runtime_seconds': {'type': ['integer', 'null'], 'minimum': 1},
                               'goal_mode': {'type': 'boolean'},
                               'expected': {'type': 'object',
                                            'properties': {'status': {'type': 'string'},
                                                           'title': {'type': 'string'},
                                                           'assignee': {'type': ['string', 'null']},
                                                           'current_step_key': {'type': ['string',
                                                                                         'null']},
                                                           'current_run_id': {'type': ['integer',
                                                                                       'null']},
                                                           'source_policy': {'type': 'string',
                                                                             'enum': ['none',
                                                                                      'required',
                                                                                      'forbidden']},
                                                           'max_retries': {'type': ['integer', 'null'],
                                                                           'minimum': 1},
                                                           'max_runtime_seconds': {'type': ['integer',
                                                                                            'null'],
                                                                                   'minimum': 1},
                                                           'goal_mode': {'type': 'boolean'}},
                                            'required': ['status',
                                                         'title',
                                                         'assignee',
                                                         'current_step_key',
                                                         'current_run_id',
                                                         'source_policy',
                                                         'max_retries',
                                                         'max_runtime_seconds',
                                                         'goal_mode'],
                                            'additionalProperties': False},
                               'board': {'type': 'string',
                                         'description': 'Kanban board slug to target. When omitted, the '
                                                        'call resolves the active board the usual way: '
                                                        'HERMES_KANBAN_DB env → HERMES_KANBAN_BOARD env '
                                                        "→ the 'current' symlink under the kanban home "
                                                        "→ 'default'. Pass an explicit slug only when "
                                                        'the caller (e.g. a Telegram routing layer) '
                                                        'needs to override the env-pinned active board '
                                                        'for this one call.'}},
                'required': ['task_id',
                             'source_policy',
                             'max_retries',
                             'max_runtime_seconds',
                             'goal_mode',
                             'expected'],
                'additionalProperties': False}}

_KANBAN_CONFIGURE_EXPECTED_SCHEMA = {'type': 'object',
 'properties': {'status': {'type': 'string'},
                'title': {'type': 'string'},
                'assignee': {'type': ['string', 'null']},
                'current_step_key': {'type': ['string', 'null']},
                'current_run_id': {'type': ['integer', 'null']},
                'source_policy': {'type': 'string', 'enum': ['none', 'required', 'forbidden']},
                'max_retries': {'type': ['integer', 'null'], 'minimum': 1},
                'max_runtime_seconds': {'type': ['integer', 'null'], 'minimum': 1},
                'goal_mode': {'type': 'boolean'}},
 'required': ['status',
              'title',
              'assignee',
              'current_step_key',
              'current_run_id',
              'source_policy',
              'max_retries',
              'max_runtime_seconds',
              'goal_mode'],
 'additionalProperties': False}

KANBAN_UNLINK_SCHEMA = {'name': 'kanban_unlink',
 'description': 'Atomically remove one exact Default-board parent→child dependency edge using the '
                "child's fresh five-field lifecycle snapshot. Only that child is reconsidered for "
                'readiness. Configured-orchestrator-only; call kanban_show immediately first and copy '
                "the child's current lifecycle fields into expected.",
 'parameters': {'type': 'object',
                'properties': {'parent_id': {'type': 'string',
                                             'minLength': 1,
                                             'description': 'Existing parent task id for the exact '
                                                            'edge.'},
                               'child_id': {'type': 'string',
                                            'minLength': 1,
                                            'description': 'Existing child task id for the exact edge.'},
                               'expected': {'type': 'object',
                                            'properties': {'status': {'type': 'string'},
                                                           'title': {'type': 'string'},
                                                           'assignee': {'type': ['string', 'null']},
                                                           'current_step_key': {'type': ['string',
                                                                                         'null']},
                                                           'current_run_id': {'type': ['integer',
                                                                                       'null']}},
                                            'required': ['status',
                                                         'title',
                                                         'assignee',
                                                         'current_step_key',
                                                         'current_run_id'],
                                            'additionalProperties': False},
                               'board': {'type': 'string',
                                         'description': 'Kanban board slug to target. When omitted, the '
                                                        'call resolves the active board the usual way: '
                                                        'HERMES_KANBAN_DB env → HERMES_KANBAN_BOARD env '
                                                        "→ the 'current' symlink under the kanban home "
                                                        "→ 'default'. Pass an explicit slug only when "
                                                        'the caller (e.g. a Telegram routing layer) '
                                                        'needs to override the env-pinned active board '
                                                        'for this one call.'}},
                'required': ['parent_id', 'child_id', 'expected'],
                'additionalProperties': False}}

_KANBAN_UNLINK_EXPECTED_SCHEMA = {'type': 'object',
 'properties': {'status': {'type': 'string'},
                'title': {'type': 'string'},
                'assignee': {'type': ['string', 'null']},
                'current_step_key': {'type': ['string', 'null']},
                'current_run_id': {'type': ['integer', 'null']}},
 'required': ['status', 'title', 'assignee', 'current_step_key', 'current_run_id'],
 'additionalProperties': False}

REVIEW_TARGET_SCHEMA = {'name': 'review_target',
 'description': 'Read the immutable Git diff pinned for the current Reviewer run. Returns fixed '
                'base/head commit SHAs, changed and binary files, and one bounded diff-line page with '
                'explicit overlong-line truncation. Continue with next_offset until complete is true.',
 'parameters': {'type': 'object',
                'properties': {'offset': {'type': 'integer',
                                          'minimum': 0,
                                          'description': 'Diff-line offset into the pinned diff '
                                                         '(default 0).'}},
                'required': [],
                'additionalProperties': False}}
