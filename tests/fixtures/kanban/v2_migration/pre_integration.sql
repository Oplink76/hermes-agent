-- Pre-integration fixture: a minimal kanban database with a mix of legacy
-- and partially-migrated tasks ready for v2 migration testing.
--
-- This creates the same schema as init_db() minus the v2-specific columns
-- (running, blocked, source_commit_required, source_commit_forbidden) —
-- those are added by _migrate_add_optional_columns at connect time and
-- represent the pre-migration state.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER,
    workflow_template_id TEXT,
    current_step_key     TEXT,
    work_contract_id     TEXT,
    work_item_kind       TEXT NOT NULL DEFAULT 'card',
    skills               TEXT,
    model_override       TEXT,
    provider_override    TEXT,
    reasoning_effort     TEXT,
    max_retries          INTEGER,
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    goal_max_turns       INTEGER,
    session_id           TEXT,
    block_kind           TEXT,
    block_recurrences    INTEGER NOT NULL DEFAULT 0,
    rework_count         INTEGER NOT NULL DEFAULT 0,
    -- v2 handoff state-model columns
    running               INTEGER NOT NULL DEFAULT 0,
    blocked               INTEGER NOT NULL DEFAULT 0,
    source_commit_required INTEGER NOT NULL DEFAULT 0,
    source_commit_forbidden INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS board_governance (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    qualification_required INTEGER NOT NULL DEFAULT 0
                               CHECK (qualification_required IN (0, 1))
);
INSERT OR IGNORE INTO board_governance (id, qualification_required) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS epic_memberships (
    epic_id          TEXT NOT NULL,
    task_id          TEXT NOT NULL UNIQUE,
    created_at       INTEGER NOT NULL,
    PRIMARY KEY (epic_id, task_id)
);

-- -----------------------------------------------------------------------
-- Sample data: 8 tasks in various states
-- -----------------------------------------------------------------------

-- t1: legacy todo task with no workflow metadata — needs migration
INSERT INTO tasks (id, title, body, assignee, status, created_at, work_item_kind) VALUES
('t_001', 'Fix login timeout bug', 'Users report timeout after 30s. Investigate root cause.', 'developer', 'todo', 1700000000, 'card');

-- t2: legacy ready task assigned to architect — should infer architecture step
INSERT INTO tasks (id, title, body, assignee, status, created_at, work_item_kind) VALUES
('t_002', 'Design new auth flow', 'High-level architecture for OAuth2 integration.', 'architect', 'ready', 1700000100, 'card');

-- t3: legacy task in review — should infer review step
INSERT INTO tasks (id, title, body, assignee, status, created_at, work_item_kind) VALUES
('t_003', 'Refactor payment module', 'Extract payment logic into separate service.', 'reviewer', 'review', 1700000200, 'card');

-- t4: already a product task with workflow_template_id=product — should be skipped
INSERT INTO tasks (id, title, body, assignee, status, created_at, workflow_template_id, current_step_key, work_item_kind) VALUES
('t_004', 'Add rate limiting middleware', 'Implement token bucket rate limiter.', 'developer', 'ready', 1700000300, 'product', 'development', 'card');

-- t5: legacy task with no assignee — should infer backlog
INSERT INTO tasks (id, title, body, assignee, status, created_at, work_item_kind) VALUES
('t_005', 'Write integration test suite', 'Cover the new API endpoints with integration tests.', NULL, 'todo', 1700000400, 'card');

-- t6: done task — should stay in done/release_measure
INSERT INTO tasks (id, title, body, assignee, status, created_at, completed_at, work_item_kind) VALUES
('t_006', 'Deploy v1.2.0', 'Release notes and deployment runbook.', NULL, 'done', 1700000500, 1700000600, 'card');

-- t7: legacy task with tester assignee — should infer test step
INSERT INTO tasks (id, title, body, assignee, status, created_at, work_item_kind) VALUES
('t_007', 'Verify OAuth2 endpoints', 'Test all OAuth2 grant types against staging.', 'tester', 'ready', 1700000700, 'card');

-- t8: epic — should be recognized and not assigned a step
INSERT INTO tasks (id, title, body, assignee, status, created_at, work_item_kind) VALUES
('t_e8', 'Epic: Platform v2 migration', 'Umbrella epic for the v2 platform migration.', NULL, 'ready', 1700000800, 'epic');

-- Epic membership: t_001 and t_002 are children of the epic
INSERT INTO epic_memberships (epic_id, task_id, created_at) VALUES
('t_e8', 't_001', 1700000900);
INSERT INTO epic_memberships (epic_id, task_id, created_at) VALUES
('t_e8', 't_002', 1700000910);

-- Dependency link: t_003 depends on t_004
INSERT INTO task_links (parent_id, child_id) VALUES
('t_004', 't_003');

-- Comments on a legacy task — must survive migration
INSERT INTO task_comments (task_id, author, body, created_at) VALUES
('t_001', 'alice', 'I can reproduce this on staging — the timeout is exactly 30s.', 1700001000);
INSERT INTO task_comments (task_id, author, body, created_at) VALUES
('t_001', 'bob', 'The session TTL is hardcoded in config.py line 142.', 1700001100);

-- Events on a legacy task — must survive migration
INSERT INTO task_events (task_id, kind, payload, created_at) VALUES
('t_001', 'created', '{"by": "alice"}', 1700000000);
INSERT INTO task_events (task_id, kind, payload, created_at) VALUES
('t_001', 'assigned', '{"assignee": "developer"}', 1700000050);