"""Tests for the Kanban DB layer (hermes_cli.kanban_db)."""

from __future__ import annotations

import concurrent.futures
from dataclasses import replace
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
import unittest.mock
from pathlib import Path, PurePosixPath

import pytest

import hermes_state
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_repository import (
    VerificationCommand,
    VerificationProfile,
    run_verification,
    verification_receipt_from_payload,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    """Write + commit a file on whatever branch is currently checked out in
    ``repo``; returns the new commit sha."""
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True, capture_output=True, text=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------



def test_no_governed_agent_memory_helpers_remain():
    offenders = sorted(
        name
        for name in vars(kb)
        if name.startswith("_agent_memory_")
        or name == "_remember_" + "kanban_run_best_effort"
    )
    assert offenders == []


def test_legacy_agent_memory_metadata_remains_readable_through_get_run(
    kanban_home,
):
    board = "legacy-memory-metadata"
    kb.create_board(board, name="Legacy metadata")
    legacy = {
        "agent_memory": {"write": {"status": "stored"}},
        "unrelated": {"worker_session_id": "session-old"},
    }

    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn, title="Legacy run", initial_status="running", board=board
        )
        claimed = kb.claim_task(conn, task_id, board=board)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps(legacy), run_id),
        )
        conn.commit()

        run = kb.get_run(conn, run_id)

    assert run is not None
    assert run.metadata == legacy


def test_init_creates_expected_tables(kanban_home):
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert {
        "tasks",
        "task_links",
        "task_comments",
        "task_events",
        "product_rework_directives",
    } <= names


def test_epic_record_schema_creates_only_the_three_additive_tables(kanban_home):
    expected = {
        "story_integration_intents",
        "epic_release_snapshots",
        "epic_release_members",
    }

    with kb.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert expected <= tables
    assert "repository_verification_runs" not in tables


def test_epic_record_migration_adds_only_three_tables_and_is_idempotent(tmp_path):
    db_path = tmp_path / "pre-feature.db"
    expected = {
        "story_integration_intents",
        "epic_release_snapshots",
        "epic_release_members",
    }

    with kb.connect(db_path) as conn:
        modern_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        conn.execute("DROP TABLE epic_release_members")
        conn.execute("DROP TABLE epic_release_snapshots")
        conn.execute("DROP TABLE story_integration_intents")

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        migrated_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert migrated_tables - (modern_tables - expected) == expected
    assert migrated_tables == modern_tables

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        rerun_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert rerun_tables == migrated_tables


def test_board_metadata_repository_policy_is_validated(kanban_home, tmp_path):
    repo = tmp_path / "repository-policy"
    _init_git_repo(repo)
    _commit_file(repo, "generated.txt", "generated\n", "add generated path")
    policy = {
        "base_ref": "refs/heads/main",
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": {
                "commands": [
                    {
                        "argv": ["python", "-m", "unittest"],
                        "workdir": ".",
                        "timeout_seconds": 60,
                    }
                ]
            },
            "epic_release": {
                "commands": [
                    {
                        "argv": ["python", "-m", "unittest"],
                        "workdir": ".",
                        "timeout_seconds": 60,
                    }
                ]
            },
        },
        "ci_observation": {
            "provider": "github_actions",
            "required_workflows": ["CI"],
        },
        "boundary_evidence": {
            "test_globs": ["tests/**"],
            "fixture_globs": ["tests/fixtures/**"],
            "generated_paths": ["generated.txt"],
        },
    }

    metadata = kb.ensure_product_board_defaults(
        "repository-policy",
        default_workdir=str(repo),
        repository=policy,
    )
    assert metadata["repository"]["base_ref"] == "refs/heads/main"
    contract = kb.repository_contract_for_board("repository-policy")
    assert contract is not None and contract.digest

    invalid = dict(policy)
    invalid["target_branch"] = ""
    with pytest.raises(kb.RepositoryConfigurationError) as exc_info:
        kb.write_board_metadata(
            "repository-policy",
            default_workdir=str(repo),
            repository=invalid,
        )
    assert exc_info.value.code == "malformed_target_branch"




def test_cross_process_init_lock_uses_windows_byte_range_lock(tmp_path, monkeypatch):
    """Windows must use a real (non-blocking) process lock, not a no-op open.

    The init lock acquires with LK_NBLCK in a bounded retry loop (#36644) so a
    wedged holder can never block connect() forever; a clean acquire takes the
    lock once and releases it once.
    """
    calls: list[tuple[int, int, int]] = []
    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=3,
        LK_UNLCK=2,
        locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)),
    )
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    db_path = tmp_path / "kanban.db"
    with kb._cross_process_init_lock(db_path):
        # Acquired exactly once via the non-blocking byte-range lock.
        assert [call[1:] for call in calls] == [(fake_msvcrt.LK_NBLCK, 1)]

    # Released once on exit.
    assert [call[1:] for call in calls] == [
        (fake_msvcrt.LK_NBLCK, 1),
        (fake_msvcrt.LK_UNLCK, 1),
    ]


def test_connect_migrates_legacy_db_before_optional_column_indexes(tmp_path):
    """Legacy DBs missing additive indexed columns must migrate cleanly.

    SCHEMA_SQL runs in ``connect()`` before ``_migrate_add_optional_columns``.
    Indexes over additive columns therefore must be created after the
    migration adds those columns, or boards predating the column fail to
    open before migration can run.

    Covers all four indexes that sit on additive columns:
    - ``tasks.session_id``       -> ``idx_tasks_session_id``    (#28447)
    - ``tasks.tenant``           -> ``idx_tasks_tenant``        (#16081)
    - ``tasks.idempotency_key``  -> ``idx_tasks_idempotency``   (#17805)
    - ``task_events.run_id``     -> ``idx_events_run``          (#17805)
    """
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    # Pre-#16081 ``tasks`` shape: missing tenant, idempotency_key, session_id.
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
    """)
    # Pre-#17805 ``task_events`` shape: missing run_id. Required because
    # ``_migrate_add_optional_columns`` unconditionally runs PRAGMA on
    # ``task_events`` for run_id back-fill.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        task_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        event_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(task_events)")
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    # Additive columns added by migration:
    assert "session_id" in task_columns
    assert "tenant" in task_columns
    assert "idempotency_key" in task_columns
    assert "run_id" in event_columns
    # And their indexes — the regression scope of this test:
    assert "idx_tasks_session_id" in indexes
    assert "idx_tasks_tenant" in indexes
    assert "idx_tasks_idempotency" in indexes
    assert "idx_events_run" in indexes


def test_fresh_db_has_running_blocked_and_rework_columns(kanban_home):
    """New state-model and bounded-rework columns exist on fresh DBs."""
    with kb.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "running" in cols
    assert "blocked" in cols
    assert "rework_count" in cols


def test_legacy_db_gains_running_and_blocked_columns_without_data_loss(tmp_path):
    """Legacy DBs missing ``running``/``blocked`` must migrate cleanly (T1.1).

    Mirrors ``test_connect_migrates_legacy_db_before_optional_column_indexes``:
    build a pre-migration ``tasks`` shape, insert a real row, then run the
    migration path via ``kb.connect`` and assert the columns exist, default to
    0, and the pre-existing row/data survive. Also asserts the migration is
    idempotent by connecting a second time.
    """
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    # Pre-handoff_v2 ``tasks`` shape: missing running, blocked.
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        cols = {row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")}
        row = migrated.execute(
            "SELECT title, status, created_at, running, blocked, rework_count FROM tasks "
            "WHERE id = 'legacy'"
        ).fetchone()

    assert "running" in cols
    assert "blocked" in cols
    # Pre-existing row and its original data are intact.
    assert row["title"] == "old board task"
    assert row["status"] == "ready"
    assert row["created_at"] == 1
    # New columns default to 0 on the pre-existing row.
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["rework_count"] == 0

    # Idempotent: connecting again does not error or duplicate columns.
    with kb.connect(db_path) as migrated_again:
        cols_again = [
            row["name"] for row in migrated_again.execute("PRAGMA table_info(tasks)")
        ]
    assert cols_again.count("running") == 1
    assert cols_again.count("blocked") == 1
    assert cols_again.count("rework_count") == 1


# ---------------------------------------------------------------------------
# _legacy_status computed view (T1.2)
# ---------------------------------------------------------------------------

def test_legacy_status_running_flag_wins_over_idle_column(kanban_home):
    row = {"current_step_key": "development", "running": 1, "blocked": 0}
    assert kb._legacy_status(row) == "running"


def test_legacy_status_blocked_flag_set(kanban_home):
    row = {"current_step_key": "development", "running": 0, "blocked": 1}
    assert kb._legacy_status(row) == "blocked"


def test_legacy_status_blocked_wins_over_running(kanban_home):
    """Precedence: blocked beats running when both flags are truthy."""
    row = {"current_step_key": "development", "running": 1, "blocked": 1}
    assert kb._legacy_status(row) == "blocked"


def test_legacy_status_done_column(kanban_home):
    row = {"current_step_key": "done", "running": 0, "blocked": 0}
    assert kb._legacy_status(row) == "done"


def test_legacy_status_review_column(kanban_home):
    row = {"current_step_key": "review", "running": 0, "blocked": 0}
    assert kb._legacy_status(row) == "review"


def test_legacy_status_idle_non_terminal_is_ready(kanban_home):
    row = {"current_step_key": "development", "running": 0, "blocked": 0}
    assert kb._legacy_status(row) == "ready"


def test_legacy_status_meta_none_uses_product_template_defaults(kanban_home):
    """meta=None falls through to the product-template defaults, same as
    ``_column_status_for_step`` does on its own."""
    row = {"current_step_key": "review", "running": 0, "blocked": 0}
    assert kb._legacy_status(row, meta=None) == "review"


def test_legacy_status_honors_custom_meta_column_status(kanban_home):
    """A board with a custom column status in ``meta`` is honored, proving
    ``meta`` is actually consulted via ``_column_status_for_step``."""
    meta = {"columns": [{"name": "triage", "status": "triage"}]}
    row = {"current_step_key": "triage", "running": 0, "blocked": 0}
    assert kb._legacy_status(row, meta=meta) == "triage"


def test_legacy_status_accepts_real_sqlite_row(kanban_home):
    """The helper must also accept a real ``sqlite3.Row``, not just a dict."""
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT 'development' AS current_step_key, 1 AS running, 0 AS blocked"
        ).fetchone()
    assert kb._legacy_status(row) == "running"


# ---------------------------------------------------------------------------
# set_phase / set_running / set_blocked writers (T1.3)
# ---------------------------------------------------------------------------

def _v2_product_board(name: str) -> None:
    """Create a product-preset board with the ``handoff_v2`` opt-in flag set."""
    kb.create_board(name, name="V2 Board", preset="product")
    meta_path = kb.board_metadata_path(name)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["handoff_v2"] = True
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _seed_v2_card(board: str, *, step: str = "development") -> str:
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key=step,
        )
    return tid


def _seed_stale_terminal_card(board: str, *, phase: str = "development") -> tuple[str, int, int]:
    task_id = _seed_v2_card(board, step=phase)
    completed_at = 1_700_000_123
    with kb.connect(board=board) as conn:
        with kb.authorized_governance_write(), kb.write_txn(conn):
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'done',
                       completed_at = ?,
                       assignee = 'developer',
                       result = 'preserve this evidence',
                       project_id = 'project-1',
                       branch_name = 'feature/preserve-evidence'
                 WHERE id = ?
                """,
                (completed_at, task_id),
            )
            event_id = kb._append_event(
                conn,
                task_id,
                "completed",
                {"evidence": "preserve this event payload"},
            )
    return task_id, completed_at, event_id


def test_clear_terminal_state_clears_only_the_stale_generic_terminal_flag(kanban_home):
    board = "clear-terminal-success"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)

    with kb.connect(board=board) as conn:
        before = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        before_runs = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ]
        before_events = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ]

        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=completed_at,
            expected_phase="development",
            expected_latest_event_id=event_id,
            actor="operator",
            reason="clear stale generic terminal state",
        )
        assert kb.clear_terminal_state(conn, request) is True

        after = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        assert after["status"] == "ready"
        assert after["completed_at"] is None
        for field, value in before.items():
            if field not in {"status", "completed_at"}:
                assert after[field] == value, field
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ] == before_runs

        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        assert [tuple(row) for row in events[:-1]] == before_events
        assert events[-1]["kind"] == "terminal_state_cleared"
        payload = json.loads(events[-1]["payload"])
        assert payload == {
            "operation": "clear_terminal_state",
            "actor": "operator",
            "reason": "clear stale generic terminal state",
            "expected": {
                "status": "done",
                "completed_at": completed_at,
                "phase": "development",
                "latest_event_id": event_id,
            },
        }


@pytest.mark.parametrize("field", ["expected_completed_at", "expected_latest_event_id"])
def test_clear_terminal_state_refuses_stale_cas_fields(kanban_home, field):
    board = f"clear-terminal-stale-{field}"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)
    with kb.connect(board=board) as conn:
        before = {
            "task": tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
            "events": [
                tuple(row)
                for row in conn.execute(
                    "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
                ).fetchall()
            ],
        }
        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=(completed_at + 1 if field == "expected_completed_at" else completed_at),
            expected_phase="development",
            expected_latest_event_id=(event_id + 1 if field == "expected_latest_event_id" else event_id),
            actor="operator",
            reason="stale CAS must refuse",
        )
        assert kb.clear_terminal_state(conn, request) is False
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()) == before["task"]
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ] == before["events"]


def test_clear_terminal_state_refuses_non_done_status(kanban_home):
    board = "clear-terminal-non-done"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)
    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        before = {
            "task": tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
            "events": len(kb.list_events(conn, task_id)),
        }
        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=completed_at,
            expected_phase="development",
            expected_latest_event_id=event_id,
            actor="operator",
            reason="non-done must refuse",
        )
        assert kb.clear_terminal_state(conn, request) is False
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()) == before["task"]
        assert len(kb.list_events(conn, task_id)) == before["events"]


def test_clear_terminal_state_refuses_already_terminal_phase(kanban_home):
    board = "clear-terminal-terminal-phase"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board, phase="done")
    with kb.connect(board=board) as conn:
        before = {
            "task": tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
            "events": len(kb.list_events(conn, task_id)),
        }
        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=completed_at,
            expected_phase="done",
            expected_latest_event_id=event_id,
            actor="operator",
            reason="terminal phase must refuse",
        )
        assert kb.clear_terminal_state(conn, request) is False
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()) == before["task"]
        assert len(kb.list_events(conn, task_id)) == before["events"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("actor", ""), ("actor", "  "), ("reason", ""), ("reason", "  ")],
)
def test_clear_terminal_state_refuses_empty_actor_or_reason(kanban_home, field, value):
    board = f"clear-terminal-empty-{field}-{len(value)}"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)
    values = {
        "task_id": task_id,
        "expected_completed_at": completed_at,
        "expected_phase": "development",
        "expected_latest_event_id": event_id,
        "actor": "operator",
        "reason": "required reason",
    }
    values[field] = value
    with kb.connect(board=board) as conn:
        with pytest.raises(ValueError, match=field):
            kb.clear_terminal_state(conn, kb.ClearTerminalStateRequest(**values))


@pytest.mark.parametrize("step", sorted(kb.PRODUCT_WORKFLOW_STEP_SET))
def test_create_task_accepts_each_product_step(kanban_home, step):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Story: valid state",
            workflow_template_id="product",
            current_step_key=step,
        )
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == step


def test_create_task_infers_missing_product_step_from_explicit_intent(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Implementation work",
            assignee="developer",
            workflow_template_id="product",
        )
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == "development"


def test_create_task_allows_custom_workflow_step(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Custom flow",
            workflow_template_id="custom",
            current_step_key="bespoke-review",
        )
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == "bespoke-review"


def test_create_task_keeps_legacy_step_without_product_template(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy flow", current_step_key="in_progress")
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.workflow_template_id is None
    assert task.current_step_key == "in_progress"


def test_create_task_rejects_unknown_project(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="unknown project"):
            kb.create_task(conn, title="Lost governance", project_id="missing-project")


def test_decomposed_child_preserves_complete_execution_context(kanban_home):
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="Context root",
            body="root body",
            assignee="orchestrator",
            workspace_kind="worktree",
            workspace_path="/repo/.worktrees/root",
            branch_name="project/root",
            tenant="tenant-a",
            max_runtime_seconds=120,
            skills=["skill-a"],
            max_retries=2,
            model_override="model-a",
            provider_override="provider-a",
            reasoning_effort="high",
            goal_mode=True,
            goal_max_turns=7,
            workflow_template_id="custom",
            current_step_key="implementation",
            source_commit_required=True,
            source_commit_forbidden=False,
            triage=True,
        )
        conn.execute(
            "UPDATE tasks SET project_id = ?, work_contract_id = ? WHERE id = ?",
            ("project-a", "contract-a", root_id),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "Generated child",
                    "body": "child body",
                    "assignee": "developer",
                },
                {
                    "title": "Overridden workspace",
                    "assignee": "tester",
                    "workspace_kind": "dir",
                    "workspace_path": "/tmp/generated-child",
                },
            ],
        )

        child = kb.get_task(conn, child_ids[0])
        overridden = kb.get_task(conn, child_ids[1])

    assert child is not None
    assert child.title == "Generated child"
    assert child.body == "child body"
    assert child.assignee == "developer"
    assert child.project_id == "project-a"
    assert child.branch_name is None
    assert child.tenant == "tenant-a"
    assert child.workspace_kind == "worktree"
    assert child.workspace_path is None
    assert child.max_runtime_seconds == 120
    assert child.skills == ["skill-a"]
    assert child.max_retries == 2
    assert child.model_override == "model-a"
    assert child.provider_override == "provider-a"
    assert child.reasoning_effort == "high"
    assert child.goal_mode is True
    assert child.goal_max_turns == 7
    assert child.workflow_template_id == "custom"
    assert child.current_step_key == "implementation"
    # Contracts are one-to-one with cards; reusing the root contract would
    # violate idx_tasks_work_contract_unique.
    assert child.work_contract_id is None
    assert child.work_item_kind == "card"
    assert child.source_commit_required is True
    assert child.source_commit_forbidden is False
    assert overridden is not None
    assert overridden.assignee == "tester"
    assert overridden.workspace_kind == "dir"
    assert overridden.workspace_path == "/tmp/generated-child"


@pytest.mark.parametrize(
    ("step", "verdict", "target"),
    [
        ("test", "changes_requested", "development"),
        ("review", "changes_requested", "development"),
        ("review", "architecture_invalid", "architecture"),
    ],
)
def test_product_rejection_routes_backward(
    kanban_home, step, verdict, target
):
    board = f"rework-{step}-{target}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: rework",
            assignee="tester" if step == "test" else "reviewer",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="Needs revision",
            metadata={
                "workflow_outcome": {
                    "verdict": verdict,
                    "target_step": target,
                    "findings": ["Concrete finding"],
                }
            },
            expected_run_id=claimed.current_run_id,
            board=board,
            product_role_assignees={
                "developer": "custom-developer",
                "architect": "custom-architect",
            },
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.current_step_key == target
    assert task.rework_count == 1
    assert task.assignee == (
        "custom-developer" if target == "development" else "custom-architect"
    )
    assert any(event.kind == "rework_requested" for event in events)


def test_rework_directive_is_append_only_with_one_active_row(kanban_home):
    board = "rework-directive-append-only"
    _v2_product_board(board)
    rejected_sha = "a" * 40
    replacement_sha = "b" * 40
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: durable directive",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        first = kb.create_rework_directive(
            conn,
            tid,
            origin_kind="test",
            origin_run_id=11,
            origin_intent_key="intent-1",
            origin_phase="test",
            target_phase="development",
            rejected_branch="story/durable-directive",
            rejected_sha=rejected_sha,
            epic_tip_sha="c" * 40,
            findings=["first finding"],
        )
        assert first.status == "active"
        assert kb.active_rework_directive(conn, tid) == first

        second = kb.create_rework_directive(
            conn,
            tid,
            origin_kind="review",
            origin_run_id=12,
            origin_intent_key="intent-2",
            origin_phase="review",
            target_phase="development",
            rejected_branch="story/durable-directive",
            rejected_sha=replacement_sha,
            epic_tip_sha="d" * 40,
            findings=["replacement finding"],
        )
        rows = conn.execute(
            "SELECT status FROM product_rework_directives "
            "WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()

    assert second.status == "active"
    assert [row["status"] for row in rows] == ["superseded", "active"]


def test_rework_directive_routes_with_exact_sha_and_precedes_attempts(
    kanban_home,
):
    board = "rework-directive-context"
    _v2_product_board(board)
    rejected_sha = "e" * 40
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: visible rework",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid, board=board, claimer="tester")
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="Test rejected the candidate",
            metadata={
                "workflow_outcome": {
                    "verdict": "changes_requested",
                    "target_step": "development",
                    "findings": ["the candidate is incomplete"],
                },
                "rejected_branch": "story/visible-rework",
                "rejected_sha": rejected_sha,
                "epic_tip_sha": "f" * 40,
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        directive = kb.active_rework_directive(conn, tid)
        context = kb.build_worker_context(conn, tid)

    assert directive is not None
    assert directive.origin_phase == "test"
    assert directive.target_phase == "development"
    assert directive.rejected_branch == "story/visible-rework"
    assert directive.rejected_sha == rejected_sha
    assert directive.findings == ("the candidate is incomplete",)
    assert "## Required rework directive" in context
    assert rejected_sha in context
    assert context.index("## Required rework directive") < context.index(
        "## Prior attempts on this task"
    )


def test_rework_directive_resolves_only_after_new_development_sha(
    kanban_home, tmp_path
):
    board = "rework-directive-resolution"
    _v2_product_board(board)
    repo = tmp_path / "directive-repo"
    _init_git_repo(repo)
    rejected_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: resolve rework",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="main",
            board=board,
        )
        kb.create_rework_directive(
            conn,
            tid,
            origin_kind="test",
            origin_run_id=21,
            origin_phase="test",
            target_phase="development",
            rejected_branch="story/resolve-rework",
            rejected_sha=rejected_sha,
            findings=["fix the implementation"],
        )
        assert not kb.resolve_rework_directive(
            conn,
            tid,
            new_sha=rejected_sha,
            resolved_by_run_id=22,
        )
        assert kb.active_rework_directive(conn, tid) is not None

        claimed = kb.claim_task(conn, tid, board=board, claimer="developer")
        assert claimed is not None and claimed.current_run_id is not None
        (repo / "fixed.txt").write_text("fixed\n", encoding="utf-8")
        assert kb.handoff(
            conn,
            tid,
            board=board,
            summary="Implemented the requested fix",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
            expected_run_id=claimed.current_run_id,
            expected_phase="development",
        )
        resolved = conn.execute(
            "SELECT status, resolved_by_run_id FROM product_rework_directives "
            "WHERE task_id = ?",
            (tid,),
        ).fetchone()

    assert resolved["status"] == "resolved"
    assert resolved["resolved_by_run_id"] == claimed.current_run_id


@pytest.mark.parametrize(
    "findings",
    [[], "not-a-list", [""], [1], ["Concrete", ""]],
)
def test_product_rework_requires_nonempty_string_findings(kanban_home, findings):
    board = "rework-findings"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: rework",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        with pytest.raises(kb.ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                tid,
                summary="Needs revision",
                metadata={
                    "workflow_outcome": {
                        "verdict": "changes_requested",
                        "target_step": "development",
                        "findings": findings,
                    }
                },
                expected_run_id=claimed.current_run_id,
                board=board,
            )
        assert raised.value.code == "invalid_findings"
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == "test"


def test_product_rework_requires_expected_run_id(kanban_home):
    board = "rework-run-required"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: rework",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        with pytest.raises(ValueError, match="expected_run_id"):
            kb.complete_task(
                conn,
                tid,
                summary="Needs revision",
                metadata={
                    "workflow_outcome": {
                        "verdict": "changes_requested",
                        "target_step": "development",
                        "findings": ["Concrete finding"],
                    }
                },
                board=board,
            )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "running"
    assert task.current_step_key == "test"
    assert task.rework_count == 0
    assert task.current_run_id == claimed.current_run_id


@pytest.mark.parametrize(
    ("step", "verdict", "next_step", "provenance"),
    [
        (
            "test",
            "passed",
            "review",
            {"tester": {"agent": "hermes", "result": "passed"}},
        ),
        (
            "review",
            "approved",
            "release_measure",
            {
                "writer": {"agent": "claude-code"},
                "reviewer": {"agent": "codex"},
            },
        ),
    ],
)
def test_product_positive_rework_outcome_uses_forward_handoff(
    kanban_home, step, verdict, next_step, provenance
):
    board = f"rework-positive-{step}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: accepted",
            assignee="tester" if step == "test" else "reviewer",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="Accepted",
            metadata={
                "workflow_outcome": {"verdict": verdict},
                "ai_provenance": provenance,
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.current_step_key == next_step
    # handoff_v2 reads board role policy; the important contract is that a
    # positive outcome advances normally rather than incrementing rework.
    assert task.rework_count == 0
    assert any(event.kind == "handoff" for event in events)
    assert not any(event.kind == "rework_requested" for event in events)


@pytest.mark.parametrize(
    ("step", "verdict", "provenance"),
    [
        (
            "test",
            "approved",
            {"tester": {"agent": "hermes", "result": "passed"}},
        ),
        (
            "review",
            "passed",
            {
                "writer": {"agent": "claude-code"},
                "reviewer": {"agent": "codex"},
            },
        ),
    ],
)
def test_product_positive_rework_verdict_must_match_phase(
    kanban_home, step, verdict, provenance
):
    board = f"rework-positive-invalid-{step}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: invalid verdict",
            assignee="tester" if step == "test" else "reviewer",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        with pytest.raises(kb.ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                tid,
                summary="Wrong verdict",
                metadata={
                    "workflow_outcome": {"verdict": verdict},
                    "ai_provenance": provenance,
                },
                expected_run_id=claimed.current_run_id,
                board=board,
            )
        assert raised.value.code == "phase_mismatch"
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == step
    assert task.status == "running"


def test_product_positive_rework_rejects_same_run_phase_change_before_handoff(
    kanban_home, monkeypatch
):
    board = "rework-positive-phase-cas"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: phase-bound verdict",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None

        original_route = kb._route_product_rework_if_requested

        def route_then_change_phase(*args, **kwargs):
            routed = original_route(*args, **kwargs)
            assert routed is None
            assert kb.set_phase(conn, tid, "review", board=board)
            return routed

        monkeypatch.setattr(
            kb, "_route_product_rework_if_requested", route_then_change_phase
        )
        completed = kb.complete_task(
            conn,
            tid,
            summary="Test verdict must stay bound to Test",
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {
                    "tester": {"agent": "hermes", "result": "passed"},
                    "writer": {"agent": "claude-code"},
                    "reviewer": {"agent": "codex"},
                },
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert completed is False
    assert task is not None
    assert task.current_step_key == "review"
    assert task.status == "running"
    assert task.running is True
    assert task.current_run_id == claimed.current_run_id
    assert not any(event.kind == "handoff" for event in events)


def test_invalid_product_rework_does_not_commit_workflow_repair(kanban_home):
    board = "rework-invalid-no-repair"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: legacy tester card",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        conn.execute(
            "UPDATE tasks SET workflow_template_id=NULL, current_step_key=NULL "
            "WHERE id=?",
            (tid,),
        )
        conn.commit()

        with pytest.raises(ValueError, match="invalid workflow_outcome"):
            kb.complete_task(
                conn,
                tid,
                summary="Invalid route",
                metadata={
                    "workflow_outcome": {
                        "verdict": "architecture_invalid",
                        "target_step": "architecture",
                        "findings": ["Not valid from Test"],
                    }
                },
                expected_run_id=claimed.current_run_id,
                board=board,
            )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.workflow_template_id is None
    assert task.current_step_key is None
    assert not any(event.kind == "workflow_repaired" for event in events)


def test_fourth_product_rejection_routes_to_human_block(kanban_home):
    board = "rework-limit"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: bounded rework",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            board=board,
        )
        conn.execute("UPDATE tasks SET rework_count=3 WHERE id=?", (tid,))
        conn.commit()
        claimed = kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="Fourth rejection",
            metadata={
                "workflow_outcome": {
                    "verdict": "changes_requested",
                    "target_step": "development",
                    "findings": ["Still unsafe"],
                }
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.status == "blocked"
    assert task.blocked is True
    assert task.rework_count == 4
    blocked = [event for event in events if event.kind == "blocked"]
    assert blocked
    assert blocked[-1].payload["kind"] == "rework_limit"
    assert blocked[-1].payload["findings"] == ["Still unsafe"]


def _route_task_to_resolver(
    conn, board: str, *, step: str = "development"
) -> tuple[str, int]:
    assignee = {
        "development": "developer",
        "test": "tester",
        "review": "reviewer",
    }[step]
    tid = kb.create_task(
        conn,
        title="Story: resolver",
        assignee=assignee,
        workflow_template_id="product",
        current_step_key=step,
        board=board,
    )
    return _route_existing_task_to_resolver(conn, tid, board, step=step)


def _route_existing_task_to_resolver(
    conn, task_id: str, board: str, *, step: str = "development"
) -> tuple[str, int]:
    tid = task_id
    first = kb.claim_task(conn, tid)
    assert first is not None and first.current_run_id is not None
    assert kb.block_task(
        conn,
        tid,
        reason="Need a decision",
        kind="needs_input",
        attempted_resolutions=["read docs"],
        expected_run_id=first.current_run_id,
        board=board,
        human_escalation_assignee="resolver",
    )
    if step == "review":
        # Review is a visible legacy column status, while the resolver must be
        # claimable as ready work. Model the dispatcher promotion explicitly.
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=? AND assignee='resolver'",
            (tid,),
        )
        conn.commit()
    resolver = kb.claim_task(conn, tid)
    assert resolver is not None and resolver.current_run_id is not None
    return tid, resolver.current_run_id


def _resolver_expected(conn, task_id: str, run_id: int) -> dict:
    task = kb.get_task(conn, task_id)
    assert task is not None
    preflight = [
        event for event in kb.list_events(conn, task_id)
        if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT
    ][-1]
    return {
        "run_id": run_id,
        "preflight_event_id": preflight.id,
        "status": task.status,
        "phase": task.current_step_key,
        "assignee": task.assignee,
        "project_id": task.project_id,
        "workflow_template_id": task.workflow_template_id,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "running": task.running,
        "blocked": task.blocked,
    }


def _resolver_request(expected: dict, decision: str = "resume", **extra) -> dict:
    request = {
        "decision": decision,
        "fault_domain": "task_state",
        "diagnosis": "The task-local workflow state is recoverable",
        "reason": "Resume the displaced ordinary worker",
        "expected": expected,
    }
    request.update(extra)
    return request


def _resolve_preflight(
    conn,
    task_id: str,
    run_id: int,
    board: str,
    *,
    decision: str = "resume",
    reason: str = "Use the configured recovery path",
    fault_domain: str = "task_state",
) -> bool:
    request = _resolver_request(
        _resolver_expected(conn, task_id, run_id),
        decision=decision,
        fault_domain=fault_domain,
        reason=reason,
    )
    task = kb.get_task(conn, task_id)
    assert task is not None and task.assignee
    return kb.resolve_product_preflight(
        conn,
        task_id,
        board=board,
        request=request,
        resolver_profile=task.assignee,
        resolver_model="test-model",
    )


def _resolver_state(conn, task_id: str) -> dict:
    return {
        "task": tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()),
        "runs": [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        ],
        "events": [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        ],
        "links": [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_links WHERE parent_id=? OR child_id=? "
                "ORDER BY parent_id, child_id",
                (task_id, task_id),
            ).fetchall()
        ],
    }


def _full_tables_state(conn) -> dict:
    """Deterministic full-table snapshot of every kanban graph table.

    Unlike ``_resolver_state`` this is not scoped to one task id, so it
    proves a rejected request mutated *nothing* board-wide (no stray fix
    task, run, event, or link anywhere).
    """
    order_by = {
        "tasks": "id",
        "task_runs": "id",
        "task_events": "id",
        "task_links": "parent_id, child_id",
    }
    return {
        table: [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY {order}"  # noqa: S608 — fixed identifiers
            ).fetchall()
        ]
        for table, order in order_by.items()
    }


def test_create_task_refuses_resolver_assignee(kanban_home):
    """A card cannot be authored into the privileged Resolver lane.

    A brand-new card has no preflight, so the Resolver it would spawn
    would hold `resolver_readonly` and no lifecycle exit at all.
    """
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="resolver"):
            kb.create_task(conn, title="Ordinary goal", assignee="resolver")
        assert kb.list_tasks(conn) == []


def test_assign_task_refuses_resolver_without_unresolved_preflight(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Ordinary goal", assignee="developer")
        with pytest.raises(ValueError, match="resolver"):
            kb.assign_task(conn, tid, "resolver")
        task = kb.get_task(conn, tid)
    assert task is not None and task.assignee == "developer"


def test_assign_task_allows_resolver_on_unresolved_preflight(kanban_home):
    """The valid path: a product card already displaced to a preflight."""
    board = "resolver-assign-valid"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _run_id = _route_task_to_resolver(conn, board)
        assert kb.has_unresolved_product_preflight(conn, tid)
        conn.execute(
            "UPDATE tasks SET claim_lock=NULL, status='ready' WHERE id=?", (tid,)
        )
        conn.commit()
        assert kb.assign_task(conn, tid, "resolver")
        task = kb.get_task(conn, tid)
    assert task is not None and task.assignee == "resolver"


def test_dispatch_refuses_resolver_routing_before_creating_a_run(
    kanban_home, all_assignees_spawnable
):
    """The 2026-08-02 deadlock: an ordinary goal card assigned to ``resolver``
    spawned a worker with no lifecycle exit and hung.

    The refusal lives in the claim transaction, so an incompatible card never
    becomes a run at all — no run row, no ``claimed`` event, no subprocess.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Ordinary goal", assignee="developer")
        # Bypass the routing guard the way the incident's cards got there.
        conn.execute("UPDATE tasks SET assignee='resolver' WHERE id=?", (tid,))
        conn.commit()

        dry = kb.dispatch_once(conn, spawn_fn=fake_spawn, dry_run=True)
        assert tid not in [row[0] for row in dry.spawned]
        assert tid in dry.skipped_nonspawnable

        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)
        ).fetchone()[0]
        kinds = [event.kind for event in kb.list_events(conn, tid)]
        error = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id=?", (tid,)
        ).fetchone()[0]

    assert tid not in spawned_ids
    assert tid not in [row[0] for row in res.spawned]
    assert task is not None and task.status == "blocked"
    assert runs == 0
    assert "claimed" not in kinds
    assert "claim_rejected" in kinds
    assert "resolver" in (error or "") and "preflight" in (error or "")


def test_dispatch_allows_resolver_on_unresolved_preflight(
    kanban_home, all_assignees_spawnable
):
    board = "resolver-dispatch-valid"
    _v2_product_board(board)
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect(board=board) as conn:
        tid, _run_id = _route_task_to_resolver(conn, board)
        conn.execute(
            "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL, "
            "current_run_id=NULL, status='ready', running=0 WHERE id=?",
            (tid,),
        )
        conn.commit()
        kb.dispatch_once(conn, spawn_fn=fake_spawn, board=board)

    assert spawned_ids == [tid]


def test_complete_task_refuses_unresolved_preflight_without_mutation(kanban_home):
    board = "resolver-complete-refusal"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="use kanban_resolve"):
            kb.complete_task(
                conn,
                tid,
                summary="Ordinary completion must not resolve preflight",
                expected_run_id=run_id,
                board=board,
            )
        assert _resolver_state(conn, tid) == before


@pytest.mark.parametrize(
    ("step", "metadata", "code"),
    [
        ("test", None, "missing"),
        (
            "review",
            {"workflow_outcome": {"verdict": "approved", "unexpected": True}},
            "invalid_shape",
        ),
    ],
)
def test_complete_task_rejects_unresolved_test_review_without_mutation(
    kanban_home, step, metadata, code
):
    """Ordinary completion validates Test/Review before the Resolver path."""
    board = f"resolver-complete-outcome-{step}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board, step=step)
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                tid,
                summary="Ordinary completion without a canonical outcome",
                metadata=metadata,
                expected_run_id=run_id,
                board=board,
            )
        after = _resolver_state(conn, tid)

    assert raised.value.code == code
    assert after["task"] == before["task"]
    assert after["runs"] == before["runs"]
    assert after["links"] == before["links"]
    assert after["events"][:-1] == before["events"]
    rejection = [
        event for event in after["events"]
        if event[3] == "completion_rejected_outcome"
    ]
    assert len(rejection) == 1
    assert json.loads(rejection[0][4]) == {
        "run_id": run_id,
        "phase": step,
        "code": code,
    }


def test_resolve_product_preflight_resume_uses_complete_snapshot(kanban_home):
    board = "resolver-entry-resume"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(_resolver_expected(conn, tid, run_id))
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        task = kb.get_task(conn, tid)
        run = kb.get_run(conn, run_id)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "developer"
    assert task.current_run_id is None
    assert run is not None and run.ended_at is not None
    assert events[-1].kind == "human_input_preflight_resolved"


def test_resolve_product_preflight_rejects_legacy_fix_task_shape_without_mutation(kanban_home):
    """The legacy create_fix_task resolver shape is dead: reject, zero mutation.

    Resolver is a task-local repair/preflight resolver only — it cannot
    create or link work. Even a well-formed legacy request (a real fix task
    already linked as a child, exactly the shape the old contract accepted)
    must be rejected, leaving tasks / task_runs / task_events / task_links
    byte-for-byte unchanged.
    """
    board = "resolver-legacy-fix-shape"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        fix_id = kb.create_task(
            conn,
            title="Fix the blocker",
            assignee="developer",
            created_by="resolver",
            parents=[tid],
            board=board,
        )
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="create_fix_task",
            fix_task_id=fix_id,
        )
        before = _full_tables_state(conn)
        with pytest.raises(
            ValueError,
            match="decision must be resume, repair, or escalate",
        ):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _full_tables_state(conn) == before


def test_resolve_product_preflight_escalates_without_completion_gate(kanban_home):
    board = "resolver-entry-escalate"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="escalate",
            fault_domain="framework",
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None and task.status == "blocked" and task.blocked
    assert any(event.kind == "blocked" for event in events)


def test_resolve_product_preflight_rejects_stale_event_with_zero_mutation(kanban_home):
    board = "resolver-stale-event"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        expected = _resolver_expected(conn, tid, run_id)
        expected["preflight_event_id"] += 1
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(expected),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolve_product_preflight_rejects_changed_snapshot_field_with_zero_mutation(kanban_home):
    board = "resolver-stale-field"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        expected = _resolver_expected(conn, tid, run_id)
        expected["branch_name"] = "changed-after-inspection"
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(expected),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolve_product_preflight_rejects_wrong_run_profile(kanban_home):
    board = "resolver-wrong-run-profile"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        conn.execute("UPDATE task_runs SET profile='developer' WHERE id=?", (run_id,))
        conn.commit()
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(_resolver_expected(conn, tid, run_id)),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolve_product_preflight_rejects_preflight_routed_to_other_profile(
    kanban_home,
):
    board = "resolver-wrong-preflight-profile"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        preflight = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (tid, kb.PRODUCT_WORKFLOW_PRECHECK_EVENT),
        ).fetchone()
        payload = json.loads(preflight["payload"])
        payload["hermes_assignee"] = "architect"
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), preflight["id"]),
        )
        conn.commit()
        before = _resolver_state(conn, tid)

        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(_resolver_expected(conn, tid, run_id)),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_rejects_overbound_governed_assignee_atomically(kanban_home):
    board = "resolver-repair-assignee-bound"
    _v2_product_board(board)
    metadata = kb.read_board_metadata(board)
    metadata.setdefault("product_workflow", {}).setdefault("assignees", {})["developer"] = "D" * 300
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")

    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "development"}},
        )
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="assignee"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model="test-model",
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_rejects_overbound_derived_workspace_path_atomically(
    kanban_home, tmp_path
):
    from hermes_cli import projects_db as pdb

    board = "resolver-repair-workspace-bound"
    _v2_product_board(board)
    long_primary_path = str(tmp_path / ("p" * 4100))
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Long Resolver Project",
            primary_path=long_primary_path,
            board_slug=board,
        )

    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"project_id": project_id}},
        )
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="workspace_path"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model="test-model",
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_workflow_repair_is_atomic_and_returns_to_ordinary_role(kanban_home):
    board = "resolver-workflow-repair"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "test", "assignee": "tester"}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == "test"
    assert task.assignee == "tester"
    assert task.status == "ready"
    assert not task.running and not task.blocked
    assert task.current_run_id is None


def test_resolver_repair_requires_at_least_one_semantic_field(kanban_home):
    board = "resolver-empty-repair"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="repair"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_rejects_unknown_project(kanban_home):
    board = "resolver-unknown-project"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="unknown project"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"workflow": {"project_id": "missing-project"}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_derives_project_worktree_and_branch(kanban_home, tmp_path):
    from hermes_cli import projects_db as pdb

    board = "resolver-project-repair"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board(board)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Resolver Project",
            primary_path=str(repo),
            board_slug=board,
        )
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"project_id": project_id}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.project_id == project_id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(repo / ".worktrees" / tid)
    assert task.branch_name.startswith("resolver-project/")


def test_resolver_project_adoption_preserves_existing_canonical_worktree(
    kanban_home, tmp_path, monkeypatch
):
    board = "resolver-project-adoption"
    shared_board_home = tmp_path / "shared-board-root"
    shared_board_home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(shared_board_home))
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    resolver_home = kanban_home / "profiles" / "resolver"
    resolver_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(resolver_home))
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Legacy project adoption",
            assignee="developer",
            workspace_kind="worktree",
            board=board,
        )
        existing_workspace = repo / ".worktrees" / tid
        existing_workspace.mkdir(parents=True)
        kb.set_workspace_path(conn, tid, existing_workspace)
        kb.set_branch_name(conn, tid, f"wt/{tid}")
        _tid, run_id = _route_existing_task_to_resolver(conn, tid, board)

        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=_resolver_request(
                _resolver_expected(conn, tid, run_id),
                decision="repair",
                repair={"workflow": {"project_id": project_id}},
            ),
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.project_id == project_id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(existing_workspace)
    assert task.branch_name == f"wt/{tid}"


def test_resolver_project_adoption_rewrites_unsafe_workspace_path(
    kanban_home, tmp_path, monkeypatch
):
    from hermes_cli import projects_db as pdb

    board = "resolver-project-unsafe-adoption"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Unsafe project adoption",
            assignee="developer",
            workspace_kind="worktree",
            board=board,
        )
        monkeypatch.chdir(repo)
        unsafe_workspace = Path(".worktrees") / tid
        kb.set_workspace_path(conn, tid, unsafe_workspace)
        kb.set_branch_name(conn, tid, f"wt/{tid}")
        _tid, run_id = _route_existing_task_to_resolver(conn, tid, board)

        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=_resolver_request(
                _resolver_expected(conn, tid, run_id),
                decision="repair",
                repair={"workflow": {"project_id": project_id}},
            ),
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)
    with pdb.connect_closing() as project_conn:
        project = pdb.get_project(project_conn, project_id)

    assert task is not None
    assert project is not None
    assert task.project_id == project_id
    assert task.workspace_path == str(repo / ".worktrees" / tid)
    assert task.branch_name == f"{project.slug}/{tid}-unsafe-project-adoption"


def test_resolver_repair_rejects_phase_assignee_mismatch(kanban_home):
    board = "resolver-role-mismatch"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="assignee"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"workflow": {"phase": "test", "assignee": "developer"}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


@pytest.mark.parametrize("phase", ["release_measure", "done", "archived"])
def test_resolver_repair_cannot_target_release_done_or_archived(kanban_home, phase):
    board = f"resolver-terminal-{phase}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="phase"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"workflow": {"phase": phase}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_framework_fault_can_only_escalate(kanban_home):
    board = "resolver-framework-only-escalate"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        with pytest.raises(ValueError, match="must escalate"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    fault_domain="framework",
                    repair={"workflow": {"phase": "development"}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )


def test_resolver_repair_does_not_change_test_or_review_runs(kanban_home):
    board = "resolver-preserve-runs"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board, step="test")
        prior_before = [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND id<>? ORDER BY id",
                (tid, run_id),
            ).fetchall()
        ]
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "review", "assignee": "reviewer"}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model=None,
        )
        prior_after = [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND id<>? ORDER BY id",
                (tid, run_id),
            ).fetchall()
        ]
    assert prior_after == prior_before


def test_successful_repair_appends_audit_and_needs_ole_events(kanban_home):
    board = "resolver-repair-audit"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "architecture", "assignee": "architect"}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        events = kb.list_events(conn, tid)
    audit = [event for event in events if event.kind == "resolver_repair_applied"][-1]
    attention = [event for event in events if event.kind == "needs_ole"][-1]
    assert audit.payload["fault_domain"] == "task_state"
    assert audit.payload["resolver"] == {
        "profile": "resolver",
        "model": "test-model",
        "run_id": run_id,
    }
    assert set(audit.payload["before"]) <= {
        "phase", "assignee", "project_id", "status", "workspace_kind",
        "workspace_path", "branch_name", "adopt_handoff_sha",
    }
    assert attention.payload["reason"] == "resolver_repair"


@pytest.mark.parametrize(
    "forbidden",
    [
        {"task_links": []},
        {"epic_memberships": []},
        {"work_contract": {"phase": "development"}},
    ],
)
def test_resolver_cannot_change_epic_membership_or_dependencies(
    kanban_home, forbidden,
):
    board = "resolver-immutable-relations"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        parent = kb.create_task(conn, title="Dependency", board=board)
        kb.link_tasks(conn, parent, tid)
        assert kb.parent_ids(conn, tid) == [parent]
        before = _resolver_state(conn, tid)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "development"}},
        )
        request.update(forbidden)
        with pytest.raises(ValueError, match="unexpected"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_cannot_override_release_classification(kanban_home):
    board = "resolver-immutable-release"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "development"}},
            release_path="standalone",
        )
        with pytest.raises(ValueError, match="unexpected"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def _route_project_task_with_audited_handoff(
    conn, board: str, repo: Path, project_id: str,
) -> tuple[str, int, Path, str]:
    tid = kb.create_task(
        conn,
        title="Story: adopted handoff",
        assignee="developer",
        workflow_template_id="product",
        current_step_key="development",
        project_id=project_id,
        board=board,
    )
    task = kb.get_task(conn, tid)
    assert task is not None
    workspace = kb.resolve_workspace(task, board=board)
    kb.set_workspace_path(conn, tid, workspace)
    task = kb.get_task(conn, tid)
    assert task is not None and task.branch_name
    sha = _commit_file(workspace, "feature.py", "value = 1\n", "feature")
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            tid,
            "handoff",
            {
                "from_step": "development",
                "to_step": "test",
                "sha": sha,
                "assignee": "tester",
                "summary": "Previously committed Development work",
            },
        )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    assert kb.block_task(
        conn,
        tid,
        reason="The committed handoff was not adopted",
        kind="needs_input",
        attempted_resolutions=["verified task branch"],
        expected_run_id=claimed.current_run_id,
        board=board,
        human_escalation_assignee="resolver",
    )
    resolver = kb.claim_task(conn, tid)
    assert resolver is not None and resolver.current_run_id is not None
    return tid, resolver.current_run_id, workspace, sha


def _resolver_project_fixture(kanban_home, tmp_path, board: str):
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board(board)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Adopted Handoff",
            primary_path=str(repo),
            board_slug=board,
        )
    return repo, project_id


def test_adopt_handoff_sha_requires_same_task_development_handoff_event(
    kanban_home, tmp_path,
):
    board = "resolver-adopt-same-task"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kb.connect(board=board) as conn:
        tid, run_id, _workspace, sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        conn.execute(
            "DELETE FROM task_events WHERE task_id=? AND kind='handoff'", (tid,),
        )
        conn.commit()
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="Development handoff"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"adopt_handoff_sha": sha},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_adopt_handoff_sha_requires_current_project_branch_head(
    kanban_home, tmp_path,
):
    board = "resolver-adopt-current-head"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kb.connect(board=board) as conn:
        tid, run_id, workspace, old_sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        _commit_file(workspace, "later.py", "value = 2\n", "later")
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="branch HEAD"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"adopt_handoff_sha": old_sha},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_development_handoff_uses_valid_adopted_sha_when_tree_is_clean(
    kanban_home, tmp_path,
):
    board = "resolver-adopt-clean-handoff"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kb.connect(board=board) as conn:
        tid, run_id, workspace, sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=_resolver_request(
                _resolver_expected(conn, tid, run_id),
                decision="repair",
                repair={"adopt_handoff_sha": sha},
            ),
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        assert subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout == ""
        assert kb.handoff(
            conn,
            tid,
            board=board,
            summary="Adopt the already committed Development handoff",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        task = kb.get_task(conn, tid)
        handoffs = [event for event in kb.list_events(conn, tid) if event.kind == "handoff"]
    assert task is not None and task.current_step_key == "test"
    assert handoffs[-1].payload["sha"] == sha


def test_invalid_adopted_sha_leaves_task_and_git_untouched(kanban_home, tmp_path):
    board = "resolver-adopt-invalid-atomic"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kb.connect(board=board) as conn:
        tid, run_id, workspace, _sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        before = _resolver_state(conn, tid)
        head_before = _head_sha(workspace)
        status_before = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        with pytest.raises(ValueError, match="Development handoff"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"adopt_handoff_sha": "0" * 40},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before
        assert _head_sha(workspace) == head_before
        assert subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout == status_before


@pytest.mark.parametrize("step", ["test", "review"])
def test_product_preflight_resolver_validation_precedes_rework(
    kanban_home, step
):
    board = f"resolver-before-rework-{step}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board, step=step)
        target = "development"
        with pytest.raises(ValueError, match="kanban_resolve"):
            kb.complete_task(
                conn,
                tid,
                summary="Trying to bypass resolver",
                metadata={
                    "workflow_outcome": {
                        "verdict": "changes_requested",
                        "target_step": target,
                        "findings": ["Workflow finding"],
                    }
                },
                expected_run_id=run_id,
                board=board,
            )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == step
    assert task.status == "running"
    assert task.rework_count == 0
    assert task.current_run_id == run_id


def test_product_preflight_requires_structured_resolver_action(kanban_home):
    board = "resolver-required"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        context = kb.build_worker_context(conn, tid)
        assert "## Required resolver action" in context
        assert "Original blocker:" in context
        assert "Attempted resolutions:" in context
        assert "Board policy:" in context
        assert "Resolve only with kanban_resolve" in context
        with pytest.raises(ValueError, match="kanban_resolve"):
            kb.complete_task(
                conn,
                tid,
                summary="I think it is fine",
                expected_run_id=run_id,
                board=board,
            )


@pytest.mark.parametrize(
    "request_mutation",
    [
        {"unexpected": True},
        {"fix_task_id": "t_not_allowed_for_resume"},
        {"diagnosis": ""},
    ],
)
def test_product_preflight_resolver_action_requires_exact_shape(
    kanban_home, request_mutation
):
    board = "resolver-exact-shape"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(_resolver_expected(conn, tid, run_id))
        request.update(request_mutation)
        with pytest.raises(ValueError, match="resolver request|diagnosis"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == run_id


def test_product_preflight_resume_restores_original_step(kanban_home):
    board = "resolver-resume"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        assert _resolve_preflight(
            conn, tid, run_id, board,
            reason="Use the configured test token source",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == "development"
    assert task.assignee == "developer"
    assert task.status == "ready"
    assert task.running is False
    assert task.blocked is False


def test_product_preflight_escalate_enters_human_block(kanban_home):
    board = "resolver-escalate"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        assert _resolve_preflight(
            conn, tid, run_id, board,
            decision="escalate",
            fault_domain="framework",
            reason="Docs and local config are insufficient",
        )
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None and task.status == "blocked" and task.blocked is True
    blocked = [event for event in events if event.kind == "blocked"]
    assert blocked
    assert blocked[-1].payload["kind"] == "resolver_escalation"
    assert blocked[-1].payload["resolution"] == "Docs and local config are insufficient"


def test_story_title_infers_product_without_role_on_product_board(kanban_home):
    board = "story-intent"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(conn, title="Story: explicit user intent", board=board)
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.workflow_template_id == "product"
    assert task.current_step_key == "backlog"


def test_set_phase_v2_board_updates_step_and_syncs_status(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-phase"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        result = kb.set_phase(conn, tid, "review", board=board)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["current_step_key"] == "review"
    assert row["status"] == "review"
    assert row["status"] == kb._legacy_status(row, meta)


def test_set_running_v2_board_sets_flag_and_syncs_status(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        result = kb.set_running(conn, tid, True, board=board)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["running"] == 1
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta)


def test_set_blocked_v2_board_sets_flag_and_syncs_status(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-blocked"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        result = kb.set_blocked(conn, tid, True, board=board, reason="waiting on human")
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["blocked"] == 1
    assert row["status"] == "blocked"
    assert row["status"] == kb._legacy_status(row, meta)


def test_set_blocked_false_clears_back_to_phase_status(kanban_home, monkeypatch):
    """set_blocked(False) after a block clears back to the phase's base status."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-unblock"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        kb.set_blocked(conn, tid, True, board=board)
        result = kb.set_blocked(conn, tid, False, board=board)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["blocked"] == 0
    assert row["status"] == "ready"


# NOTE: test_set_running_and_blocked_precedence_matches_legacy_status (T1.3)
# is superseded by T1.4's _assert_card_consistent invariant: a card can no
# longer be both running and blocked, so that scenario now raises + rolls
# back instead of committing "blocked" precedence. See
# test_set_blocked_after_running_raises_limbo_and_leaves_card_unchanged below,
# which covers the identical setup and asserts the new behavior.


def test_set_phase_legacy_board_is_noop(kanban_home):
    """Legacy (non-v2) boards must be byte-for-byte unchanged."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        result = kb.set_phase(conn, tid, "review")
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert result is False
    assert after == before


def test_set_running_legacy_board_is_noop(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        result = kb.set_running(conn, tid, True)
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert result is False
    assert after == before


def test_set_blocked_legacy_board_is_noop(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        result = kb.set_blocked(conn, tid, True, reason="whatever")
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert result is False
    assert after == before


def test_set_phase_product_board_without_handoff_v2_is_noop(kanban_home, monkeypatch):
    """A product-preset board that has NOT opted into handoff_v2 also no-ops."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-no-v2"
    kb.create_board(board, name="Product No V2", preset="product")
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="Story", workflow_template_id="product", current_step_key="development",
        )
        result = kb.set_phase(conn, tid, "review", board=board)
        row = conn.execute(
            "SELECT current_step_key FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert result is False
    assert row["current_step_key"] == "development"


def test_set_phase_missing_task_returns_false(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-missing-phase"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        result = kb.set_phase(conn, "does-not-exist", "review", board=board)
    assert result is False


def test_set_running_missing_task_returns_false(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-missing-running"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        result = kb.set_running(conn, "does-not-exist", True, board=board)
    assert result is False


def test_set_blocked_missing_task_returns_false(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-missing-blocked"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        result = kb.set_blocked(conn, "does-not-exist", True, board=board)
    assert result is False


# ---------------------------------------------------------------------------
# _assert_card_consistent invariant -- limbo is unrepresentable (T1.4)
# ---------------------------------------------------------------------------

def test_assert_card_consistent_running_only_is_valid():
    assert kb._assert_card_consistent({"running": 1, "blocked": 0}) is None


def test_assert_card_consistent_blocked_only_is_valid():
    assert kb._assert_card_consistent({"running": 0, "blocked": 1}) is None


def test_assert_card_consistent_neither_is_valid():
    assert kb._assert_card_consistent({"running": 0, "blocked": 0}) is None


def test_assert_card_consistent_running_and_blocked_raises():
    with pytest.raises(ValueError, match="running and blocked"):
        kb._assert_card_consistent({"running": 1, "blocked": 1})


def test_set_blocked_after_running_raises_limbo_and_leaves_card_unchanged(kanban_home, monkeypatch):
    """A running card cannot also become blocked: set_blocked(True) must
    raise ValueError, and (because the assert runs inside write_txn) the
    transaction rolls back -- the card is byte-for-byte unchanged."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-limbo-running-then-blocked"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        kb.set_running(conn, tid, True, board=board)
        before = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

        with pytest.raises(ValueError, match="running and blocked"):
            kb.set_blocked(conn, tid, True, board=board)

        after = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert after == before
    assert after["running"] == 1
    assert after["blocked"] == 0


def test_set_running_after_blocked_raises_limbo_and_leaves_card_unchanged(kanban_home, monkeypatch):
    """Symmetric case: a blocked card cannot also become running."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-limbo-blocked-then-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        kb.set_blocked(conn, tid, True, board=board)
        before = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

        with pytest.raises(ValueError, match="running and blocked"):
            kb.set_running(conn, tid, True, board=board)

        after = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert after == before
    assert after["blocked"] == 1
    assert after["running"] == 0


# ---------------------------------------------------------------------------
# Task creation + status inference
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Links + dependency resolution
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Atomic claim (CAS)
# ---------------------------------------------------------------------------



def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        assert any(e.kind == "scheduled" and e.payload == {"reason": "run next week"} for e in events)








def test_stale_claim_reclaim_event_records_diagnostic_payload(
    kanban_home, monkeypatch,
):
    """``reclaimed`` events should carry claim_expires, last_heartbeat_at,
    and worker_pid so operators can diagnose why a claim went stale
    (#23025: previous payload only had ``stale_lock`` which gives no
    timing context)."""
    import json
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 3600
        hb_at = int(time.time()) - 1800
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, hb_at, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (t,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["claim_expires"] == old_expires
        assert payload["last_heartbeat_at"] == hb_at
        assert payload["worker_pid"] == 12345
        assert payload["host_local"] is True






# ---------------------------------------------------------------------------
# Rate-limit requeue: a worker that bails on a provider quota wall must be
# released back to ``ready`` WITHOUT counting a failure, so a long (e.g.
# 5-hour) quota window can't trip the circuit breaker and permanently block
# the card. The respawn guard then defers it on a cooldown until quota
# returns. Regression coverage for the kanban-rate-limit-failure report.
# ---------------------------------------------------------------------------


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8




def test_rate_limit_exit_requeues_without_counting_failure(
    kanban_home, monkeypatch,
):
    """A rate-limit sentinel exit releases the task to ``ready`` and leaves
    ``consecutive_failures`` untouched — the breaker must never trip on a
    transient throttle, even across many quota-wall hits."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")

        # Simulate FAR more quota-wall hits than DEFAULT_FAILURE_LIMIT (2).
        # If any of these counted as a failure the task would be blocked.
        for i in range(6):
            pid = 70000 + i
            # Claim to open a real run (so detect_crashed_workers can close
            # it with a rate_limited outcome), then point the claim at this
            # host + a dead pid so the crash path acts on it.
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? "
                "WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            _kb._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            )

            crashed = kb.detect_crashed_workers(conn)
            # Rate-limited requeues are NOT crashes.
            assert tid not in crashed
            rl = getattr(_kb.detect_crashed_workers, "_last_rate_limited", [])
            assert tid in rl

            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.consecutive_failures == 0, (
                f"hit {i}: rate-limit must not count a failure, "
                f"got {task.consecutive_failures}"
            )

        # Last failure error stamped so the respawn guard recognizes the
        # quota wall.
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # A ``rate_limited`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "rate_limited" in outcomes
        assert "crashed" not in outcomes




def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, tid) is None








# ---------------------------------------------------------------------------
# Complete / block / unblock / archive / assign
# ---------------------------------------------------------------------------





@pytest.mark.parametrize(
    ("step", "expected_assignee"),
    [
        ("architecture", "architect"),
        ("development", "developer"),
        ("test", "tester"),
    ],
)
def test_unblock_restores_product_step_assignee(
    kanban_home, step, expected_assignee
):
    board = f"unblock-restores-{step}"
    kb.ensure_product_board_defaults(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Escalated card",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == expected_assignee


def test_unblock_keeps_release_measure_unassigned(kanban_home):
    board = "unblock-keeps-release-unassigned"
    kb.ensure_product_board_defaults(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Release gate",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="release_measure",
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee is None


def test_unblock_does_not_clear_unmapped_executable_phase(kanban_home):
    board = "unblock-keeps-unmapped-executable-phase"
    kb.ensure_product_board_defaults(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Temporarily unmapped phase",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="development",
            board=board,
        )
        meta_path = kb.board_metadata_path(board)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["qualification"]["required"] = True
        meta["qualification"]["phase_assignees"]["development"] = None
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "default"


def test_unblock_keeps_custom_workflow_assignee_on_product_board(kanban_home):
    board = "unblock-keeps-custom-workflow-route"
    kb.ensure_product_board_defaults(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Custom workflow task",
            assignee="custom-worker",
            initial_status="blocked",
            workflow_template_id="custom",
            current_step_key="development",
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "custom-worker"


def test_unblock_uses_custom_non_strict_product_assignee(kanban_home):
    board = "unblock-custom-product-route"
    kb.ensure_product_board_defaults(board)
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["product_workflow"]["assignees"]["developer"] = "custom-developer"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Custom-routed product task",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="development",
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "custom-developer"


def test_unblock_keeps_non_product_assignee_unchanged(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Generic blocked task",
            assignee="default",
            initial_status="blocked",
            current_step_key="development",
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "default"


def test_unblock_resets_failure_counters(kanban_home):
    """unblock_task must reset consecutive_failures and last_failure_error."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        # Simulate accumulated failures from the circuit breaker
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 5, "
            "last_failure_error = 'test error' WHERE id = ?",
            (t,),
        )
        conn.commit()
        assert kb.unblock_task(conn, t)
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_skips_tasks_at_failure_limit(kanban_home):
    """recompute_ready must not auto-recover tasks whose consecutive_failures
    has reached the circuit-breaker limit (#35072).

    Without this guard, a task that repeatedly exhausts its iteration
    budget would cycle forever: block → auto-recover (counter reset)
    → respawn → budget exhausted → block → …
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a",
                               parents=[parent])
        # Complete the parent so the child's dependencies are satisfied.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="done")

        # Simulate the child having exhausted its budget twice,
        # hitting the default failure limit (2).
        kb.claim_task(conn, child)
        kb._record_task_failure(
            conn, child, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        kb._record_task_failure(
            conn, child, error="budget exhausted 2",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.consecutive_failures >= 2

        # recompute_ready must NOT promote this task — the circuit
        # breaker has tripped and it should stay blocked.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"

        # Explicit unblock should still work and reset the counter.
        assert kb.unblock_task(conn, child)
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_recompute_ready_recovers_below_limit(kanban_home):
    """recompute_ready auto-recovers blocked tasks that haven't hit the
    failure limit yet — the counter is preserved across recovery."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="task", assignee="a")
        kb.claim_task(conn, t)
        # One failure, below the default limit of 2.
        kb._record_task_failure(
            conn, t, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Simulate being blocked by something else (not circuit breaker).
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        # Counter must be preserved, not reset.
        assert task.consecutive_failures == 1


def test_recompute_ready_honours_dispatcher_failure_limit(kanban_home):
    """The guard's effective limit must follow the same resolution order
    as the circuit breaker (#35072): per-task max_retries → dispatcher
    failure_limit → DEFAULT_FAILURE_LIMIT.

    Without threading the dispatcher's ``kanban.failure_limit`` through,
    the guard falls back to DEFAULT_FAILURE_LIMIT and disagrees with the
    breaker — sticking a task prematurely (config limit > default) or
    letting a tripped task escape (config limit < default).
    """
    with kb.connect() as conn:
        # Config allows MORE retries than the default. A task blocked
        # with failures below the configured limit must still recover.
        t = kb.create_task(conn, title="lenient", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=? "
            "WHERE id=?",
            (kb.DEFAULT_FAILURE_LIMIT, t),
        )
        conn.commit()
        # Default-limit call would stick it (failures >= default).
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"
        # Dispatcher configured a higher limit → recover, preserve counter.
        promoted = kb.recompute_ready(
            conn, failure_limit=kb.DEFAULT_FAILURE_LIMIT + 2
        )
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT

        # Config allows FEWER retries than the default. A task at the
        # stricter limit must stay blocked even though it's below default.
        t2 = kb.create_task(conn, title="strict", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (t2,),
        )
        conn.commit()
        # Default-limit (2) would recover it (1 < 2).
        # Stricter config limit (1) must keep it blocked (1 >= 1).
        assert kb.recompute_ready(conn, failure_limit=1) == 0
        assert kb.get_task(conn, t2).status == "blocked"




# ---------------------------------------------------------------------------
# Parent-completion invariant at the claim gate (RCA t_a6acd07d)
# ---------------------------------------------------------------------------











def test_approve_unblock_task_checks_snapshot_and_comments_atomically(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Approve blocked card",
            body="Keep this body",
            assignee="developer",
            initial_status="blocked",
        )

        task = kb.approve_unblock_task(
            conn,
            tid,
            expected_status="blocked",
            expected_title="Approve blocked card",
            comment_author="agentic-os-cockpit/developer",
            comment_source="Agentic OS Cockpit approve/unblock control",
        )

        assert task is not None
        assert task.status == "ready"
        row = conn.execute(
            "SELECT status, body, assignee, consecutive_failures, last_failure_error "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert tuple(row) == ("ready", "Keep this body", "developer", 0, None)
        comments = kb.list_comments(conn, tid)
        assert len(comments) == 1
        assert comments[0].author == "agentic-os-cockpit/developer"
        assert "Decision: approved_unblock" in comments[0].body
        assert "Resulting status: ready" in comments[0].body
        events = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
            )
        ]
        assert events[-2:] == ["unblocked", "commented"]


def test_approve_unblock_task_rejects_stale_snapshot_without_comment(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Current title", initial_status="blocked")

        with pytest.raises(RuntimeError, match="refresh"):
            kb.approve_unblock_task(
                conn,
                tid,
                expected_status="blocked",
                expected_title="Old title",
                comment_author="agentic-os-cockpit/developer",
                comment_source="Agentic OS Cockpit approve/unblock control",
            )

        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.list_comments(conn, tid) == []
        event_kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
            )
        ]
        assert "unblocked" not in event_kinds
        assert "commented" not in event_kinds


def test_approve_unblock_task_uses_todo_when_parent_is_not_done(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="Blocked child", parents=[parent])
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (child,))
        conn.commit()

        task = kb.approve_unblock_task(
            conn,
            child,
            expected_status="blocked",
            expected_title="Blocked child",
            comment_author="agentic-os-cockpit/developer",
            comment_source="Agentic OS Cockpit approve/unblock control",
        )

        assert task is not None
        assert task.status == "todo"
        comments = kb.list_comments(conn, child)
        assert "Resulting status: todo" in comments[0].body


def test_assign_refuses_while_running(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        with pytest.raises(RuntimeError, match="currently running"):
            kb.assign_task(conn, t, "b")



def test_delete_archived_task_removes_related_rows(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        tid = kb.create_task(conn, title="child", parents=[parent], assignee="worker")
        kb.add_comment(conn, tid, "user", "cleanup me")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="done")
        assert kb.archive_task(conn, tid)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', '123', '', 'u', 0, 0)",
            (tid,),
        )
        conn.commit()

        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (tid, tid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_task_removes_task_and_cascades(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="to-delete", assignee="alice")
        kb.add_comment(conn, t, "user", "comment")
        kb.add_comment(conn, t, "user", "another")
        assert kb.delete_task(conn, t)
        assert kb.get_task(conn, t) is None
        assert len(kb.list_comments(conn, t)) == 0
        assert len(kb.list_events(conn, t)) == 0
        assert len(kb.list_runs(conn, t)) == 0




# ---------------------------------------------------------------------------
# Comments / events / worker context
# ---------------------------------------------------------------------------


def test_worker_context_marks_product_test_as_evidence_only(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "worker-context-evidence-boundary"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, _workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        run = kb.get_run(conn, task.current_run_id)
        context = kb.build_worker_context(conn, tid)

    assert run is not None and run.metadata is not None
    assert "## Evidence-phase source boundary" in context
    assert "never commit source or fixture changes" in context
    assert "workflow_outcome.verdict=changes_requested" in context
    assert run.metadata["test_head_sha"] in context







# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Respawn guard (check_respawn_guard + dispatch_once integration)
# ---------------------------------------------------------------------------







def test_respawn_guard_blocker_auth_on_authentication_error(kanban_home):
    """Full word 'Authentication' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="authn-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("Authentication failed: invalid credentials", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authorization_error(kanban_home):
    """Full word 'authorization' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="authz-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("authorization denied for scope repo", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_recent_success(kanban_home):
    """A completed run within the guard window triggers recent_success."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="already-done", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "recent_success"


def test_respawn_guard_advanced_outcome_does_not_park_pipeline(kanban_home):
    """A product-workflow step-advance (outcome='advanced') must NOT trip the
    recent_success guard — otherwise every pipeline hop parks the card for the
    full guard window. This is the regression that stalled the Trading Company
    board when a parallel branch stamped step-advances as 'completed'."""
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: pipeline hop",
            assignee="architect-profile",
            workflow_template_id="product",
            current_step_key="architecture",
        )
        # Advancing the step records a run — it must be outcome='advanced',
        # which the guard ignores, so the next role can spawn immediately.
        assert kb.complete_task(
            conn, tid, summary="architecture done", board="prod",
            product_role_assignees={"developer": "developer-profile"},
        )
        latest = kb.latest_run(conn, tid)
        reason = kb.check_respawn_guard(conn, tid)
    assert latest.outcome == "advanced", "step-advance must not be 'completed'"
    assert reason is None, f"pipeline card wrongly parked: {reason}"


def test_respawn_guard_recent_success_bypassed_by_requeue(kanban_home):
    """An explicit re-queue after a recent success (operator done->ready,
    promote, unblock, reclaim) is a deliberate re-run and must bypass the
    recent_success guard — otherwise a manual done->ready just sits there
    until the window elapses."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="rerun-me", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        # Baseline: a recent completion defers the respawn.
        assert kb.check_respawn_guard(conn, t) == "recent_success"
        # Operator drags done -> ready: a 'status' event after completion.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'status', ?)",
            (t, now - 10),
        )
        assert kb.check_respawn_guard(conn, t) is None


def test_respawn_guard_stale_success_not_guarded(kanban_home):
    """A completed run outside the guard window does not block re-spawn."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="old-done", assignee="alice")
        old_end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 60
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, old_end - 300, old_end),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_in_comment(kanban_home):
    """A GitHub PR URL in a recent comment triggers active_pr."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_old_pr_comment_not_guarded(kanban_home):
    """A GitHub PR URL in a comment older than the PR window does not block."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="old-pr", assignee="alice")
        old_ts = int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW - 60
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'worker', "
            "'PR: https://github.com/totemx-AI/subsidysmart/pull/10', ?)",
            (t, old_ts),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_dispatch_respawn_guard_defers_auth_error_without_auto_block(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose last
    error is a blocker_auth.

    The old behaviour auto-blocked on first occurrence, which was too
    aggressive: a transient 429 rate-limit (which typically clears in
    seconds to minutes) would end up requiring manual unblock. The new
    behaviour defers the spawn this tick; the task stays in ``ready``
    and gets another chance next tick. If the auth error genuinely
    persists, the existing ``consecutive_failures`` circuit breaker
    will auto-block via the normal failure-limit path.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="quota-storm", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("rate limit exceeded: 429 Too Many Requests", t),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    # Critical: task is NOT auto-blocked on first occurrence.
    assert t not in res.auto_blocked, (
        f"blocker_auth should defer, not auto-block on first occurrence; "
        f"got auto_blocked={res.auto_blocked!r}"
    )
    # It IS recorded as respawn_guarded with the reason.
    assert (t, "blocker_auth") in res.respawn_guarded, (
        f"expected (task_id, 'blocker_auth') in respawn_guarded; "
        f"got {res.respawn_guarded!r}"
    )
    # And it's NOT spawned this tick.
    assert t not in spawned_ids
    # Status stays ``ready`` so a future tick (or operator action) can
    # retry without manual unblock.
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_skips_recent_success(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with a recent completed run."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="recent-winner", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "recent_success") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # not blocked, just skipped


def test_dispatch_respawn_guard_skips_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with an active PR comment."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_dry_run_no_auto_block(
    kanban_home, all_assignees_spawnable
):
    """In dry_run mode, blocker_auth tasks are recorded in respawn_guarded (not auto-blocked)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="dry-quota", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("quota exceeded", t),
        )
        res = kb.dispatch_once(conn, dry_run=True)

    assert (t, "blocker_auth") in res.respawn_guarded
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # dry_run: no writes


def test_dispatch_respawn_guard_allows_clean_task(
    kanban_home, all_assignees_spawnable
):
    """A task with no guard triggers is spawned normally."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="clean-task", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids
    assert not res.respawn_guarded
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_emits_event_for_skipped_task(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once emits a respawn_guarded task_event so operators can diagnose stuck-ready tasks."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="event-check", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "respawn_guarded" in kinds
    guarded_evt = next(e for e in events if e.kind == "respawn_guarded")
    # Event.payload is already parsed as a dict by list_events.
    assert isinstance(guarded_evt.payload, dict)
    assert guarded_evt.payload.get("reason") == "recent_success"


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------









def test_worktree_workspace_explicit_target_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "custom-task"
    branch = "wt/custom-task"
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="ship",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=branch,
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    assert ws == target
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {target}" in listed
    assert f"branch refs/heads/{branch}" in listed


# ---------------------------------------------------------------------------
# Epic-branch base ref threading for v2 story worktrees (T4.1)
# ---------------------------------------------------------------------------

def test_ensure_git_worktree_base_param_branches_off_given_branch(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "feat"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "feat"], check=True, capture_output=True, text=True)
    feat_sha = _commit_file(repo, "feat.txt", "feat\n", "feat commit")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)

    target = repo / ".worktrees" / "story1"
    kb._ensure_git_worktree(repo, target, "story1", base="feat")

    assert (target / "feat.txt").exists()
    subprocess.run(
        ["git", "-C", str(target), "merge-base", "--is-ancestor", feat_sha, "HEAD"],
        check=True, capture_output=True, text=True,
    )


def test_ensure_git_worktree_default_base_still_branches_off_head(tmp_path):
    """Legacy call sites (no ``base`` kwarg) keep branching off HEAD, byte-for-byte."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "feat"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "feat"], check=True, capture_output=True, text=True)
    _commit_file(repo, "feat.txt", "feat\n", "feat commit")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    target = repo / ".worktrees" / "story-legacy"
    kb._ensure_git_worktree(repo, target, "story-legacy")

    assert not (target / "feat.txt").exists()
    worktree_head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert worktree_head == head_sha


def test_epic_branch_for_naming():
    assert kb.epic_branch_for("epic-123") == "epic/epic-123"


def test_ensure_epic_branch_creates_off_head_idempotently(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    epic_branch = kb.epic_branch_for("epic-1")

    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    assert kb._git_branch_exists(repo, epic_branch)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    branch_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", epic_branch], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert branch_sha == head_sha

    # Idempotent: calling again does not error or move the branch.
    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    branch_sha_again = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", epic_branch], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert branch_sha_again == head_sha


def test_story_base_branch_v2_story_with_epic_parent_returns_epic_branch(kanban_home):
    board = "v2-story-base-branch"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        result = kb._story_base_branch(conn, story, board=board)
    assert result == kb.epic_branch_for(epic)


def test_story_base_branch_no_parent_returns_none(kanban_home):
    board = "v2-story-base-branch-no-parent"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        story = kb.create_task(conn, title="Story", board=board)
        result = kb._story_base_branch(conn, story, board=board)
    assert result is None


def test_story_base_branch_non_v2_board_returns_none(kanban_home):
    board = "legacy-story-base-branch"
    kb.create_board(board, name="Legacy Board", preset="product")
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        result = kb._story_base_branch(conn, story, board=board)
    assert result is None


def test_resolve_worktree_workspace_default_base_branch_none_uses_head(kanban_home, tmp_path):
    """Legacy call sites (no ``base_branch`` kwarg) keep branching off HEAD."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    with kb.connect() as conn:
        t = kb.create_task(conn, title="ship", workspace_kind="worktree", workspace_path=str(repo))
        task = kb.get_task(conn, t)
        assert task is not None
        ws, _branch = kb._resolve_worktree_workspace(task)
    ws_head = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert ws_head == head_sha


def test_story_worktree_branches_off_epic_branch_contains_upstream_commit(kanban_home, tmp_path):
    """The plan's key test: a downstream story's worktree must contain the
    upstream story's committed code, proven by branching off the epic branch."""
    board = "v2-epic-branching"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        epic_branch = kb.epic_branch_for(epic)

    # Simulate an upstream story's integrated commit landing on the epic branch.
    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    subprocess.run(["git", "-C", str(repo), "checkout", epic_branch], check=True, capture_output=True, text=True)
    upstream_sha = _commit_file(repo, "upstream.txt", "upstream story code\n", "upstream story")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)

    with kb.connect(board=board) as conn:
        story = kb.create_task(
            conn, title="Story", board=board,
            workspace_kind="worktree", workspace_path=str(repo),
        )
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        task = kb.get_task(conn, story)
        assert task is not None
        ws, _branch = kb._resolve_worktree_workspace(
            task, board=board, base_branch=epic_branch,
        )

    assert (ws / "upstream.txt").exists()
    subprocess.run(
        ["git", "-C", str(ws), "merge-base", "--is-ancestor", upstream_sha, "HEAD"],
        check=True, capture_output=True, text=True,
    )


def test_spawn_one_v2_wires_story_base_branch_to_epic(kanban_home, tmp_path, monkeypatch):
    """_spawn_one_v2 (the v2 spawn path) computes _story_base_branch and threads
    it into _resolve_worktree_workspace, so a v2 story's worktree lands on top
    of its epic branch -- without touching the live dispatch loop."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-epic-branch"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)

    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        epic_branch = kb.epic_branch_for(epic)

    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    subprocess.run(["git", "-C", str(repo), "checkout", epic_branch], check=True, capture_output=True, text=True)
    upstream_sha = _commit_file(repo, "upstream.txt", "upstream story code\n", "upstream story")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)

    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kb.connect(board=board) as conn:
        story = kb.create_task(
            conn, title="Story", board=board,
            assignee="developer", workspace_kind="worktree", workspace_path=str(repo),
        )
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        task = kb.get_task(conn, story)
        assert task is not None and task.status == "ready", (
            "story with a done epic parent should be immediately ready"
        )
        pid = kb._spawn_one_v2(conn, story, board=board, spawn_fn=fake_spawn)

    assert pid == 4242
    assert len(spawns) == 1
    ws = Path(spawns[0][1])
    assert (ws / "upstream.txt").exists()
    subprocess.run(
        ["git", "-C", str(ws), "merge-base", "--is-ancestor", upstream_sha, "HEAD"],
        check=True, capture_output=True, text=True,
    )


def test_spawn_one_v2_success_sets_running_flag(kanban_home, tmp_path, monkeypatch):
    """A successful _spawn_one_v2 spawn ends with the v2 running flag set.
    R1 update: the flag is now set by claim_task (the seam _spawn_one_v2
    calls internally), not by a separate set_running() call in this
    function -- see test_claim_task_v2_board_sets_running_flag_and_consistent_status
    for direct coverage of that seam."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-sets-running"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def fake_spawn(task, workspace, board=None):
        return 4242

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        pid = kb._spawn_one_v2(conn, tid, board=board, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        row = conn.execute(
            "SELECT running, status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert pid == 4242
    assert task is not None and task.worker_pid == 4242
    assert row["running"] == 1
    assert row["status"] == "running"


def test_spawn_one_v2_stamps_runtime_identity_before_spawn(
    kanban_home, tmp_path, monkeypatch
):
    """The event-driven v2 path stamps the same executor facts as polling."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-stamps-runtime"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    stamped: list[str] = []

    def stamp(_conn, task):
        stamped.append(task.id)
        return {
            "profile": "developer",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
        }

    monkeypatch.setattr(kb, "_stamp_run_executor_identity", stamp)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        pid = kb._spawn_one_v2(
            conn,
            tid,
            board=board,
            spawn_fn=lambda _task, _workspace, board=None: 4242,
        )

    assert pid == 4242
    assert stamped == [tid]


def test_spawn_one_v2_failure_clears_running_flag(kanban_home, tmp_path, monkeypatch):
    """R3 fix: claim_task sets running=1 at claim time (R1), and a failed
    spawn now goes through _record_task_failure (via _record_spawn_failure),
    which clears ``running`` back to 0 alongside the legacy ``status`` revert
    to 'ready' -- closing the status/flag gap R1's reviewer flagged. This
    test used to pin the pre-R3 gap (running stuck at 1); it now asserts the
    fixed behavior."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-failure-no-running"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def boom(task, workspace, board=None):
        raise RuntimeError("spawn failed")

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        pid = kb._spawn_one_v2(conn, tid, board=board, spawn_fn=boom)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert pid is None
    # R3: _record_task_failure now clears running (and blocked) on the
    # failure path, so flags and status agree again.
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] != "running"


def test_spawn_then_handoff_running_flag_round_trip(kanban_home, tmp_path, monkeypatch):
    """W2 lifecycle: spawn sets running=1 (via _spawn_one_v2), then handoff
    clears it back to running=0 on the same card -- the full set/clear
    round-trip the running flag is meant to support."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-handoff-roundtrip"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def fake_spawn(task, workspace, board=None):
        return 4242

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        kb._spawn_one_v2(conn, tid, board=board, spawn_fn=fake_spawn)
        after_spawn = conn.execute(
            "SELECT running FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        spawned_workspace = Path(kb.get_task(conn, tid).workspace_path)
        (spawned_workspace / "src.py").write_text("print('hi')\n", encoding="utf-8")
        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        after_handoff = conn.execute(
            "SELECT running FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert after_spawn["running"] == 1
    assert result is True
    assert after_handoff["running"] == 0


def test_handoff_releases_worker_claim_so_next_agent_can_spawn(kanban_home, tmp_path, monkeypatch):
    """Regression: handoff must release the completing worker's claim
    (claim_lock / claim_expires / worker_pid), not just clear ``running``.

    Otherwise the handed-off card stays ready+claimed, and
    ``spawn_after_handoff`` (``WHERE claim_lock IS NULL``) skips it -- the
    event-driven chain stalls at every handoff until a manual reclaim.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-releases-claim"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def fake_spawn(task, workspace, board=None):
        return 4242

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        kb._spawn_one_v2(conn, tid, board=board, spawn_fn=fake_spawn)
        after_spawn = conn.execute(
            "SELECT claim_lock, worker_pid FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        spawned_workspace = Path(kb.get_task(conn, tid).workspace_path)
        (spawned_workspace / "src.py").write_text("print('hi')\n", encoding="utf-8")
        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        after_handoff = conn.execute(
            "SELECT status, claim_lock, claim_expires, worker_pid FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    # spawn claimed the card
    assert after_spawn["claim_lock"] is not None
    # handoff advanced AND released the claim -> card is ready + unclaimed,
    # which is exactly what spawn_after_handoff requires to fire the next agent.
    assert result is True
    assert after_handoff["status"] == "ready"
    assert after_handoff["claim_lock"] is None
    assert after_handoff["claim_expires"] is None
    assert after_handoff["worker_pid"] is None


# ---------------------------------------------------------------------------
# R1: claim_task maintains the v2 running flag (state-model integrity)
#
# The v2 running flag used to be set only by _spawn_one_v2 -- a path the
# LIVE gateway never calls (it spawns via dispatch_once -> claim_task
# directly). So a gateway-spawned v2 card ended up status='running',
# running=0: flags and status disagreeing, the exact defect the v2 state
# model exists to prevent. _apply_v2_flags is the single in-txn seam that
# fixes this; claim_task is its first (and, after this task, only) caller
# for the running flag on the claim path.
# ---------------------------------------------------------------------------

def test_apply_v2_flags_sets_flag_and_syncs_status(kanban_home, monkeypatch):
    """Direct unit coverage of the seam helper: sets the requested flag(s)
    and re-derives legacy status via _sync_legacy_status."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-apply-flags"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        with kb.write_txn(conn):
            kb._apply_v2_flags(conn, tid, meta, running=True, blocked=False)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert row["running"] == 1
    assert row["blocked"] == 0
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta)


def test_apply_v2_flags_legacy_board_is_noop(kanban_home):
    """meta=None (legacy board) -- flags and status must be untouched."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        with kb.write_txn(conn):
            kb._apply_v2_flags(conn, tid, None, running=True, blocked=True)
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert after == before


def test_apply_v2_flags_noop_when_not_handoff_v2_enabled(kanban_home, monkeypatch):
    """A product-preset board that hasn't opted into handoff_v2 also no-ops."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-no-v2-apply-flags"
    kb.create_board(board, name="Product No V2", preset="product")
    meta = kb.read_board_metadata(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="Story", workflow_template_id="product", current_step_key="development",
        )
        before = dict(conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone())
        with kb.write_txn(conn):
            kb._apply_v2_flags(conn, tid, meta, running=True, blocked=True)
        after = dict(conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert after == before


def test_claim_task_v2_board_sets_running_flag_and_consistent_status(kanban_home, monkeypatch):
    """claim_task itself (not _spawn_one_v2) must set running=1 on a v2
    board's card -- this is the fix for the gateway-bypasses-the-flag gap."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-claim-sets-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        claimed = kb.claim_task(conn, tid, claimer="host:1")
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert claimed is not None
    assert row["running"] == 1
    assert row["blocked"] == 0
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta)


def test_claim_task_legacy_board_does_not_touch_flags(kanban_home):
    """Legacy (non-v2) boards: claim_task must remain byte-for-byte
    unchanged -- neither running nor blocked is touched by the claim."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task", assignee="alice")
        claimed = kb.claim_task(conn, tid, claimer="host:1")
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert claimed is not None
    assert row["status"] == "running"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_dispatch_once_gateway_spawn_sets_running_flag(kanban_home, tmp_path, monkeypatch):
    """THE key integration test: drive the REAL dispatch_once -> claim_task
    live-gateway path (not _spawn_one_v2, not set_running directly) on a v2
    board and prove the claimed card's running flag and legacy status agree.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    board = "v2-dispatch-sets-running"
    _v2_product_board(board)
    meta = kb.read_board_metadata(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn, board=board)

        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert len(spawns) == 1
    assert spawns[0][0] == tid
    assert {s[0] for s in result.spawned} == {tid}
    assert row["running"] == 1
    assert row["blocked"] == 0
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta), (
        "flags and status must not disagree on a live v2 board"
    )


def test_task_from_row_exposes_running_and_blocked(kanban_home, monkeypatch):
    """get_task/Task.from_row surfaces running/blocked reflecting the row."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-task-exposes-flags"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute(
            "UPDATE tasks SET running = 1, blocked = 0 WHERE id = ?", (tid,)
        )
        task = kb.get_task(conn, tid)

    assert task.running is True
    assert task.blocked is False


def test_task_from_row_defaults_flags_false_when_columns_absent():
    """Defensive mapping: a row lacking running/blocked columns (e.g. an
    older schema snapshot) defaults both to False rather than raising."""
    row = {
        "id": "t1",
        "title": "T",
        "body": None,
        "assignee": None,
        "status": "ready",
        "priority": 0,
        "created_by": None,
        "created_at": 0,
        "started_at": None,
        "completed_at": None,
        "workspace_kind": "inline",
        "workspace_path": None,
        "claim_lock": None,
        "claim_expires": None,
    }
    task = kb.Task.from_row(row)
    assert task.running is False
    assert task.blocked is False


# ---------------------------------------------------------------------------
# _commit_worker_diff (Phase 2 atomic commit-first handoff)
# ---------------------------------------------------------------------------

def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_dependency_source_base_selects_required_parent_receipt(kanban_home, tmp_path):
    repo = tmp_path / "dependency-source-base"
    _init_git_repo(repo)


    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn, title="Parent", workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.claim_task(conn, parent_id)
        assert parent is not None and parent.current_run_id is not None
        (repo / "parent.txt").write_text("parent\n", encoding="utf-8")
        assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
        child_id = kb.create_task(conn, title="Child", parents=[parent_id])
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == _head_sha(repo)


def test_dependency_source_base_uses_latest_completed_receipt(kanban_home, tmp_path):
    repo = tmp_path / "completed-receipt-repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn, title="Parent", workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.claim_task(conn, parent_id)
        assert parent is not None and parent.current_run_id is not None
        (repo / "first.txt").write_text("first\n", encoding="utf-8")
        assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
        first_sha = _head_sha(repo)
        conn.execute(
            "UPDATE task_runs SET ended_at = ended_at + 100 WHERE id = ?",
            (parent.current_run_id,),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, metadata) "
            "VALUES (?, 'crashed', 'crashed', 200, 300, ?)",
            (parent_id, json.dumps({"source_completion_receipt": {"commit_sha": first_sha}})),
        )
        child_id = kb.create_task(conn, title="Child", parents=[parent_id])
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == first_sha


def test_dependency_source_base_deduplicates_identical_receipts(kanban_home, tmp_path):
    repo = tmp_path / "duplicate-receipt-repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        parents = []
        shared_sha = _head_sha(repo)
        for title in ("One", "Two"):
            parent_id = kb.create_task(
                conn, title=title, workspace_kind="worktree", workspace_path=str(repo),
                source_commit_required=True,
            )
            parent = kb.claim_task(conn, parent_id)
            assert parent is not None and parent.current_run_id is not None
            (repo / f"{title.lower()}.txt").write_text(f"{title}\n", encoding="utf-8")
            assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps({"source_completion_receipt": {"commit_sha": shared_sha}}), parent.current_run_id),
            )
            parents.append(parent_id)
        child_id = kb.create_task(conn, title="Child", parents=parents)
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == shared_sha


def test_resolve_worktree_workspace_rejects_divergent_receipts_before_materializing(
    kanban_home, tmp_path, monkeypatch
):
    board = "dependency-source-divergence"
    kb.create_board(board, name="Dependency Source", preset="generic")
    repo = tmp_path / "divergent-receipts-repo"
    _init_git_repo(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "side"], check=True,
                   capture_output=True, text=True)
    second_sha = _commit_file(repo, "second.txt", "second\n", "second")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True,
                   capture_output=True, text=True)
    first_sha = _commit_file(repo, "first.txt", "first\n", "first")
    target = repo / ".worktrees" / "child"
    monkeypatch.setattr(kb, "_story_base_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(kb, "_handoff_v2_enabled", lambda _meta: False)
    with kb.connect(board=board) as conn:
        parents = []
        for title, sha in (("One", first_sha), ("Two", second_sha)):
            parent_id = kb.create_task(
                conn, title=title, board=board, workspace_kind="worktree", workspace_path=str(repo),
                source_commit_required=True,
            )
            parent = kb.claim_task(conn, parent_id)
            assert parent is not None and parent.current_run_id is not None
            (repo / f"{title.lower()}.txt").write_text(f"{title}\n", encoding="utf-8")
            assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps({"source_completion_receipt": {"commit_sha": sha}}),
                 parent.current_run_id),
            )
            parents.append(parent_id)
        child_id = kb.create_task(
            conn, title="Child", board=board, parents=parents, workspace_kind="worktree",
            workspace_path=str(repo),
        )
        child = kb.get_task(conn, child_id)
        assert child is not None
        with pytest.raises(RuntimeError, match="diverge"):
            kb._resolve_worktree_workspace(child, board=board, conn=conn)
    assert not target.exists()
    assert not kb._git_branch_exists(repo, f"wt/{child_id}")


def test_dependency_source_base_linear_multi_parent_uses_descendant(kanban_home, tmp_path):
    repo = tmp_path / "linear-multi-parent-repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        parent_ids = []
        for title in ("Ancestor", "Descendant"):
            parent_id = kb.create_task(conn, title=title, workspace_kind="worktree", workspace_path=str(repo), source_commit_required=True)
            parent = kb.claim_task(conn, parent_id)
            assert parent is not None and parent.current_run_id is not None
            (repo / f"{title.lower()}.txt").write_text(title, encoding="utf-8")
            assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
            parent_ids.append(parent_id)
        child_id = kb.create_task(conn, title="Child", parents=parent_ids)
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == _head_sha(repo)


def test_dependency_source_flow_resolves_parent_and_child_worktrees(kanban_home, tmp_path):
    board = "dependency-source-e2e"
    kb.create_board(board, name="Dependency Source E2E", preset="generic")
    repo = tmp_path / "dependency-source-e2e-repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        parent_id = kb.create_task(
            conn, title="Parent", board=board, assignee="developer",
            workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.get_task(conn, parent_id)
        assert parent is not None
        parent_ws, parent_branch = kb._resolve_worktree_workspace(parent, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(parent_ws), parent_id))
        (parent_ws / "parent.txt").write_text("parent content\n", encoding="utf-8")
        claimed = kb.claim_task(conn, parent_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(conn, parent_id, expected_run_id=claimed.current_run_id)
        child_id = kb.create_task(conn, title="Child", board=board, parents=[parent_id], workspace_kind="worktree", workspace_path=str(repo), source_commit_required=True)
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "ready"
        child_ws, child_branch = kb._resolve_worktree_workspace(child, board=board, conn=conn)
    assert parent_ws != child_ws
    assert parent_branch != child_branch
    assert (child_ws / "parent.txt").read_text(encoding="utf-8") == "parent content\n"


def test_dependency_source_flow_resolves_concrete_child_worktree_from_parent(kanban_home, tmp_path):
    board = "dependency-source-concrete-child"
    kb.create_board(board, name="Dependency Source Concrete Child", preset="generic")
    repo = tmp_path / "dependency-source-concrete-child-repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        parent_id = kb.create_task(
            conn, title="Parent", board=board, assignee="developer",
            workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.get_task(conn, parent_id)
        assert parent is not None
        parent_ws, parent_branch = kb._resolve_worktree_workspace(parent, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(parent_ws), parent_id))
        (parent_ws / "parent.txt").write_text("parent content\n", encoding="utf-8")
        claimed = kb.claim_task(conn, parent_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(conn, parent_id, expected_run_id=claimed.current_run_id)

        child_id = kb.create_task(
            conn, title="Child", board=board, parents=[parent_id], assignee="developer",
            workspace_kind="worktree", workspace_path=str(repo / ".worktrees" / "child"),
            branch_name=f"child/{parent_id}",
        )
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "ready"
        child_ws, child_branch = kb._resolve_worktree_workspace(child, board=board, conn=conn)

    assert parent_branch != child_branch
    assert child_ws == repo / ".worktrees" / "child"
    assert (child_ws / "parent.txt").read_text(encoding="utf-8") == "parent content\n"


def test_complete_task_required_source_commits_before_terminal_update_and_persists_receipt(
    kanban_home, tmp_path
):
    repo = tmp_path / "completion-repo"
    _init_git_repo(repo)
    base_sha = _head_sha(repo)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Commit before done",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_required=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")

        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented feature",
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, tid)
        run = kb.get_run(conn, run_id)

    assert task is not None and task.status == "done"
    assert run is not None and run.metadata is not None
    intent = run.metadata["source_completion_intent"]
    receipt = run.metadata["source_completion_receipt"]
    assert intent["run_id"] == run_id
    assert receipt["intent_id"] == intent["intent_id"]
    assert receipt["run_id"] == run_id
    assert receipt["base_sha"] == base_sha
    assert receipt["commit_sha"] == _head_sha(repo)
    assert receipt["commit_sha"] != base_sha
    assert len(receipt["tree_sha"]) == 40
    assert len(receipt["diff_digest"]) == 64
    assert receipt["paths"] == ["feature.py"]
    assert receipt["created_at"] >= intent["created_at"]
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def test_default_board_forbidden_dependency_chain_forwards_candidate_sha(
    kanban_home, tmp_path
):
    board = "default"
    repo = tmp_path / "default-source-three-card-repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        developer_id = kb.create_task(conn, title="Developer", board=board,
            assignee="developer", workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True)
        developer = kb.get_task(conn, developer_id)
        assert developer is not None
        developer_ws, _ = kb._resolve_worktree_workspace(developer, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(developer_ws), developer_id))
        (developer_ws / "feature.txt").write_text("candidate\n", encoding="utf-8")
        claimed = kb.claim_task(conn, developer_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(conn, developer_id, expected_run_id=claimed.current_run_id)
        developer_sha = _head_sha(developer_ws)
        tester_id = kb.create_task(conn, title="Tester", board=board, assignee="tester",
            parents=[developer_id], workspace_kind="worktree", workspace_path=str(repo),
            source_commit_forbidden=True)
        tester = kb.get_task(conn, tester_id)
        assert tester is not None and tester.status == "ready"
        tester_ws, _ = kb._resolve_worktree_workspace(tester, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(tester_ws), tester_id))
        assert _head_sha(tester_ws) == developer_sha
        tester_claimed = kb.claim_task(conn, tester_id)
        assert tester_claimed is not None and tester_claimed.current_run_id is not None
        assert kb.complete_task(conn, tester_id, metadata={"candidate_sha": "caller-value"},
            expected_run_id=tester_claimed.current_run_id)
        tester_run = kb.get_run(conn, tester_claimed.current_run_id)
        assert tester_run is not None and tester_run.metadata["candidate_sha"] == developer_sha
        reviewer_id = kb.create_task(conn, title="Reviewer", board=board, assignee="reviewer",
            parents=[tester_id], workspace_kind="worktree", workspace_path=str(repo),
            source_commit_forbidden=True)
        reviewer = kb.get_task(conn, reviewer_id)
        assert reviewer is not None and reviewer.status == "ready"
        reviewer_ws, _ = kb._resolve_worktree_workspace(reviewer, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(reviewer_ws), reviewer_id))
        assert _head_sha(reviewer_ws) == developer_sha
        assert (reviewer_ws / "feature.txt").read_text(encoding="utf-8") == "candidate\n"


def test_complete_task_adopts_the_one_exact_commit_after_crash_before_receipt(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "adoption-repo"
    _init_git_repo(repo)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Adopt exact commit",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_required=True,
        )
        first = kb.claim_task(conn, tid)
        assert first is not None and first.current_run_id is not None
        first_run_id = first.current_run_id
        (repo / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        original_persist = kb._persist_source_completion_metadata
        persist_calls = 0

        def crash_before_receipt(*args, **kwargs):
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 2:
                raise RuntimeError("simulated crash after git commit")
            return original_persist(*args, **kwargs)

        monkeypatch.setattr(kb, "_persist_source_completion_metadata", crash_before_receipt)
        with pytest.raises(RuntimeError, match="simulated crash"):
            kb.complete_task(conn, tid, expected_run_id=first_run_id)
        committed_sha = _head_sha(repo)
        assert kb.get_task(conn, tid).status == "running"

        monkeypatch.setattr(kb, "_persist_source_completion_metadata", original_persist)
        assert kb.reclaim_task(conn, tid, reason="retry source finalization")
        second = kb.claim_task(conn, tid)
        assert second is not None and second.current_run_id is not None
        second_run_id = second.current_run_id

        assert kb.complete_task(conn, tid, expected_run_id=second_run_id)
        receipt = kb.get_run(conn, second_run_id).metadata["source_completion_receipt"]

    assert receipt["adopted"] is True
    assert receipt["commit_sha"] == committed_sha == _head_sha(repo)
    assert receipt["intent_run_id"] == first_run_id
    assert receipt["run_id"] == second_run_id
    log_count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log_count == "2"


def test_complete_task_forbidden_source_does_not_commit_worker_diff(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "forbidden-repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Evidence only",
            assignee="reviewer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        monkeypatch.setattr(
            kb,
            "_commit_worker_diff",
            lambda *args, **kwargs: pytest.fail("forbidden completion authored source"),
        )

        assert kb.complete_task(
            conn, tid, expected_run_id=claimed.current_run_id
        )

        task = kb.get_task(conn, tid)

    assert task is not None and task.status == "done"
    assert task.source_commit_forbidden is True
    assert _head_sha(repo) == subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_complete_task_forbidden_source_rejects_dirty_git_before_terminal_mutation(
    kanban_home, tmp_path
):
    repo = tmp_path / "dirty-forbidden-repo"
    _init_git_repo(repo)
    before_sha = _head_sha(repo)
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="Evidence-only parent",
            assignee="reviewer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        child_id = kb.create_task(conn, title="Dependent")
        kb.link_tasks(conn, parent_id, child_id)
        claimed = kb.claim_task(conn, parent_id)
        assert claimed is not None and claimed.current_run_id is not None
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("diagnosis\n", encoding="utf-8")

        with pytest.raises(kb._SourceCommitError) as raised:
            kb.complete_task(conn, parent_id, expected_run_id=claimed.current_run_id)

        parent = kb.get_task(conn, parent_id)
        child = kb.get_task(conn, child_id)
        run = kb.get_run(conn, claimed.current_run_id)

    assert raised.value.code == "source_forbidden_dirty"
    assert parent is not None and parent.status == "running"
    assert child is not None and child.status == "todo"
    assert run is not None and run.ended_at is None
    assert _head_sha(repo) == before_sha
    assert (repo / "README.md").read_text(encoding="utf-8") == "dirty\n"
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "diagnosis\n"


def test_complete_task_forbidden_source_allows_non_git_report_only_workspace(
    kanban_home, tmp_path
):
    workspace = tmp_path / "report-only"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Report-only evidence",
            assignee="reviewer",
            workspace_kind="dir",
            workspace_path=str(workspace),
            source_commit_forbidden=True,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None

        assert kb.complete_task(conn, task_id, expected_run_id=claimed.current_run_id)

        task = kb.get_task(conn, task_id)

    assert task is not None and task.status == "done"


def test_complete_task_required_source_raises_typed_failure_without_commit(
    kanban_home, tmp_path
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Missing source",
            assignee="developer",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            source_commit_required=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None

        with pytest.raises(kb._SourceCommitError) as raised:
            kb.complete_task(conn, tid, expected_run_id=claimed.current_run_id)

        task = kb.get_task(conn, tid)

    assert raised.value.code == "not_a_git_repository"
    assert task is not None and task.status == "running"


def test_complete_task_required_source_rechecks_run_ownership_before_done(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "cas-repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="CAS before done",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_required=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        (repo / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
        original_persist = kb._persist_source_completion_metadata

        def lose_ownership_after_receipt(*args, **kwargs):
            original_persist(*args, **kwargs)
            if kwargs.get("receipt") is not None:
                conn.execute(
                    "UPDATE tasks SET current_run_id = NULL WHERE id = ?",
                    (tid,),
                )
                conn.commit()

        monkeypatch.setattr(
            kb, "_persist_source_completion_metadata", lose_ownership_after_receipt
        )

        with pytest.raises(kb._SourceCommitError) as raised:
            kb.complete_task(conn, tid, expected_run_id=run_id)

        task = kb.get_task(conn, tid)

    assert raised.value.code == "run_changed"
    assert task is not None and task.status == "running"
    assert _head_sha(repo) != subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_commit_worker_diff_dirty_worktree_returns_sha_and_cleans_tree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="worktree", workspace_path=str(repo)
        )
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")
        sha = kb._commit_worker_diff(conn, tid)

    assert sha is not None
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""
    assert _head_sha(repo) == sha


def test_commit_worker_diff_nothing_to_commit_returns_none(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="worktree", workspace_path=str(repo)
        )
        before = _head_sha(repo)
        result = kb._commit_worker_diff(conn, tid)

    assert result is None
    assert _head_sha(repo) == before


def test_commit_worker_diff_no_repo_returns_none(kanban_home, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="dir", workspace_path=str(not_a_repo)
        )
        result = kb._commit_worker_diff(conn, tid)

    assert result is None


def test_commit_worker_diff_missing_workspace_path_returns_none(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="no workspace")
        result = kb._commit_worker_diff(conn, tid)

    assert result is None


def test_commit_worker_diff_respects_gitignore(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add gitignore"], check=True, capture_output=True, text=True)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="worktree", workspace_path=str(repo)
        )
        state_dir = repo / "state"
        state_dir.mkdir()
        (state_dir / "runtime.json").write_text("{}\n", encoding="utf-8")
        (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
        sha = kb._commit_worker_diff(conn, tid)

    assert sha is not None
    show = subprocess.run(
        ["git", "-C", str(repo), "show", "--stat", "--name-only", sha],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "feature.py" in show
    assert "state/runtime.json" not in show
    ls_files = subprocess.run(
        ["git", "-C", str(repo), "ls-files"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "state/runtime.json" not in ls_files


# ---------------------------------------------------------------------------
# handoff() -- atomic commit-first advance (T2.2-T2.4)
# ---------------------------------------------------------------------------

def _card_snapshot(conn: sqlite3.Connection, task_id: str) -> dict:
    return dict(conn.execute(
        "SELECT current_step_key, running, blocked, status, assignee, result "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone())


def test_handoff_commit_first_gate_blocks_advance_on_clean_tree(kanban_home, tmp_path, monkeypatch):
    """T2.2: no committed diff (clean tree) -> False, card untouched, no event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-gate"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        before = _card_snapshot(conn, tid)

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert after == before
    assert after["current_step_key"] == "development"
    assert not any(event.kind == "handoff" for event in events)


def test_handoff_happy_path_commits_advances_and_emits_one_event(kanban_home, tmp_path, monkeypatch):
    """T2.3: dirty worktree + provenance -> commits, advances, retags, syncs status, one event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-happy"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET running = 1 WHERE id = ?", (tid,))
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)
        meta = kb.read_board_metadata(board)

    assert result is True
    assert card["current_step_key"] == "test"
    assert card["assignee"] == "tester"
    assert card["running"] == 0
    assert card["result"] == "Implemented checkout"
    assert card["status"] == kb._legacy_status(card, meta)

    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    payload = handoff_events[0].payload
    assert payload["from_step"] == "development"
    assert payload["to_step"] == "test"
    assert payload["assignee"] == "tester"
    assert payload["summary"] == "Implemented checkout"
    sha = payload["sha"]
    assert sha and len(sha) == 40

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == sha
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


def test_handoff_terminal_review_advances_with_no_next_assignee(kanban_home, tmp_path, monkeypatch):
    """T2.4a: review -> release_measure advances with assignee None, one event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-review"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        (repo / "notes.md").write_text("looks good\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board,
            metadata={
                "ai_provenance": {
                    "writer": {"agent": "hermes"},
                    "reviewer": {"agent": "codex"},
                }
            },
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is True
    assert card["current_step_key"] == "release_measure"
    assert card["assignee"] is None
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["to_step"] == "release_measure"
    assert handoff_events[0].payload["assignee"] is None


def test_handoff_terminal_release_measure_does_not_auto_advance(kanban_home, tmp_path, monkeypatch):
    """T2.4b: release_measure has no transition -> False, nothing committed, no event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-terminal"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="release_measure",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        # Dirty tree present: if handoff mistakenly committed first, this
        # would prove the bug (HEAD would move even without a transition).
        (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        before_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        before = _card_snapshot(conn, tid)

        result = kb.handoff(conn, tid, board=board)

        after = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert after == before
    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert after_head == before_head
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() != ""  # still dirty -- never staged/committed
    assert not any(event.kind == "handoff" for event in events)


def test_handoff_non_v2_board_is_noop(kanban_home, monkeypatch):
    """Non-v2 (legacy) boards never use handoff() -- False, no mutation (T2.5 guarantee)."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-no-v2-handoff"
    kb.create_board(board, name="Product No V2", preset="product")
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="development",
        )
        before = _card_snapshot(conn, tid)

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after = _card_snapshot(conn, tid)

    assert result is False
    assert after == before


def test_handoff_noop_then_legacy_complete_task_advances_card(kanban_home, monkeypatch):
    """Coexistence guard (T2.5): on a non-v2 product board, ``handoff()``
    no-ops (returns ``False``, mutates nothing, emits no ``handoff`` event)
    and legacy ``complete_task`` still advances the same card exactly as
    ``test_product_completion_advances_card_to_next_role`` asserts.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-legacy-coexist"
    kb.create_board(board, name="Product Legacy Coexist", preset="product")
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="architect-profile",
            workflow_template_id="product",
            current_step_key="architecture",
        )
        before = _card_snapshot(conn, tid)

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after_handoff = _card_snapshot(conn, tid)
        events_after_handoff = kb.list_events(conn, tid)

        assert result is False
        assert after_handoff == before
        assert not any(event.kind == "handoff" for event in events_after_handoff)

        # Legacy completion must still advance the card exactly as before.
        assert kb.complete_task(
            conn,
            tid,
            summary="Architecture settled",
            board=board,
            product_role_assignees={"developer": "developer-profile"},
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest_run = kb.latest_run(conn, tid)

    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "developer-profile"
    assert latest_run.outcome == "advanced"
    advanced = [event for event in events if event.kind == "workflow_advanced"]
    assert advanced
    assert advanced[-1].payload["from_step"] == "architecture"
    assert advanced[-1].payload["to_step"] == "development"


def test_handoff_provenance_failure_raises_and_leaves_card_untouched(kanban_home, tmp_path, monkeypatch):
    """Provenance gate runs before commit-first: raises, nothing committed or mutated."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-provenance"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")
        before = _card_snapshot(conn, tid)

        with pytest.raises(kb.ProductProvenanceError, match="Development completion"):
            kb.handoff(conn, tid, board=board)

        after = _card_snapshot(conn, tid)

    assert after == before
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() != ""  # never staged/committed


# ---------------------------------------------------------------------------
# complete_task -> handoff() routing on handoff_v2 boards (W1)
# ---------------------------------------------------------------------------

def test_complete_task_v2_non_terminal_routes_to_commit_first_handoff(kanban_home, tmp_path, monkeypatch):
    """A real v2 worker's non-terminal completion routes through ``handoff()``:
    dirty worktree -> committed, card advances, exactly one ``handoff`` event,
    ``running`` cleared, and the run is ended cleanly (no dangling open run).
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-happy"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id_before = claimed.current_run_id
        assert run_id_before is not None
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        closed_run = kb.get_run(conn, run_id_before)

    assert result is True
    assert card["current_step_key"] == "test"
    assert card["assignee"] == "tester"
    assert card["running"] == 0

    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["from_step"] == "development"
    assert handoff_events[0].payload["to_step"] == "test"
    sha = handoff_events[0].payload["sha"]
    assert sha and len(sha) == 40
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == sha

    # Run bookkeeping: no dangling open run.
    assert task.current_run_id is None
    assert closed_run is not None
    assert closed_run.ended_at is not None
    assert closed_run.outcome == "advanced"
    assert closed_run.status == "completed"


def test_complete_task_v2_no_diff_does_not_complete(kanban_home, tmp_path, monkeypatch):
    """A v2 completion with a clean worktree (no committed diff) returns
    False and does NOT complete or advance the card -- the commit-first
    gate reaches real workers via ``complete_task``.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-no-diff"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        before = _card_snapshot(conn, tid)

        result = kb.complete_task(
            conn,
            tid,
            summary="Nothing changed",
            board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert after == before
    assert after["current_step_key"] == "development"
    assert task.status != "done"
    assert not any(event.kind == "handoff" for event in events)
    assert not any(event.kind == "workflow_advanced" for event in events)


def test_complete_task_v2_clean_test_evidence_advances_without_commit(kanban_home, tmp_path, monkeypatch):
    """A test-step handoff records evidence and advances even when the
    worktree is clean: testers verify the existing development commit and
    usually have no source diff of their own to commit.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-clean-test"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    before_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )

        result = kb.complete_task(
            conn,
            tid,
            summary="Tests passed",
            board=board,
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {"tester": {"agent": "hermes", "result": "passed"}},
            },
        )

        card = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert result is True
    assert card["current_step_key"] == "review"
    assert card["assignee"] == "reviewer"
    assert card["running"] == 0
    assert task.status == "review"
    assert before_head == after_head
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["from_step"] == "test"
    assert handoff_events[0].payload["to_step"] == "review"
    assert handoff_events[0].payload["sha"] is None


def test_standalone_release_measure_review_evidence_advances_without_commit(
    kanban_home, tmp_path, monkeypatch
):
    """A review-step handoff can be evidence-only: independent review should
    move the card to Release / Measure without requiring a new code commit.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-clean-review"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    before_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )

        result = kb.complete_task(
            conn,
            tid,
            summary="Independent review passed",
            board=board,
            metadata={
                "workflow_outcome": {"verdict": "approved"},
                "ai_provenance": {
                    "writer": {"agent": "claude-code"},
                    "reviewer": {"agent": "codex"},
                }
            },
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert result is True
    assert card["current_step_key"] == "release_measure"
    assert card["assignee"] is None
    assert before_head == after_head
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["from_step"] == "review"
    assert handoff_events[0].payload["to_step"] == "release_measure"
    assert handoff_events[0].payload["sha"] is None


def test_complete_task_v2_with_unresolved_preflight_resumes_instead_of_handoff(
    kanban_home, tmp_path, monkeypatch,
):
    """A v2 card with an unresolved product preflight (the T3.3 obstacle chain
    routed it to the ``default`` resolver via ``block_task``) must RESUME
    to its original assignee/step when that
    resolver's turn ends via ``complete_task`` -- NOT be treated as real work
    and routed through the commit-first ``handoff()``, even though a diff is
    sitting uncommitted in the worktree.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-preflight"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        assert kb.block_task(
            conn,
            tid,
            reason="Need API credentials",
            kind="needs_input",
            attempted_resolutions=["checked env"],
            board=board,
            human_escalation_assignee="default",
        )
        blocked_card = _card_snapshot(conn, tid)
        assert blocked_card["assignee"] == "default"
        assert blocked_card["current_step_key"] == "development"
        resolver_run = kb.claim_task(conn, tid)
        assert resolver_run is not None and resolver_run.current_run_id is not None

        # A stray uncommitted diff is present in the worktree -- if the
        # buggy v2 branch fired here, it would wrongly commit it and
        # advance the card, mistaking obstacle-resolution for real work.
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = _resolve_preflight(
            conn,
            tid,
            resolver_run.current_run_id,
            board,
            reason="Found internal test token path",
        )

        card = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        head = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    assert result is True
    assert card["current_step_key"] == "development"
    assert card["assignee"] == "developer-profile"
    assert task.status == "ready"
    assert not any(event.kind == "handoff" for event in events)
    assert [event.kind for event in events].count("human_input_preflight_resolved") == 1
    # The legacy resume path never touches git -- the stray diff is still
    # sitting there uncommitted (it was NOT swept into a handoff commit).
    assert head != ""


def test_complete_task_v2_terminal_release_measure_requires_release_evidence(kanban_home):
    board = "v2-complete-task-terminal"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="release_measure",
        )

        with pytest.raises(kb.ReleaseEvidenceError):
            kb.complete_task(
                conn,
                tid,
                summary="Released and measured",
                board=board,
            )

        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "ready"
    assert not any(event.kind == "handoff" for event in events)


def test_complete_task_legacy_board_unchanged(kanban_home):
    """Non-v2 product boards keep using the legacy advance path unchanged
    (mirrors ``test_product_completion_advances_card_to_next_role``).
    """
    kb.create_board("prod-w1-legacy", preset="product")
    with kb.connect(board="prod-w1-legacy") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="architect-profile",
            workflow_template_id="product",
            current_step_key="architecture",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Architecture settled",
            board="prod-w1-legacy",
            product_role_assignees={"developer": "developer-profile"},
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest_run = kb.latest_run(conn, tid)
    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "developer-profile"
    assert latest_run.outcome == "advanced"
    advanced = [event for event in events if event.kind == "workflow_advanced"]
    assert advanced
    assert advanced[-1].payload["from_step"] == "architecture"
    assert advanced[-1].payload["to_step"] == "development"
    assert not any(event.kind == "handoff" for event in events)


# ---------------------------------------------------------------------------
# handoff() honors expected_run_id — stale reclaimed worker cannot advance
# (Codex P1: complete_task's v2 routing used to call handoff() without the
# worker's run id, so a RECLAIMED worker could still commit + advance.)
# ---------------------------------------------------------------------------

def test_complete_task_v2_stale_reclaimed_worker_cannot_advance(kanban_home, tmp_path, monkeypatch):
    """Real path: claim a v2 card, write a dirty diff, RECLAIM the claim
    (operator-driven, same as the dashboard recovery flow), then the OLD
    run's worker calls ``complete_task(..., expected_run_id=<old run id>)``.

    Must return False: no advance (current_step_key unchanged), no commit
    (HEAD unmoved, worktree still dirty), and no ``handoff`` event -- the
    ownership was revoked out from under it.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-stale-reclaim"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        stale_run_id = claimed.current_run_id
        assert stale_run_id is not None
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        before_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Operator (or crash detector) reclaims the claim -- ownership is
        # revoked, current_run_id cleared, card back to ready.
        assert kb.reclaim_task(conn, tid, reason="test reclaim") is True
        reclaimed_card = kb.get_task(conn, tid)
        assert reclaimed_card.current_run_id is None
        assert reclaimed_card.status == "ready"

        # The stale worker (still holding the OLD run id) tries to complete.
        result = kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            board=board,
            expected_run_id=stale_run_id,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert card["current_step_key"] == "development"
    assert not any(event.kind == "handoff" for event in events)
    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert after_head == before_head
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() != ""  # still dirty -- never staged/committed


def test_end_run_expected_id_cannot_close_new_owner(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="owned run", assignee="developer")
        first = kb.claim_task(conn, tid, claimer="old")
        assert first is not None and first.current_run_id is not None
        old_run_id = first.current_run_id
        assert kb.reclaim_task(conn, tid, reason="new owner") is True
        second = kb.claim_task(conn, tid, claimer="new")
        assert second is not None and second.current_run_id is not None
        new_run_id = second.current_run_id

        with kb.write_txn(conn):
            ended = kb._end_run(
                conn,
                tid,
                outcome="advanced",
                expected_run_id=old_run_id,
            )
        task = kb.get_task(conn, tid)
        old_run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id=?", (old_run_id,)
        ).fetchone()
        new_run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id=?", (new_run_id,)
        ).fetchone()

    assert ended is None
    assert task is not None and task.current_run_id == new_run_id
    assert old_run["ended_at"] is not None and old_run["outcome"] == "reclaimed"
    assert new_run["ended_at"] is None and new_run["outcome"] is None

def test_complete_task_v2_owning_worker_still_advances_with_expected_run_id(
    kanban_home, tmp_path, monkeypatch,
):
    """The current run's worker (expected_run_id == current_run_id, status
    running) must still commit + advance exactly as before -- passing
    expected_run_id must not regress the happy path.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-owning-worker"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            board=board,
            expected_run_id=run_id,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is True
    assert card["current_step_key"] == "test"
    assert card["assignee"] == "tester"
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1


def test_handoff_cas_race_loses_ownership_between_commit_and_advance(
    kanban_home, tmp_path, monkeypatch,
):
    """CAS on the advance UPDATE: even when the precheck passed, if
    ownership changes in the gap between the commit-first gate and the
    final advance (a competing reclaim races in), the advance must refuse
    (rowcount != 1) and emit no event. The diff is already committed at
    the git level by this point (can't be undone), but the DB/card must
    not advance nor record a handoff.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-cas-race"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        real_commit = kb._commit_worker_diff

        def _racing_commit(conn_, task_id_, *args, **kwargs):
            sha = real_commit(conn_, task_id_, *args, **kwargs)
            # Simulate a competing reclaim landing in the window between
            # the commit-first gate and the advance UPDATE below.
            conn_.execute(
                "UPDATE tasks SET current_run_id = NULL, status = 'ready' "
                "WHERE id = ?",
                (task_id_,),
            )
            return sha

        monkeypatch.setattr(kb, "_commit_worker_diff", _racing_commit)

        result = kb.handoff(
            conn, tid, board=board, expected_run_id=run_id,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert card["current_step_key"] == "development"
    assert not any(event.kind == "handoff" for event in events)

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""  # the commit itself DID happen -- git can't undo it


# ---------------------------------------------------------------------------
# spawn_after_handoff — event-driven fire-once spawn consumer (T3.1)
# ---------------------------------------------------------------------------

def test_spawn_after_handoff_fire_once_spawns_the_handed_off_card(kanban_home, tmp_path, monkeypatch):
    """One handoff -> spawn_after_handoff spawns the next-role agent exactly once."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-fire-once"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET running = 1 WHERE id = ?", (tid,))
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        assert result is True

        spawned_ids = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)

        task = kb.get_task(conn, tid)

    assert spawned_ids == [tid]
    assert len(spawns) == 1
    assert spawns[0][0] == tid
    assert task is not None
    assert task.status == "running"
    assert task.worker_pid == 4242
    assert task.assignee == "tester"


def test_spawn_after_handoff_second_pass_spawns_nothing(kanban_home, tmp_path, monkeypatch):
    """Regression guard: a second pass over an already-claimed card is a no-op (claim-CAS)."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-no-respawn"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET running = 1 WHERE id = ?", (tid,))
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        assert result is True

        first = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)
        second = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)

    assert first == [tid]
    assert second == []
    assert len(spawns) == 1  # spawn count stays <= 1 across both passes


def test_spawn_after_handoff_terminal_review_handoff_spawns_nothing(kanban_home, tmp_path, monkeypatch):
    """A review -> release_measure handoff leaves assignee=NULL: not a candidate, no spawn."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-terminal"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        (repo / "notes.md").write_text("looks good\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board,
            metadata={
                "ai_provenance": {
                    "writer": {"agent": "hermes"},
                    "reviewer": {"agent": "codex"},
                }
            },
        )
        assert result is True

        card = _card_snapshot(conn, tid)
        assert card["assignee"] is None

        spawned_ids = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)

    assert spawned_ids == []
    assert spawns == []


def test_spawn_after_handoff_legacy_board_is_noop(kanban_home, monkeypatch):
    """Non-v2 (legacy) boards never use spawn_after_handoff -- returns [], spawn_fn never called."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kb.connect() as conn:
        kb.create_task(conn, title="legacy card", assignee="developer")

        spawned_ids = kb.spawn_after_handoff(conn, spawn_fn=fake_spawn)

    assert spawned_ids == []
    assert spawns == []


# ---------------------------------------------------------------------------
# reconcile() -- bounded safety-net poller for handoff_v2 boards (T3.2)
# ---------------------------------------------------------------------------

def test_reconcile_recovers_dead_pid_then_spawns_next_pass_bounded(
    kanban_home, tmp_path, monkeypatch,
):
    """Dead-PID running card: pass 1 reclaims only (0 spawns); pass 2 spawns
    only (having become ready+idle). Never more than one action per pass --
    the direct regression guard against the multi-spawn storm."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-dead-pid"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    host = kb._claimer_id().split(":", 1)[0]
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, f"{host}:w0", stale_started_at, tid),
        )
        conn.commit()

        pass1 = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert pass1.reclaimed == [tid]
        assert pass1.spawned == []
        assert spawns == []  # zero spawns this pass -- the anti-storm guard

        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.claim_lock is None
        assert card.worker_pid is None

        pass2 = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert pass2.reclaimed == []
        assert pass2.spawned == [tid]
        assert spawns == [tid]  # exactly one spawn total, on pass 2

        card = kb.get_task(conn, tid)
        assert card.status == "running"


def test_reconcile_no_thrash_on_healthy_running_card(kanban_home, tmp_path, monkeypatch):
    """A running card with a LIVE pid gets no action -- repeatedly."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-healthy"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    host = kb._claimer_id().split(":", 1)[0]
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (os.getpid(), f"{host}:w0", stale_started_at, tid),
        )
        conn.commit()

        first = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        second = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)

    assert first.reclaimed == [] and first.spawned == []
    assert second.reclaimed == [] and second.spawned == []
    assert spawns == []


def test_reconcile_honors_crash_grace_period(kanban_home, tmp_path, monkeypatch):
    """A dead-reading PID whose worker just started (within the crash grace
    window) must NOT be reclaimed -- mirrors detect_crashed_workers' grace
    logic (#T3.2 review finding). Without this, reconcile's poll cadence can
    misclassify a freshly-spawned healthy worker as dead before its PID is
    visible on /proc, reintroducing the exact respawn churn reconcile exists
    to prevent."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-grace"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.delenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", raising=False)
    host = kb._claimer_id().split(":", 1)[0]

    now = 5_000_000.0
    monkeypatch.setattr(kb.time, "time", lambda: now)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, f"{host}:w0", int(now), tid),
        )
        conn.commit()

        # Just started (started_at == now): inside the grace window, so no
        # reclaim despite the dead-reading pid.
        within_grace = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert within_grace.reclaimed == []
        assert within_grace.spawned == []
        assert spawns == []
        card = kb.get_task(conn, tid)
        assert card.status == "running"

        # Past the default 30s grace window: now reclaim proceeds as before.
        monkeypatch.setattr(kb.time, "time", lambda: now + 60)
        past_grace = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert past_grace.reclaimed == [tid]
        assert past_grace.spawned == []
        card = kb.get_task(conn, tid)
        assert card.status == "ready"


def test_reconcile_skips_liveness_check_for_other_host_claim(
    kanban_home, tmp_path, monkeypatch,
):
    """A running card claimed by a different host is never reclaimed --
    ``_pid_alive`` checks the LOCAL process table, so a remote host's pid is
    meaningless (mirrors detect_crashed_workers' host-ownership guard)."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-other-host"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, "some-other-host:w0", stale_started_at, tid),
        )
        conn.commit()

        result = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)

    assert result.reclaimed == []
    assert result.spawned == []
    assert spawns == []


def test_reconcile_spawns_stranded_ready_card_idempotently(kanban_home, tmp_path, monkeypatch):
    """A ready+idle+spawnable card gets spawned once; a second pass (now
    running) is a no-op via the claim CAS."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-stranded-ready"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return os.getpid()  # a real, live pid so the second pass's own
        # dead-worker-recovery step doesn't reclaim it out from under us

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        assert kb.get_task(conn, tid).status == "ready"

        first = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        second = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)

    assert first.reclaimed == []
    assert first.spawned == [tid]
    assert second.reclaimed == []
    assert second.spawned == []
    assert spawns == [tid]  # spawn count stays <= 1 across both passes




def test_reconcile_legacy_board_is_noop(kanban_home, monkeypatch):
    """Non-v2 (legacy) boards never use reconcile -- empty result, spawn_fn never called."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kb.connect() as conn:
        kb.create_task(conn, title="legacy card", assignee="developer")

        result = kb.reconcile(conn, spawn_fn=fake_spawn)

    assert result.reclaimed == []
    assert result.spawned == []
    assert spawns == []


def test_reconcile_spawn_ready_false_recovers_but_skips_spawn(
    kanban_home, tmp_path, monkeypatch,
):
    """``spawn_ready=False`` skips ONLY step 2 (the stranded-ready spawn
    loop) while step 1 (dead-worker recovery) still runs. The gateway tick uses
    (Codex re-review P1): dispatch_once is already the tick's sole capped
    spawn owner, so reconcile in the tick must recover but not duplicate spawn,
    never spawn an arbitrary ready card."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-reconcile-spawn-ready-false"
    _v2_product_board_with_repo(board, repo)

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    host = kb._claimer_id().split(":", 1)[0]
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kb.connect(board=board) as conn:
        dead_tid = kb.create_task(conn, title="Dead worker story", assignee="developer")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, f"{host}:w0", stale_started_at, dead_tid),
        )
        conn.commit()
        ready_tid = kb.create_task(conn, title="Stranded ready story", assignee="developer")

        result = kb.reconcile(conn, board=board, spawn_fn=fake_spawn, spawn_ready=False)

        dead_card = kb.get_task(conn, dead_tid)
        ready_card = kb.get_task(conn, ready_tid)

    assert result.reclaimed == [dead_tid], "recovery (step 1) still runs"
    assert result.spawned == [], "the ready-spawn step (step 2) is skipped"
    assert spawns == []
    assert result.integrated == []

    assert dead_card.status == "ready", "dead-pid card was still re-idled"
    assert ready_card.status == "ready", "stranded ready card was NOT spawned"


# ---------------------------------------------------------------------------
# Scratch cleanup containment (#28818)
# ---------------------------------------------------------------------------



def test_complete_task_persists_scratch_artifacts_before_cleanup(kanban_home):
    """Completion artifacts from scratch workspaces survive workspace cleanup."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="render chart")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"png-bytes")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert persisted.exists(), "artifact copy should survive scratch cleanup"
    assert persisted.parent == kb.task_attachments_dir(t)
    assert persisted.name == "chart.png"
    assert persisted.read_bytes() == b"png-bytes"
    assert str(persisted) != str(artifact)
    assert run is not None
    assert run.metadata["artifacts"] == [str(persisted)]
    with kb.connect() as conn:
        attachments = kb.list_attachments(conn, t)
    assert [(a.filename, a.stored_path) for a in attachments] == [
        ("chart.png", str(persisted.resolve()))
    ]




# ---------------------------------------------------------------------------
# Deferred scratch cleanup for parent/child handoff (#33774)
# ---------------------------------------------------------------------------




def test_dir_child_completion_unblocks_deferred_scratch_parent(kanban_home, tmp_path):
    """A non-scratch ('dir') child completing must still sweep its scratch parent.

    Regression for the gap where ``_cleanup_workspace`` returned early for a
    non-scratch task and never ran the parent sweep — leaking the parent's
    deferred scratch dir forever.
    """
    child_dir = tmp_path / "persistent-child"
    child_dir.mkdir()
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(
            conn, title="dir child", workspace_kind="dir",
            workspace_path=str(child_dir),
        )
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)

        kb.complete_task(conn, parent, result="handoff")
        assert parent_ws.exists(), "deferred while dir child active"

        kb.complete_task(conn, child, result="built")

    assert not parent_ws.exists(), (
        "A 'dir' child completing must trigger the parent scratch sweep"
    )
    assert child_dir.exists(), "Non-scratch 'dir' child workspace is never deleted"




def test_is_managed_scratch_path_rejects_kanban_metadata_subtrees(kanban_home):
    """Hermes' own DB/metadata/log subtrees under ``<kanban_home>/kanban`` are NOT managed.

    Regression guard for the Copilot finding on #28819: a scratch task whose
    ``workspace_path`` was mis-set to the kanban home, the logs dir, or a
    board's metadata dir (i.e. the board root itself, not its ``workspaces/``
    child) must be refused. Without this, the containment check would happily
    ``shutil.rmtree`` Hermes' DB/metadata/logs on task completion.
    """
    kanban_root = kanban_home / "kanban"
    kanban_root.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(kanban_root)

    logs_dir = kanban_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(logs_dir)

    board_root = kanban_root / "boards" / "my-board"
    board_root.mkdir(parents=True, exist_ok=True)
    # The board root itself is NOT a managed scratch dir — only the
    # ``workspaces/`` child (and its descendants) are.
    assert not kb._is_managed_scratch_path(board_root)

    # Sibling subtrees of ``workspaces/`` under a board (e.g. its kanban.db
    # or board.json living next to ``workspaces/``) are also not managed.
    board_logs = board_root / "logs"
    board_logs.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(board_logs)

    # Now create the board's workspaces dir and a task scratch dir under it —
    # the latter is the only thing the guard should allow.
    board_workspaces = board_root / "workspaces"
    board_workspaces.mkdir(parents=True, exist_ok=True)
    # The workspaces root itself is also NOT managed — deleting it would
    # wipe every task's scratch dir at once.
    assert not kb._is_managed_scratch_path(board_workspaces)
    task_dir = board_workspaces / "task-42"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert kb._is_managed_scratch_path(task_dir)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------





def test_product_completion_advances_card_to_next_role(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="architect-profile",
            workflow_template_id="product",
            current_step_key="architecture",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Architecture settled",
            board="prod",
            product_role_assignees={"developer": "developer-profile"},
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest_run = kb.latest_run(conn, tid)
    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "developer-profile"
    assert latest_run.outcome == "advanced"
    advanced = [event for event in events if event.kind == "workflow_advanced"]
    assert advanced
    assert advanced[-1].payload["from_step"] == "architecture"
    assert advanced[-1].payload["to_step"] == "development"


def test_product_test_completion_moves_to_review_status(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="tester-profile",
            workflow_template_id="product",
            current_step_key="test",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Tests passed",
            metadata={
                "ai_provenance": {
                    "tester": {"agent": "hermes", "result": "passed"},
                }
            },
            board="prod",
            product_role_assignees={"reviewer": "reviewer-profile"},
        )
        task = kb.get_task(conn, tid)
    assert task.status == "review"
    assert task.current_step_key == "review"
    assert task.assignee == "reviewer-profile"


def test_product_development_completion_requires_writer_provenance(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
        )
        with pytest.raises(kb.ProductProvenanceError, match="Development completion"):
            kb.complete_task(
                conn,
                tid,
                summary="Implemented checkout",
                board="prod",
                product_role_assignees={"tester": "tester-profile"},
            )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.current_step_key == "development"
    assert task.assignee == "developer-profile"
    assert any(event.kind == kb.PRODUCT_PROVENANCE_BLOCKED_EVENT for event in events)


def test_product_development_completion_records_writer_provenance(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            metadata={
                "ai_provenance": {
                    "writer": {
                        "agent": "claude-code",
                        "model": "opus-4.8",
                        "branch": "feature/checkout",
                        "commit": "abc123",
                    }
                }
            },
            board="prod",
            product_role_assignees={"tester": "tester-profile"},
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest = kb.latest_ai_provenance_by_task(conn, [tid])[tid]
    assert task.current_step_key == "test"
    assert task.assignee == "tester-profile"
    assert latest["writer_agent"] == "claude-code"
    assert latest["branch"] == "feature/checkout"
    advanced = [event for event in events if event.kind == "workflow_advanced"]
    assert advanced[-1].payload["ai_provenance"]["writer_agent"] == "claude-code"


def test_product_review_completion_rejects_same_ai_as_writer(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "Claude Code"}}},
            board="prod",
            product_role_assignees={"tester": "tester-profile"},
        )
        conn.execute(
            "UPDATE tasks SET current_step_key='review', status='review', assignee='reviewer-profile' WHERE id=?",
            (tid,),
        )
        conn.commit()
        with pytest.raises(
            kb.ProductProvenanceError,
            match="canonical reviewer provider must differ",
        ):
            kb.complete_task(
                conn,
                tid,
                summary="Reviewed implementation",
                metadata={"ai_provenance": {"reviewer": {"agent": "claude-code"}}},
                board="prod",
            )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.current_step_key == "review"
    assert task.status == "review"
    rejected = [event for event in events if event.kind == kb.PRODUCT_PROVENANCE_BLOCKED_EVENT]
    assert rejected
    assert rejected[-1].payload["writer_agent"] == "Claude Code"
    assert rejected[-1].payload["reviewer_agent"] == "claude-code"


def test_product_review_completion_accepts_different_ai_reviewer(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "claude-code"}}},
            board="prod",
            product_role_assignees={"tester": "tester-profile"},
        )
        conn.execute(
            "UPDATE tasks SET current_step_key='review', status='review', assignee='reviewer-profile' WHERE id=?",
            (tid,),
        )
        conn.commit()
        assert kb.complete_task(
            conn,
            tid,
            summary="Reviewed implementation",
            metadata={
                "ai_provenance": {
                    "reviewer": {"agent": "codex", "verdict": "approved"},
                }
            },
            board="prod",
        )
        task = kb.get_task(conn, tid)
        provenance = kb.latest_ai_provenance_by_task(conn, [tid])[tid]
    assert task.current_step_key == "release_measure"
    assert provenance["writer_agent"] == "claude-code"
    assert provenance["reviewer_agent"] == "codex"
    assert provenance["review_rule"]["different_agent"] is True


def _stamp_test_runtime(
    monkeypatch,
    conn,
    task,
    *,
    provider: str,
    model: str,
    effort: str,
):
    identity = {
        "profile": task.assignee,
        "provider": provider,
        "model": model,
        "effort": effort,
        "surface": "claude-cli" if provider == "claude-cli" else "hermes-primary",
        "source": "dispatcher",
        "version": 1,
    }
    monkeypatch.setattr(
        kb, "_resolve_worker_runtime_identity", lambda _task: identity
    )
    assert kb._stamp_run_executor_identity(conn, task) == identity
    return identity


def test_dispatched_run_canonical_executor_overrides_writer_self_report(
    kanban_home, monkeypatch
):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="Canonical writer identity",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, tid, board="prod")
        assert claimed is not None
        identity = _stamp_test_runtime(
            monkeypatch,
            conn,
            claimed,
            provider="openai-codex",
            model="gpt-5.6-sol",
            effort="xhigh",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented directly.",
            metadata={
                "ai_provenance": {
                    "writer": {
                        "agent": "forged-claude",
                        "branch": "feature/canonical-writer",
                    }
                }
            },
            expected_run_id=claimed.current_run_id,
            board="prod",
            product_role_assignees={"tester": "tester"},
        )
        run = kb.get_run(conn, claimed.current_run_id)
        provenance = kb.latest_ai_provenance_by_task(conn, [tid])[tid]

    assert run is not None
    assert run.metadata["executor"] == identity
    assert run.metadata["ai_provenance"]["writer"]["agent"] == "openai-codex"
    assert run.metadata["ai_provenance"]["writer"]["model"] == "gpt-5.6-sol"
    assert run.metadata["ai_provenance"]["writer"]["effort"] == "xhigh"
    assert provenance["writer_agent"] == "openai-codex"
    assert provenance["branch"] == "feature/canonical-writer"


def test_review_independence_uses_canonical_provider_not_worker_alias(
    kanban_home, monkeypatch
):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="Canonical reviewer independence",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        writer = kb.claim_task(conn, tid, board="prod")
        assert writer is not None
        _stamp_test_runtime(
            monkeypatch,
            conn,
            writer,
            provider="openai-codex",
            model="gpt-5.6-sol",
            effort="xhigh",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented directly.",
            metadata={"ai_provenance": {"writer": {"agent": "anything"}}},
            expected_run_id=writer.current_run_id,
            board="prod",
            product_role_assignees={"tester": "tester"},
        )
        conn.execute(
            "UPDATE tasks SET current_step_key='review', status='review', "
            "assignee='reviewer' WHERE id=?",
            (tid,),
        )
        conn.commit()
        reviewer = kb.claim_review_task(conn, tid)
        assert reviewer is not None
        _stamp_test_runtime(
            monkeypatch,
            conn,
            reviewer,
            provider="openai-codex",
            model="different-model",
            effort="high",
        )

        with pytest.raises(
            kb.ProductProvenanceError,
            match="canonical reviewer provider must differ",
        ):
            kb.complete_task(
                conn,
                tid,
                summary="Claims an independent review.",
                metadata={
                    "ai_provenance": {
                        "reviewer": {"agent": "claude-cli", "verdict": "approved"}
                    }
                },
                expected_run_id=reviewer.current_run_id,
                board="prod",
            )

        rejected = [
            event for event in kb.list_events(conn, tid)
            if event.kind == kb.PRODUCT_PROVENANCE_BLOCKED_EVENT
        ][-1]
    assert rejected.payload["writer_agent"] == "openai-codex"
    assert rejected.payload["reviewer_agent"] == "openai-codex"


def test_review_rejects_partial_canonical_executor_identity(
    kanban_home, monkeypatch
):
    """A stamped reviewer must not compare against a legacy writer alias."""
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="Partial canonical reviewer identity",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        writer = kb.claim_task(conn, tid, board="prod")
        assert writer is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="Legacy unstamped development run.",
            metadata={
                "ai_provenance": {
                    "writer": {"agent": "legacy-writer-alias"}
                }
            },
            expected_run_id=writer.current_run_id,
            board="prod",
            product_role_assignees={"tester": "tester"},
        )
        conn.execute(
            "UPDATE tasks SET current_step_key='review', status='review', "
            "assignee='reviewer' WHERE id=?",
            (tid,),
        )
        conn.commit()
        reviewer = kb.claim_review_task(conn, tid)
        assert reviewer is not None
        _stamp_test_runtime(
            monkeypatch,
            conn,
            reviewer,
            provider="claude-cli",
            model="claude-opus-5",
            effort="high",
        )

        with pytest.raises(
            kb.ProductProvenanceError,
            match="canonical writer and reviewer executor identities",
        ):
            kb.complete_task(
                conn,
                tid,
                summary="Must not compare canonical identity with an alias.",
                metadata={
                    "ai_provenance": {
                        "reviewer": {
                            "agent": "claude-cli",
                            "verdict": "approved",
                        }
                    }
                },
                expected_run_id=reviewer.current_run_id,
                board="prod",
            )

        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == "review"
    assert task.status == "running"


def test_runtime_identity_uses_effective_per_model_cli_effort(
    kanban_home, monkeypatch, tmp_path
):
    """Canonical effort matches override resolution and CLI clamping."""
    import hermes_cli.config as config_module
    import hermes_cli.profiles as profiles_module

    profile_home = tmp_path / "reviewer-profile"
    profile_home.mkdir()
    monkeypatch.setattr(
        profiles_module, "resolve_profile_env", lambda _profile: profile_home
    )
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "model": {
                "provider": "claude-cli",
                "default": "claude-opus-5",
            },
            "agent": {
                "reasoning_effort": "high",
                "reasoning_overrides": {"claude-opus-5": "ultra"},
            },
        },
    )
    task = types.SimpleNamespace(
        assignee="reviewer",
        provider_override=None,
        model_override=None,
    )

    identity = kb._resolve_worker_runtime_identity(task)

    assert identity is not None
    assert identity["provider"] == "claude-cli"
    assert identity["model"] == "claude-opus-5"
    assert identity["effort"] == "max"


def test_strict_product_run_requires_canonical_runtime_identity(
    kanban_home, monkeypatch
):
    """Governed product dispatch fails closed when identity is unresolved."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Governed identity required",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        with kb.authorized_governance_write():
            conn.execute(
                "UPDATE board_governance "
                "SET qualification_required=1 WHERE id=1"
            )
        conn.commit()
        monkeypatch.setattr(
            kb, "_resolve_worker_runtime_identity", lambda _task: None
        )

        with pytest.raises(
            kb.WorkerRuntimeIdentityError,
            match="explicit provider, model, and effective effort",
        ):
            kb._stamp_run_executor_identity(conn, claimed)


def test_strict_product_run_rejects_identity_that_cannot_be_persisted(
    kanban_home, monkeypatch
):
    """A resolved identity is not enough when its active run has ended."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Governed identity persistence required",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        with kb.authorized_governance_write():
            conn.execute(
                "UPDATE board_governance "
                "SET qualification_required=1 WHERE id=1"
            )
        conn.execute(
            "UPDATE task_runs SET ended_at=? WHERE id=?",
            (int(time.time()), claimed.current_run_id),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_resolve_worker_runtime_identity",
            lambda _task: {
                "profile": "developer",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "surface": "hermes-primary",
                "source": "dispatcher",
                "version": 1,
            },
        )

        with pytest.raises(
            kb.WorkerRuntimeIdentityError,
            match="could not be persisted on the active run",
        ):
            kb._stamp_run_executor_identity(conn, claimed)


@pytest.mark.parametrize("review", [False, True])
def test_dispatch_records_runtime_identity_failure_and_blocks(
    kanban_home, monkeypatch, review
):
    """Both polling loops turn identity errors into bounded spawn failures."""
    import hermes_cli.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "profile_exists", lambda _name: True)

    def fail_stamp(_conn, _task):
        raise kb.WorkerRuntimeIdentityError("canonical identity unavailable")

    monkeypatch.setattr(kb, "_stamp_run_executor_identity", fail_stamp)
    spawned: list[str] = []
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Dispatch identity failure",
            assignee="reviewer" if review else "developer",
            workflow_template_id="product",
            current_step_key="review" if review else "development",
        )
        if review:
            conn.execute(
                "UPDATE tasks SET status='review' WHERE id=?",
                (tid,),
            )
            conn.commit()
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
            failure_limit=1,
        )
        task = kb.get_task(conn, tid)

    assert spawned == []
    assert result.auto_blocked == [tid]
    assert task is not None
    assert task.status == "blocked"
    assert task.last_failure_error == "canonical identity unavailable"


def test_product_human_block_routes_to_hermes_preflight_before_blocked(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
            initial_status="running",
        )
        assert kb.block_task(
            conn,
            tid,
            reason="Need API credentials",
            kind="needs_input",
            attempted_resolutions=["checked env", "checked docs"],
            board="prod",
            human_escalation_assignee="default",
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest_run = kb.latest_run(conn, tid)
    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "default"
    assert latest_run.outcome == "preflight"
    preflights = [event for event in events if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT]
    assert preflights
    assert preflights[-1].payload["original_assignee"] == "developer-profile"
    assert preflights[-1].payload["attempted_resolutions"] == ["checked env", "checked docs"]


def test_product_preflight_resolution_returns_card_to_original_assignee(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
            initial_status="running",
        )
        assert kb.block_task(
            conn,
            tid,
            reason="Need API credentials",
            kind="needs_input",
            attempted_resolutions=["checked env"],
            board="prod",
            human_escalation_assignee="default",
        )
        resolver_run = kb.claim_task(conn, tid)
        assert resolver_run is not None and resolver_run.current_run_id is not None
        assert _resolve_preflight(
            conn,
            tid,
            resolver_run.current_run_id,
            "prod",
            reason="Found internal test token path",
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest_run = kb.latest_run(conn, tid)
    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "developer-profile"
    assert latest_run.outcome == "preflight_resolved"
    assert [event.kind for event in events].count("human_input_preflight_resolved") == 1


def test_product_second_human_block_after_preflight_enters_blocked(kanban_home):
    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
            initial_status="running",
        )
        assert kb.block_task(
            conn,
            tid,
            reason="Need API credentials",
            kind="needs_input",
            attempted_resolutions=["checked env"],
            board="prod",
            human_escalation_assignee="default",
        )
        assert kb.block_task(
            conn,
            tid,
            reason="Hermes could not find a safe substitute credential",
            kind="needs_input",
            attempted_resolutions=["searched project docs", "checked local env"],
            board="prod",
            human_escalation_assignee="default",
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "blocked"
    assert task.current_step_key == "development"
    blocked = [event for event in events if event.kind == "blocked"]
    assert blocked
    assert blocked[-1].payload["attempted_resolutions"] == ["searched project docs", "checked local env"]


def test_list_runs_state_filter_requires_pair_and_valid_type(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="both"):
            kb.list_runs(conn, tid, state_type="status", state_name=None)
        with pytest.raises(ValueError, match="both"):
            kb.list_runs(conn, tid, state_type=None, state_name="done")
        with pytest.raises(ValueError, match="state_type"):
            kb.list_runs(conn, tid, state_type="nope", state_name="done")




# ---------------------------------------------------------------------------
# Originating session id (ACP propagation)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Shared-board path resolution (issue #19348)
#
# The kanban board is a cross-profile coordination primitive: a worker
# spawned with `hermes -p <profile>` must read/write the same kanban.db
# as the dispatcher that claimed the task. These tests exercise the
# path-resolution layer directly and would have caught the regression
# where `kanban_db_path()` resolved to the active profile's HERMES_HOME.
# ---------------------------------------------------------------------------

class TestSharedBoardPaths:
    """`kanban_home`/`kanban_db_path`/`workspaces_root`/`worker_log_path`
    must anchor at the **shared root**, not the active profile's HERMES_HOME."""

    def _set_home(self, monkeypatch, tmp_path, hermes_home):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)


    def test_profile_worker_resolves_to_shared_root(
        self, tmp_path, monkeypatch
    ):
        # Reproduces the bug: dispatcher uses ~/.hermes/kanban.db,
        # worker spawned with -p <profile> previously resolved to
        # ~/.hermes/profiles/<profile>/kanban.db. After the fix both
        # converge on ~/.hermes/kanban.db.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, profile_home)

        # All four resolvers must anchor at the shared root, not the
        # profile-local HERMES_HOME.
        assert kb.kanban_home() == default_home
        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"
        assert (
            kb.worker_log_path("t_0d214f19")
            == default_home / "kanban" / "logs" / "t_0d214f19.log"
        )

        # Sanity: the profile-local path that used to be returned is
        # explicitly NOT what we resolve to anymore.
        assert kb.kanban_db_path() != profile_home / "kanban.db"






    def test_dispatcher_and_worker_share_a_real_database(
        self, tmp_path, monkeypatch
    ):
        # Belt-and-suspenders: round-trip a task across the two
        # HERMES_HOME perspectives via a real SQLite file. Without the
        # fix the worker would open a different file and see no rows.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)

        # Dispatcher creates the board and a task.
        self._set_home(monkeypatch, tmp_path, default_home)
        kb.init_db()
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="cross-profile")

        # Worker switches to the profile HERMES_HOME and reads.
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.title == "cross-profile"




    def test_dispatcher_spawn_injects_kanban_paths_without_stale_session(
        self, tmp_path, monkeypatch
    ):
        # The dispatcher must pin board paths while stripping any unrelated
        # HERMES_SESSION_* identity inherited from the long-lived gateway.
        # The one exception is HERMES_SESSION_SOURCE, which the dispatcher
        # re-sets to its own `kanban` tag AFTER the strip — a value it owns,
        # never one inherited from whatever the gateway last routed.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        from gateway import session_context as sc

        # A dispatcher can launch before the gateway binds its first session.
        monkeypatch.setattr(sc, "_session_context_engaged", False)
        sc.reset_session_vars()
        for key in sc._VAR_MAP:
            monkeypatch.setenv(key, "stale-routing-value")

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = kb.Task(
            id="t_dispatch_env",
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_dispatch_env",
        )
        kb._default_spawn(task, str(tmp_path / "ws"))

        env = captured["env"]
        assert env["HERMES_KANBAN_DB"] == str(default_home / "kanban.db")
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
            default_home / "kanban" / "workspaces"
        )
        assert env["HERMES_KANBAN_TASK"] == "t_dispatch_env"
        assert env["HERMES_KANBAN_BRANCH"] == "wt/t_dispatch_env"
        for key in sc._VAR_MAP:
            if key == "HERMES_SESSION_SOURCE":
                # Re-set by the dispatcher, so what matters is that it carries
                # the worker's own tag rather than the inherited routing value.
                assert env[key] == "kanban"
                continue
            assert key not in env


# ---------------------------------------------------------------------------
# latest_summary / latest_summaries — surface task_runs.summary handoffs
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# NFS / network-filesystem fallback (see hermes_state.apply_wal_with_fallback)
# ---------------------------------------------------------------------------

def test_connect_falls_back_to_delete_on_locking_protocol(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must handle ``locking protocol`` on NFS/SMB.

    Without this fallback, the gateway's kanban dispatcher crashes every
    60s and the kanban migration (``consecutive_failures`` ADD COLUMN) is
    retried forever — which is what the real-world user report shows
    (see hermes-agent issue #22032).

    NOTE: We do NOT use the ``kanban_home`` fixture here because that
    fixture pre-initializes the DB via ``kb.init_db()`` — putting the
    file in WAL on disk. The Bug D safety guard now refuses to downgrade
    to DELETE when the on-disk header is already WAL, so testing the
    NFS-fallback path requires a truly-fresh DB file (NFS scenario in
    production: first connection of the first process ever to touch the
    file, where downgrading is safe because nobody else has WAL state
    yet).
    """
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # These tests exercise the WAL-attempt path; assume a fixed SQLite so the
    # WAL-reset vulnerability gate doesn't short-circuit before the pragma.
    import hermes_state as _hermes_state
    monkeypatch.setattr(
        _hermes_state, "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )
    _hermes_state._wal_fallback_warned_paths.clear()

    # Clear module cache so a fresh connect() is attempted
    kb._INITIALIZED_PATHS.clear()
    hermes_state._wal_fallback_warned_paths.clear()

    real_connect = _sqlite3.connect

    class _WalBlockingConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                raise _sqlite3.OperationalError("locking protocol")
            return super().execute(sql, *args, **kwargs)

    def wal_blocking_connect(*args, **kwargs):
        # connect_tracked passes a tracking-augmented factory; drop it and
        # substitute the double, which connect_tracked re-applies to the
        # returned instance.
        kwargs.pop("factory", None)
        return real_connect(
            *args, factory=_WalBlockingConnection, **kwargs
        )

    with _patch("hermes_cli.kanban_db.sqlite3.connect", side_effect=wal_blocking_connect):
        with caplog.at_level("ERROR", logger="hermes_state"):
            conn = kb.connect()

    # One fallback error, naming kanban.db
    errors = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "kanban.db" in r.getMessage()
    ]
    assert len(errors) >= 1, (
        f"Expected a kanban.db ERROR, got: {[r.getMessage() for r in caplog.records]}"
    )

    # DB still usable end-to-end — create + list a task
    t = kb.create_task(conn, title="post-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()


def test_connect_works_when_wal_is_silently_refused(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must stay usable when WAL silently no-ops to DELETE."""
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    hermes_state._wal_fallback_warned_paths.clear()
    # Assume a fixed SQLite so the WAL-reset gate doesn't short-circuit.
    monkeypatch.setattr(
        hermes_state, "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )

    real_connect = _sqlite3.connect

    class _WalSilentNoOpConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                return super().execute("PRAGMA journal_mode=delete", *args, **kwargs)
            return super().execute(sql, *args, **kwargs)

    def wal_silent_noop_connect(*args, **kwargs):
        kwargs.pop("factory", None)
        return real_connect(
            *args, factory=_WalSilentNoOpConnection, **kwargs
        )

    with _patch(
        "hermes_cli.kanban_db.sqlite3.connect",
        side_effect=wal_silent_noop_connect,
    ):
        with caplog.at_level("ERROR", logger="hermes_state"):
            conn = kb.connect()

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    t = kb.create_task(conn, title="post-silent-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()

    errors = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "kanban.db" in r.getMessage()
    ]
    assert len(errors) >= 1, (
        f"Expected a kanban.db ERROR, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_unlink_tasks_promotes_only_named_child(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="parent-done")
        kb.complete_task(conn, a)
        c = kb.create_task(conn, title="parent-running")
        kb.claim_task(conn, c, claimer="worker:1")
        b = kb.create_task(conn, title="child", parents=[a, c])
        unrelated = kb.create_task(conn, title="unrelated eligible todo")
        conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id = ?",
            (unrelated,),
        )
        conn.commit()
        assert kb.get_task(conn, b).status == "todo"
        assert kb.get_task(conn, unrelated).status == "todo"
        unrelated_before = (
            dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (unrelated,)).fetchone()),
            [(event.kind, event.payload) for event in kb.list_events(conn, unrelated)],
        )
        child = kb.get_task(conn, b)
        assert child is not None

        removed = kb.unlink_tasks(
            conn,
            c,
            b,
            expected={
                "status": child.status,
                "title": child.title,
                "assignee": child.assignee,
                "current_step_key": child.current_step_key,
                "current_run_id": child.current_run_id,
            },
        )
        assert removed is True
        assert kb.get_task(conn, b).status == "ready"
        assert (
            dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (unrelated,)).fetchone()),
            [(event.kind, event.payload) for event in kb.list_events(conn, unrelated)],
        ) == unrelated_before
        assert [event.kind for event in kb.list_events(conn, b)][-2:] == [
            "unlinked",
            "promoted",
        ]


def _unlink_expected(task):
    return {
        "status": task.status,
        "title": task.title,
        "assignee": task.assignee,
        "current_step_key": task.current_step_key,
        "current_run_id": task.current_run_id,
    }


def _unlink_db_state(conn, task_ids):
    rows = {
        task_id: dict(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
        for task_id in task_ids
    }
    events = {
        task_id: [
            (event.kind, event.payload, event.run_id)
            for event in kb.list_events(conn, task_id)
        ]
        for task_id in task_ids
    }
    edges = [
        tuple(row)
        for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall()
    ]
    return rows, events, edges


def test_unlink_tasks_keeps_todo_with_remaining_unsatisfied_parent(kanban_home):
    with kb.connect() as conn:
        removed_parent = kb.create_task(conn, title="removed parent")
        remaining_parent = kb.create_task(conn, title="remaining parent")
        child_id = kb.create_task(
            conn,
            title="still waiting",
            parents=[removed_parent, remaining_parent],
        )
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "todo"
        parents_before = _unlink_db_state(conn, [removed_parent, remaining_parent])

        assert kb.unlink_tasks(
            conn,
            removed_parent,
            child_id,
        )

        assert kb.get_task(conn, child_id).status == "todo"
        assert kb.parent_ids(conn, child_id) == [remaining_parent]
        parents_after = _unlink_db_state(conn, [removed_parent, remaining_parent])
        assert parents_after[:2] == parents_before[:2]
        assert parents_after[2] == [(remaining_parent, child_id)]
        assert [event.kind for event in kb.list_events(conn, child_id)][-1] == "unlinked"


@pytest.mark.parametrize("blocked_case", ["sticky", "failure_limit"])
def test_unlink_tasks_preserves_ineligible_block(blocked_case, kanban_home):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title=f"{blocked_case} parent")
        child_id = kb.create_task(conn, title=f"{blocked_case} child")
        if blocked_case == "sticky":
            claimed = kb.claim_task(conn, child_id)
            assert claimed is not None
            assert kb.block_task(conn, child_id, reason="human decision")
        else:
            conn.execute(
                "UPDATE tasks SET status = 'blocked', consecutive_failures = 1, "
                "max_retries = 1 WHERE id = ?",
                (child_id,),
            )
            conn.commit()
        kb.link_tasks(conn, parent_id, child_id)
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "blocked"

        assert kb.unlink_tasks(
            conn,
            parent_id,
            child_id,
            expected=_unlink_expected(child),
        )

        assert kb.get_task(conn, child_id).status == "blocked"
        assert [event.kind for event in kb.list_events(conn, child_id)][-1] == "unlinked"


def test_unlink_tasks_missing_edge_and_stale_snapshot_are_atomic(kanban_home):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent")
        other_parent = kb.create_task(conn, title="other parent")
        child_id = kb.create_task(conn, title="child", parents=[parent_id])
        stale = kb.get_task(conn, child_id)
        assert stale is not None
        conn.execute(
            "UPDATE tasks SET title = 'changed child' WHERE id = ?",
            (child_id,),
        )
        conn.commit()
        before_stale = _unlink_db_state(conn, [parent_id, other_parent, child_id])

        with pytest.raises(kb.TaskSnapshotConflict):
            kb.unlink_tasks(
                conn,
                parent_id,
                child_id,
                expected=_unlink_expected(stale),
            )
        assert _unlink_db_state(conn, [parent_id, other_parent, child_id]) == before_stale

        current = kb.get_task(conn, child_id)
        assert current is not None
        before_missing = _unlink_db_state(conn, [parent_id, other_parent, child_id])
        assert kb.unlink_tasks(
            conn,
            other_parent,
            child_id,
            expected=_unlink_expected(current),
        ) is False
        assert _unlink_db_state(conn, [parent_id, other_parent, child_id]) == before_missing



# ---------------------------------------------------------------------------
# _add_column_if_missing / _migrate_add_optional_columns idempotency (#21708)
# ---------------------------------------------------------------------------

def test_add_column_if_missing_is_idempotent_on_race(kanban_home):
    """``_add_column_if_missing`` must swallow 'duplicate column name' errors.

    Regression for #21708: the kanban dispatcher opens the DB twice per tick
    (once via _tick_once_for_board, once via init_db's discard-and-reconnect
    path).  A second concurrent connection runs _migrate_add_optional_columns
    before the first one commits, so ALTER TABLE raises OperationalError with
    'duplicate column name: consecutive_failures'.  Without the idempotency
    guard that crashes the dispatcher on the first tick after every restart.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )

    # First call adds the column — returns True.
    added = kb._add_column_if_missing(conn, "tasks", "extra_col", "extra_col TEXT")
    assert added is True
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "extra_col" in cols

    # Second call on same connection — column already exists — must return
    # False without raising, simulating the race the dispatcher hits.
    added_again = kb._add_column_if_missing(
        conn, "tasks", "extra_col", "extra_col TEXT"
    )
    assert added_again is False

    conn.close()


def test_migrate_add_optional_columns_tolerates_concurrent_migration(kanban_home):
    """Full _migrate_add_optional_columns must not raise when columns already
    exist (issue #21708 race window — two connections migrate concurrently)."""
    import sqlite3

    # Schema already in fully-migrated state (all optional columns present).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            branch_name TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid INTEGER,
            last_failure_error TEXT,
            max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER,
            current_run_id INTEGER,
            workflow_template_id TEXT,
            current_step_key TEXT,
            skills TEXT,
            max_retries INTEGER,
            session_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL DEFAULT '',
            run_id     INTEGER,
            kind       TEXT NOT NULL DEFAULT '',
            payload    TEXT,
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Running migration on an already-migrated schema must not raise.
    kb._migrate_add_optional_columns(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Dispatcher spawn invocation — _resolve_hermes_argv()
#
# Workers spawned by the dispatcher must use a `hermes` invocation that does
# not depend on PATH being set up correctly. cron jobs, systemd User= services,
# launchd jobs, and other detached processes routinely run with a stripped
# $PATH that doesn't include the venv's bin/, so a bare `["hermes", ...]`
# spawn fails with FileNotFoundError and the task gets stuck. The resolver
# prefers the PATH shim (familiar `ps` output) but falls back to the module
# form so the spawn keeps working when PATH is missing the shim.
# ---------------------------------------------------------------------------


def test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim(monkeypatch):
    """When the shim is not on PATH, fall back to `python -m hermes_cli.main`.

    Pins the correct module name (NOT `hermes` — there is no top-level
    `hermes` package). Regression for #23198: the original PR shipped
    `python -m hermes` which fails with `No module named hermes` on every
    invocation.
    """
    import shutil
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = kb._resolve_hermes_argv()
    assert argv == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_module_actually_runs():
    """The fallback module name must be importable + runnable.

    A unit test that pins the literal string is necessary but not
    sufficient — if `hermes_cli.main` ever loses `if __name__ == "__main__"`
    handling or its argparse setup, `python -m hermes_cli.main --version`
    would fail and so would every dispatcher spawn that hits the fallback.
    Run it as a real subprocess to catch that regression.
    """
    import subprocess
    import hermes_cli.kanban_db as kb
    import shutil
    import unittest.mock as mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kb._resolve_hermes_argv()
    r = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        f"`{' '.join(argv)} --version` failed (rc={r.returncode}); "
        f"stderr={r.stderr[:200]!r}"
    )
    assert "Hermes Agent" in r.stdout, f"unexpected output: {r.stdout[:200]!r}"


# ---------------------------------------------------------------------------
# task_age — guard against corrupt timestamp values
#
# The Task dataclass declares ``created_at: int`` but rows come from sqlite
# without coercion at the boundary. A row that ever held a non-int (e.g. an
# unsubstituted ``'%s'`` from a logged format string, ``None``, an arbitrary
# string, or a float-as-string) used to crash ``task_age`` with ``ValueError``
# and turn ``GET /api/plugins/kanban/board`` into a 500 because the dashboard
# calls ``task_age`` unguarded for every task in the response.
#
# After the fix, ``_safe_int`` returns ``None`` on bad input and ``task_age``
# degrades gracefully (per-field ``None`` rather than a hard crash).
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> "kb.Task":
    """Minimal Task with all required fields filled in. Override anything."""
    defaults = dict(
        id="t_age",
        title="x",
        body=None,
        assignee=None,
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    defaults.update(overrides)
    return kb.Task(**defaults)












# ---------------------------------------------------------------------------
# Board-level default_workdir
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# dispatch_once — max_in_progress
# ---------------------------------------------------------------------------


# Review column dispatch
# ---------------------------------------------------------------------------


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Test helper: set a task's status directly."""
    conn.execute(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (status, task_id),
    )








def test_dispatch_review_dry_run(kanban_home, all_assignees_spawnable):
    """dispatch_once dry-run sees review tasks and reports them as spawned."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == t
    # Dry run must NOT mutate status.
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "review"


def test_dispatch_review_does_not_force_profile_scoped_skill(
    kanban_home, all_assignees_spawnable,
):
    """Review lifecycle guidance comes from the reviewer profile."""
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42  # fake PID

    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=capture_spawn)
    assert len(res.spawned) == 1
    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].skills is None


def test_dispatch_review_skips_unassigned(kanban_home):
    """Unassigned review tasks go to skipped_unassigned, not spawned."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review floater")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_unassigned
    assert not res.spawned


def test_dispatch_review_counts_toward_max_spawn(
    kanban_home, all_assignees_spawnable,
):
    """Review spawns count against max_spawn alongside ready tasks."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        # Create 2 ready tasks + 1 review task, max_spawn=2
        t1 = kb.create_task(conn, title="ready 1", assignee="alice")
        t2 = kb.create_task(conn, title="ready 2", assignee="bob")
        t3 = kb.create_task(conn, title="review", assignee="alice")
        _set_task_status(conn, t3, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)
    # Only 2 should spawn (ready tasks get priority in the loop)
    assert len(res.spawned) == 2
    assert len(spawns) == 2


def test_dispatch_review_spawns_when_ready_empty(
    kanban_home, all_assignees_spawnable,
):
    """When only review tasks exist, they still get dispatched."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
    assert len(res.spawned) == 1
    assert spawns[0] == t


def _seed_product_review_worktree(
    conn, board: str, repo: Path, *, dirty=False, base_ref="main"
):
    tid = kb.create_task(
        conn,
        title="Review committed change",
        board=board,
        assignee="reviewer",
        workflow_template_id="product",
        current_step_key="review",
        workspace_kind="worktree",
        workspace_path=str(repo),
        max_retries=5,
    )
    task = kb.get_task(conn, tid)
    assert task is not None
    workspace, branch = kb._resolve_worktree_workspace(task, board=board)
    kb.set_workspace_path(conn, tid, str(workspace))
    kb.set_branch_name(conn, tid, branch)
    head_sha = _commit_file(
        workspace,
        "reviewed.txt",
        "immutable reviewer input\n",
        "reviewed change",
    )
    if dirty:
        (workspace / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", base_ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return tid, workspace, base_sha, head_sha


def _seed_product_test_worktree(conn, board: str, repo: Path):
    tid = kb.create_task(
        conn,
        title="Test committed change",
        board=board,
        assignee="tester",
        workflow_template_id="product",
        current_step_key="test",
        workspace_kind="worktree",
        workspace_path=str(repo),
        max_retries=5,
    )
    task = kb.get_task(conn, tid)
    assert task is not None
    workspace, branch = kb._resolve_worktree_workspace(task, board=board, conn=conn)
    kb.set_workspace_path(conn, tid, str(workspace))
    kb.set_branch_name(conn, tid, branch)
    return tid, workspace, branch


def test_dispatch_pins_test_target_before_tester_spawn(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "test-target-pinned"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    observed = []

    with kb.connect(board=board) as conn:
        tid, workspace, branch = _seed_product_test_worktree(conn, board, repo)

        def capture_spawn(task, launched_workspace, board=None):
            run = kb.get_run(conn, task.current_run_id)
            observed.append((launched_workspace, run.metadata))
            return 5252

        result = kb.dispatch_once(conn, board=board, spawn_fn=capture_spawn)

    head_sha = _git_output(workspace, "rev-parse", "HEAD")
    assert result.spawned[0][0] == tid
    assert observed == [
        (
            str(workspace),
            {"test_branch": branch, "test_head_sha": head_sha},
        )
    ]


def test_dispatch_pins_review_target_before_reviewer_spawn(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "review-target-pinned"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    observed = []

    with kb.connect(board=board) as conn:
        tid, workspace, base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo
        )

        def capture_spawn(task, launched_workspace, board=None):
            run = kb.get_run(conn, task.current_run_id)
            observed.append((launched_workspace, run.metadata))
            return 4242

        result = kb.dispatch_once(conn, board=board, spawn_fn=capture_spawn)

    assert result.spawned[0][0] == tid
    assert observed == [
        (
            str(workspace),
            {
                "review_branch": _git_output(workspace, "branch", "--show-current"),
                "review_base_sha": base_sha,
                "review_head_sha": head_sha,
            },
        )
    ]


def test_default_review_dispatch_requires_structural_target_contract(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    """A generic review card must not launch without an explicit target contract."""
    board = "default-review-contract"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    base_sha = _head_sha(repo)
    head_sha = _commit_file(repo, "reviewed.txt", "candidate\n", "candidate")
    _set_generated_path_policy(board, repo)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Default review without product step",
            board=board,
            assignee="reviewer",
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()

        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: pytest.fail("review launched without contract"),
        )
        task = kb.get_task(conn, tid)

    assert result.spawned == []
    assert task is not None and task.status == "blocked"
    assert task.current_step_key is None
    assert task.last_failure_error is not None
    assert "review execution contract" in task.last_failure_error
    assert base_sha != head_sha


@pytest.mark.parametrize("board_repository", [True, False])
@pytest.mark.parametrize("current_step_key", [None, "review"])
def test_default_review_dispatch_pins_completed_predecessor_target(
    kanban_home,
    tmp_path,
    all_assignees_spawnable,
    board_repository,
    current_step_key,
):
    board = "default-review-target-pinned"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    base_sha = _head_sha(repo)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "review-candidate"],
        check=True,
        capture_output=True,
        text=True,
    )
    head_sha = _commit_file(repo, "reviewed.txt", "candidate\n", "candidate")
    if board_repository:
        _set_generated_path_policy(board, repo)
    observed = []

    with kb.connect(board=board) as conn:
        predecessor_id = kb.create_task(
            conn,
            title="Completed test gate",
            board=board,
            assignee="tester",
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        kb.set_branch_name(conn, predecessor_id, "review-candidate")
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=1 WHERE id=?",
            (predecessor_id,),
        )
        predecessor_metadata = {"candidate_sha": head_sha}
        if not board_repository:
            predecessor_metadata["review_base_sha"] = base_sha
        predecessor_run_id = kb._synthesize_ended_run(
            conn,
            predecessor_id,
            outcome="completed",
            metadata=predecessor_metadata,
        )
        tid = kb.create_task(
            conn,
            title="Default immutable review",
            board=board,
            assignee="reviewer",
            parents=[predecessor_id],
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        kb.set_branch_name(conn, tid, "review-candidate")
        conn.execute(
            "UPDATE tasks SET status='review', current_step_key=? WHERE id=?",
            (current_step_key, tid),
        )
        conn.commit()

        def capture_spawn(task, launched_workspace, board=None):
            run = kb.get_run(conn, task.current_run_id)
            observed.append((launched_workspace, run.step_key, run.metadata))
            return 4242

        result = kb.dispatch_once(conn, board=board, spawn_fn=capture_spawn)
        task = kb.get_task(conn, tid)

    assert result.spawned[0][0] == tid
    assert task is not None
    assert task.workflow_template_id is None
    assert task.current_step_key == current_step_key
    assert predecessor_run_id > 0
    assert observed == [
        (
            str(repo),
            current_step_key,
            {
                "review_contract_kind": "default",
                "review_branch": "review-candidate",
                "review_base_sha": base_sha,
                "review_head_sha": head_sha,
            },
        )
    ]


@pytest.mark.parametrize(
    "invalid_contract",
    [
        "missing_candidate",
        "invalid_candidate",
        "missing_source_policy",
        "dirty_workspace",
        "mismatched_branch",
        "mismatched_repository",
        "moved_candidate",
        "wrong_active_profile",
        "absent_predecessor",
        "nonancestor_base",
    ],
)
def test_default_review_dispatch_rejects_invalid_target_contract_before_spawn(
    kanban_home, tmp_path, all_assignees_spawnable, monkeypatch, invalid_contract,
):
    board = f"default-review-invalid-{invalid_contract}"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    predecessor_base = None
    if invalid_contract == "nonancestor_base":
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "unrelated"],
            check=True,
            capture_output=True,
            text=True,
        )
        predecessor_base = _commit_file(
            repo, "unrelated.txt", "unrelated\n", "unrelated"
        )
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "review-candidate"],
        check=True,
        capture_output=True,
        text=True,
    )
    head_sha = _commit_file(repo, "reviewed.txt", "candidate\n", "candidate")
    configured_repo = repo
    if invalid_contract == "mismatched_repository":
        configured_repo = tmp_path / "configured-repo"
        _init_git_repo(configured_repo)
    _set_generated_path_policy(board, configured_repo)

    with kb.connect(board=board) as conn:
        predecessor_id = kb.create_task(
            conn,
            title="Completed Tester gate",
            board=board,
            assignee="tester",
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        kb.set_branch_name(
            conn,
            predecessor_id,
            "main" if invalid_contract == "mismatched_branch" else "review-candidate",
        )
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=1 WHERE id=?",
            (predecessor_id,),
        )
        predecessor_metadata = {
            "candidate_sha": (
                "not-a-full-sha" if invalid_contract == "invalid_candidate" else head_sha
            )
        }
        if invalid_contract == "missing_candidate":
            predecessor_metadata = {}
        elif predecessor_base is not None:
            predecessor_metadata["review_base_sha"] = predecessor_base
        kb._synthesize_ended_run(
            conn,
            predecessor_id,
            outcome="completed",
            metadata=predecessor_metadata,
        )
        tid = kb.create_task(
            conn,
            title="Invalid Default review target",
            board=board,
            assignee="reviewer",
            parents=[] if invalid_contract == "absent_predecessor" else [predecessor_id],
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=invalid_contract != "missing_source_policy",
        )
        kb.set_branch_name(conn, tid, "review-candidate")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        if invalid_contract == "dirty_workspace":
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        elif invalid_contract == "moved_candidate":
            _commit_file(repo, "moved.txt", "moved\n", "moved review head")
        if invalid_contract == "wrong_active_profile":
            original_stamp = kb._stamp_run_executor_identity

            def stamp_wrong_profile(active_conn, claimed):
                original_stamp(active_conn, claimed)
                active_conn.execute(
                    "UPDATE task_runs SET profile='default' WHERE id=?",
                    (claimed.current_run_id,),
                )
                active_conn.commit()

            monkeypatch.setattr(kb, "_stamp_run_executor_identity", stamp_wrong_profile)

        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 4242,
        )
        task = kb.get_task(conn, tid)

    assert result.spawned == []
    assert task is not None and task.status == "blocked"
    assert task.workflow_template_id is None
    assert task.current_step_key is None
    assert task.last_failure_error is not None
    assert "review target preparation" in task.last_failure_error
    if invalid_contract == "nonancestor_base":
        assert "not an ancestor" in task.last_failure_error


def test_non_review_source_forbidden_task_remains_outside_default_review_contract(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "default-read-only-non-review"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawned = []

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Read-only inspection",
            board=board,
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda task, workspace: spawned.append((task.id, workspace)) or 4242,
        )
        task = kb.get_task(conn, tid)

    assert result.spawned[0][0] == tid
    assert spawned == [(tid, str(repo))]
    assert task is not None and task.status == "running"
    assert task.last_failure_error is None


def test_pinned_review_target_survives_run_completion(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "review-target-audit"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, workspace, base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo
        )
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 4242,
        )
        assert result.spawned[0][0] == tid
        running_task = kb.get_task(conn, tid)
        assert running_task.current_run_id is not None
        run_id = running_task.current_run_id
        with kb.write_txn(conn):
            assert kb._end_run(
                conn,
                tid,
                outcome="completed",
                metadata={"findings": []},
                expected_run_id=run_id,
            ) == run_id
        run = kb.get_run(conn, run_id)

    assert run.metadata == {
        "findings": [],
        "review_branch": _git_output(workspace, "branch", "--show-current"),
        "review_base_sha": base_sha,
        "review_head_sha": head_sha,
    }


# Production baseline measured before R04: 56 ended Test runs contained six
# distinct SHAs and 76 ended Review runs contained zero SHAs.  These behavior
# tests cover the replacement contract instead of freezing those observations.


def _set_generated_path_policy(board: str, repo: Path, *paths: str) -> None:
    policy = {
        "base_ref": "refs/heads/main",
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": {
                "commands": [
                    {
                        "argv": ["python3", "-m", "unittest"],
                        "workdir": ".",
                        "timeout_seconds": 60,
                    }
                ]
            },
            "epic_release": {
                "commands": [
                    {
                        "argv": ["python3", "-m", "unittest"],
                        "workdir": ".",
                        "timeout_seconds": 60,
                    }
                ]
            },
        },
        "ci_observation": {
            "provider": "github_actions",
            "required_workflows": ["CI"],
        },
        "boundary_evidence": {
            "test_globs": ["tests/**"],
            "fixture_globs": ["tests/fixtures/**"],
            "generated_paths": list(paths),
        },
    }
    kb.write_board_metadata(
        board,
        default_workdir=str(repo),
        repository=policy,
    )


def test_test_completion_rejects_source_head_movement(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "test-evidence-head-moved"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        pinned = kb.get_run(conn, task.current_run_id)
        assert pinned is not None
        pinned_sha = pinned.metadata["test_head_sha"]
        _commit_file(workspace, "source.txt", "moved\n", "move test head")

        with pytest.raises(kb.EvidenceWorkspaceError, match="source_moved"):
            kb.complete_task(
                conn,
                tid,
                summary="Tests passed",
                board=board,
                metadata={
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {"tester": {"agent": "hermes"}},
                },
                expected_run_id=pinned.id,
            )

        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None and task.current_step_key == "test"
    assert task.status == "running"
    assert _git_output(workspace, "rev-parse", "HEAD") != pinned_sha
    assert not any(event.kind == "handoff" for event in events)


def test_test_completion_rejects_missing_dispatcher_pin(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "test-evidence-missing-pin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        run_id = task.current_run_id
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps({}), run_id),
        )
        with pytest.raises(kb.EvidenceWorkspaceError, match="missing_pin"):
            kb.complete_task(
                conn,
                tid,
                summary="Tests passed",
                board=board,
                metadata={
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {"tester": {"agent": "hermes"}},
                },
                expected_run_id=run_id,
            )
        current = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert current is not None and current.status == "running"
    assert current.current_step_key == "test"
    assert not any(event.kind == "handoff" for event in events)


def test_test_completion_restores_declared_generated_path_and_uses_pinned_sha(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "test-evidence-generated-restore"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    _set_generated_path_policy(board, repo, "README.md")

    with kb.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        run_id = task.current_run_id
        run = kb.get_run(conn, run_id)
        assert run is not None
        pinned_sha = run.metadata["test_head_sha"]
        (workspace / "README.md").write_text("generated evidence\n", encoding="utf-8")

        assert kb.complete_task(
            conn,
            tid,
            summary="Tests passed",
            board=board,
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {"tester": {"agent": "hermes"}},
            },
            expected_run_id=run_id,
        )
        closed = kb.get_run(conn, run_id)
        events = kb.list_events(conn, tid)

    assert (workspace / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert closed is not None and closed.metadata is not None
    assert closed.metadata["evidence_workspace"]["declared_generated"] == ["README.md"]
    generated_event = next(
        event for event in events if event.kind == "evidence_generated_mutations"
    )
    assert generated_event.payload == {"run_id": run_id, "paths": ["README.md"]}
    handoff = next(event for event in events if event.kind == "handoff")
    assert handoff.payload["sha"] == pinned_sha


def test_test_completion_preserves_nonignored_untracked_output_and_rejects(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "test-evidence-untracked"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        (workspace / "artifact.txt").write_text("diagnosis\n", encoding="utf-8")

        with pytest.raises(kb.EvidenceWorkspaceError, match="untracked_output"):
            kb.complete_task(
                conn,
                tid,
                summary="Tests passed",
                board=board,
                metadata={
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {"tester": {"agent": "hermes"}},
                },
                expected_run_id=task.current_run_id,
            )
        current = kb.get_task(conn, tid)

    assert current is not None and current.current_step_key == "test"
    assert (workspace / "artifact.txt").read_text(encoding="utf-8") == "diagnosis\n"


def test_test_completion_rejects_undeclared_tracked_mutation(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "test-evidence-undeclared-tracked"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        (workspace / "README.md").write_text("source edit\n", encoding="utf-8")

        with pytest.raises(kb.EvidenceWorkspaceError, match="source_moved"):
            kb.complete_task(
                conn,
                tid,
                summary="Tests passed",
                board=board,
                metadata={
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {"tester": {"agent": "hermes"}},
                },
                expected_run_id=task.current_run_id,
            )

    assert (workspace / "README.md").read_text(encoding="utf-8") == "source edit\n"


def test_review_dispatch_requires_the_latest_test_sha_when_one_is_pinned(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "review-evidence-tested-sha"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, workspace, _base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo
        )
        with kb.write_txn(conn):
            kb._synthesize_ended_run(
                conn,
                tid,
                outcome="advanced",
                step_key="test",
                metadata={
                    "test_branch": kb._git_current_branch(workspace),
                    "test_head_sha": head_sha,
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {
                        "writer": {"agent": "writer"},
                        "tester": {"agent": "hermes"},
                    },
                },
            )
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        run = kb.get_run(conn, task.current_run_id)

    assert result.spawned[0][0] == tid
    assert run is not None
    assert run.metadata["review_head_sha"] == head_sha


def test_review_completion_rejects_source_edit_without_authoring_a_commit(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "review-evidence-source-edit"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, workspace, _base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo
        )
        task = kb.get_task(conn, tid)
        assert task is not None and task.branch_name
        with kb.write_txn(conn):
            kb._synthesize_ended_run(
                conn,
                tid,
                outcome="advanced",
                step_key="test",
                metadata={
                    "test_branch": task.branch_name,
                    "test_head_sha": head_sha,
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {
                        "writer": {"agent": "writer"},
                        "tester": {"agent": "hermes"},
                    },
                },
            )
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        running = kb.get_task(conn, tid)
        assert running is not None and running.current_run_id is not None
        before_head = _git_output(workspace, "rev-parse", "HEAD")
        (workspace / "README.md").write_text("reviewer source edit\n", encoding="utf-8")

        with pytest.raises(kb.EvidenceWorkspaceError, match="source_moved"):
            kb.handoff(
                conn,
                tid,
                board=board,
                summary="Approved review",
                metadata={
                    "workflow_outcome": {"verdict": "approved"},
                    "ai_provenance": {
                        "writer": {"agent": "writer"},
                        "reviewer": {"agent": "reviewer", "verdict": "approved"},
                    },
                },
                expected_run_id=running.current_run_id,
                expected_phase="review",
            )

    assert _git_output(workspace, "rev-parse", "HEAD") == before_head
    assert (workspace / "README.md").read_text(encoding="utf-8") == "reviewer source edit\n"


def test_review_closure_keeps_dispatcher_pins_over_worker_claims(
    kanban_home, tmp_path, all_assignees_spawnable, monkeypatch,
):
    board = "review-target-authoritative-at-closure"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    reviewer_executor = {
        "profile": "reviewer",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "surface": "hermes-primary",
        "source": "dispatcher",
        "version": 1,
    }
    writer_executor = {
        "profile": "developer",
        "provider": "anthropic",
        "model": "claude-opus-4.5",
        "effort": "high",
        "surface": "hermes-primary",
        "source": "dispatcher",
        "version": 1,
    }
    worker_head_sha = "b" * 40
    worker_base_sha = "c" * 40
    worker_executor = {
        "profile": "reviewer",
        "provider": "worker-claim",
        "model": "worker-model",
        "effort": "worker-effort",
        "surface": "worker-surface",
    }
    monkeypatch.setattr(
        kb, "_resolve_worker_runtime_identity", lambda _task: reviewer_executor,
    )

    with kb.connect(board=board) as conn:
        tid, _workspace, pinned_base_sha, pinned_head_sha = (
            _seed_product_review_worktree(conn, board, repo)
        )
        review_task = kb.get_task(conn, tid)
        assert review_task is not None and review_task.branch_name
        branch = review_task.branch_name
        with kb.write_txn(conn):
            kb._synthesize_ended_run(
                conn,
                tid,
                outcome="advanced",
                step_key="development",
                metadata={
                    "executor": writer_executor,
                    "ai_provenance": {
                        "writer": {"agent": writer_executor["provider"]},
                    },
                },
            )
            kb._synthesize_ended_run(
                conn,
                tid,
                outcome="advanced",
                step_key="test",
                metadata={
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {
                        "writer": {"agent": writer_executor["provider"]},
                        "tester": {"agent": "hermes", "result": "passed"},
                    },
                    "test_branch": branch,
                    "test_head_sha": pinned_head_sha,
                },
            )
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 4242,
        )
        assert result.spawned[0][0] == tid
        running_task = kb.get_task(conn, tid)
        assert running_task is not None and running_task.current_run_id is not None
        run_id = running_task.current_run_id
        assert kb.handoff(
            conn,
            tid,
            board=board,
            summary="Approved the dispatcher-pinned review target.",
            metadata={
                "review_base_sha": worker_base_sha,
                "review_head_sha": worker_head_sha,
                "executor": worker_executor,
                "workflow_outcome": {"verdict": "approved"},
                "ai_provenance": {
                    "writer": {"agent": writer_executor["provider"]},
                    "reviewer": {
                        "agent": reviewer_executor["provider"],
                        "verdict": "approved",
                        "reviewed_branch": branch,
                        "reviewed_commit": worker_head_sha,
                    },
                },
            },
            expected_run_id=run_id,
            expected_phase="review",
        )
        persisted = kb.get_run(conn, run_id)
        evidence = kb._release_run_evidence(conn, tid, branch, pinned_head_sha)
        with pytest.raises(kb.ReleaseEvidenceError) as exc_info:
            kb._release_run_evidence(conn, tid, branch, worker_head_sha)
        conflict_events = [
            event
            for event in kb.list_events(conn, tid)
            if event.kind == "dispatcher_metadata_conflict"
        ]

    assert persisted is not None
    assert persisted.metadata["review_base_sha"] == pinned_base_sha
    assert persisted.metadata["review_head_sha"] == pinned_head_sha
    assert persisted.metadata["executor"] == reviewer_executor
    assert (
        persisted.metadata["ai_provenance"]["reviewer"]["reviewed_commit"]
        == pinned_head_sha
    )
    assert evidence["review_run_id"] == run_id
    assert "reviewed_candidate" in exc_info.value.missing
    assert conflict_events[-1].payload == {
        "conflicts": {
            "review_base_sha": {
                "dispatcher": pinned_base_sha,
                "worker": worker_base_sha,
            },
            "review_head_sha": {
                "dispatcher": pinned_head_sha,
                "worker": worker_head_sha,
            },
            "executor": {
                "dispatcher": reviewer_executor,
                "worker": worker_executor,
            },
        }
    }


def test_dispatch_blocks_dirty_review_target_before_spawn(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "review-target-dirty"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    spawned = []

    with kb.connect(board=board) as conn:
        tid, _workspace, _base_sha, _head_sha = _seed_product_review_worktree(
            conn, board, repo, dirty=True
        )
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: spawned.append(args) or 4242,
            failure_limit=5,
        )
        task = kb.get_task(conn, tid)

    assert spawned == []
    assert tid in result.auto_blocked
    assert task.status == "blocked"
    assert "review target preparation" in task.last_failure_error
    assert "dirty" in task.last_failure_error


def test_review_target_preparation_rejects_workspace_mismatch(
    kanban_home, tmp_path,
):
    board = "review-target-workspace-mismatch"
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    _init_git_repo(repo)
    _init_git_repo(other)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, _workspace, _base_sha, _head_sha = _seed_product_review_worktree(
            conn, board, repo
        )
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(
            kb.ReviewTargetPreparationError,
            match="does not match task workspace",
        ):
            kb._prepare_review_target(conn, tid, other, board=board)


def test_dispatch_review_pins_against_board_checkout_branch(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "review-target-develop"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "develop", str(repo)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"],
        check=True,
    )
    _v2_product_board_with_repo(board, repo)
    observed = []

    with kb.connect(board=board) as conn:
        tid, workspace, base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo, base_ref="develop"
        )

        def capture_spawn(task, launched_workspace, board=None):
            observed.append(kb.get_run(conn, task.current_run_id).metadata)
            return 4242

        result = kb.dispatch_once(conn, board=board, spawn_fn=capture_spawn)

    assert result.spawned[0][0] == tid
    assert observed == [
        {
            "review_branch": _git_output(workspace, "branch", "--show-current"),
            "review_base_sha": base_sha,
            "review_head_sha": head_sha,
        }
    ]
    assert workspace.name == tid


def test_dispatch_custom_review_assignee_does_not_require_reviewer_pin(
    kanban_home, all_assignees_spawnable,
):
    spawned = []

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="custom non-Claude review",
            assignee="alice",
            workflow_template_id="product",
            current_step_key="review",
        )
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append(args) or 4242,
        )

    assert result.spawned[0][0] == tid
    assert len(spawned) == 1


def test_unexpected_review_pin_failure_blocks_without_aborting_dispatch(
    kanban_home, tmp_path, all_assignees_spawnable, monkeypatch,
):
    board = "review-target-unexpected-failure"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kb.connect(board=board) as conn:
        tid, _workspace, _base_sha, _head_sha = _seed_product_review_worktree(
            conn, board, repo
        )
        monkeypatch.setattr(
            kb,
            "_prepare_review_target",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
            ),
        )

        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 4242,
        )
        task = kb.get_task(conn, tid)

    assert tid in result.auto_blocked
    assert result.spawned == []
    assert task.status == "blocked"
    assert "review target preparation" in task.last_failure_error


def test_review_git_output_decodes_unusual_bytes_lossily(
    kanban_home, monkeypatch, tmp_path,
):
    observed = {}

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return Result()

    monkeypatch.setattr(kb.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(kb.subprocess, "run", fake_run)

    assert kb._review_git_output(tmp_path, "status") == "ok"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"


def test_has_spawnable_review_true(kanban_home):
    """has_spawnable_review returns True when review tasks exist with real profiles."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="default")
        _set_task_status(conn, t, "review")
        # default profile should exist in the test env
        assert kb.has_spawnable_review(conn) is True


def test_has_spawnable_review_false_on_empty(kanban_home):
    """has_spawnable_review returns False when no review tasks exist."""
    with kb.connect() as conn:
        assert kb.has_spawnable_review(conn) is False


def test_has_spawnable_review_false_when_only_terminal_lanes(
    kanban_home, monkeypatch,
):
    """has_spawnable_review returns False when review tasks are terminal lanes."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        assert kb.has_spawnable_review(conn) is False


def test_dispatch_review_skips_nonspawnable(kanban_home, monkeypatch):
    """Review tasks with non-existent profiles go to skipped_nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        res = kb.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert not res.spawned


def test_review_status_in_valid_statuses():
    """'review' is a valid task status."""
    assert "review" in kb.VALID_STATUSES


def test_dispatch_review_does_not_claim_ready_tasks(
    kanban_home, all_assignees_spawnable,
):
    """Review dispatch uses claim_review_task, which only claims review tasks."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="ready task", assignee="alice")
        # claim_review_task should NOT claim a ready task
        claimed = kb.claim_review_task(conn, t)
    assert claimed is None

# Stale detection — detect_stale_running
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Corruption guard (issue #30687)
# ---------------------------------------------------------------------------

def _write_corrupt_db(path: Path) -> bytes:
    """Write a kanban DB with a VALID SQLite header but malformed page content.

    This is the corruption shape the integrity guard specifically targets
    (e.g. issue #29507 follow-up reports where the file's first 16 bytes
    pass the header byte check but ``PRAGMA integrity_check`` then fails
    because the internal pages are damaged). It's what main's header-only
    validator was letting through, and what this PR adds the full guard
    for.
    """
    # 100-byte SQLite header (magic + minimal valid-looking fields) so the
    # cheap header check passes, then deliberate garbage so sqlite refuses
    # to read the file past the header.
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob




def test_repeated_corrupt_open_reuses_single_backup(tmp_path):
    """Repeated quarantines of the same corrupt bytes must not amplify disk usage.

    Regression for the gateway dispatcher's 5-min retry loop on shared kanban
    DBs across multi-profile fleets: each retry on an unchanged corrupt file
    used to create a fresh ``.corrupt.<timestamp>.bak`` until disk filled. The
    content-addressed backup name is deterministic in the DB's sha256, so
    N retries of the same bytes share one backup.
    """
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)

    backups: set[Path] = set()
    for _ in range(10):
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)
        assert excinfo.value.backup_path is not None
        backups.add(excinfo.value.backup_path)

    assert len(backups) == 1, f"expected 1 deterministic backup, got {len(backups)}"
    (backup,) = backups
    assert backup.exists()
    assert backup.read_bytes() == original

    # Mutate the corrupt bytes — fingerprint changes, separate backup preserved.
    with db_path.open("r+b") as f:
        f.seek(4096)
        f.write(b"\xAB" * 64)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with pytest.raises(kb.KanbanDbCorruptError) as excinfo2:
        kb.connect(db_path=db_path)
    second_backup = excinfo2.value.backup_path
    assert second_backup is not None
    assert second_backup != backup
    assert second_backup.exists()


def test_locked_healthy_db_does_not_classify_as_corrupt(tmp_path, monkeypatch):
    """A transient lock during the probe must not produce a .corrupt backup
    and must not be reported as :class:`KanbanDbCorruptError`. Raw sqlite
    ``OperationalError`` (lock/busy) is acceptable and expected."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        # First call is the integrity probe — simulate a lock.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)

    with pytest.raises(sqlite3.OperationalError):
        kb.connect(db_path=db_path)

    # No .corrupt backup may be produced for a healthy-but-locked DB.
    backups = list(tmp_path.glob("*.corrupt.*"))
    assert backups == [], f"unexpected corrupt backups: {backups}"

    # And once the lock clears, normal access still works.
    monkeypatch.setattr(kb.sqlite3, "connect", real_connect)
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="still here")
        titles = [t.title for t in kb.list_tasks(conn)]
    assert "still here" in titles




# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------

def test_maybe_emit_scratch_tip_fires_once_per_install(kanban_home, caplog):
    """First scratch workspace materialization warns + emits an event.

    Subsequent scratch workspaces on the SAME install stay silent — the
    sentinel file under kanban_home() flips after the first emit.
    """
    import logging

    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="first scratch")
        t2 = kb.create_task(conn, title="second scratch")

    # Sentinel must not exist yet on a fresh install.
    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t1, "scratch")

    # Sentinel is now set.
    assert kb._scratch_tip_shown()
    assert kb._scratch_tip_sentinel_path().exists()

    # Warning was logged exactly once.
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert len(tip_records) == 1, (
        f"Expected exactly one tip warning, got {len(tip_records)}: "
        f"{[r.getMessage() for r in tip_records]!r}"
    )

    # An event row was appended on the first task.
    with kb.connect() as conn:
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t1,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "tip_scratch_workspace" in kinds, (
        f"Expected tip_scratch_workspace event on first scratch task; "
        f"got {kinds!r}"
    )

    # Second scratch materialization on the same install stays silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t2, "scratch")
    tip_records2 = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records2 == [], (
        f"Tip should not re-fire after sentinel is set; got "
        f"{[r.getMessage() for r in tip_records2]!r}"
    )
    with kb.connect() as conn:
        events2 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t2,),
        ).fetchall()
    assert "tip_scratch_workspace" not in [e["kind"] for e in events2], (
        "Tip event should not be appended for subsequent scratch tasks."
    )




# ---------------------------------------------------------------------------
# Connection pragmas (secure_delete, cell_size_check, synchronous=FULL)
# ---------------------------------------------------------------------------


def test_connect_sets_secure_delete_on(tmp_path):
    """secure_delete=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA secure_delete").fetchone()
    assert row[0] == 1, f"expected secure_delete=1, got {row[0]}"




def test_connect_sets_synchronous_full(tmp_path):
    """synchronous must be FULL (=2), not NORMAL (=1)."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA synchronous").fetchone()
    assert row[0] == 2, f"expected synchronous=2 (FULL), got {row[0]}"


def test_product_backlog_completion_advances_to_architecture(kanban_home, monkeypatch):
    """PO completion on a product Backlog card hands the same card to Architect.

    Regression guard for product boards where a Product Owner worker marked a
    backlog story ``done`` instead of moving it to the Architecture step. The
    story card must stay alive as the same task, switch step key, and dispatch
    to the architect role.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    board = "product-handoff"
    kb.create_board(board, name="Product Handoff", preset="product")

    with kb.connect(board=board) as conn:
        tid = kb.create_task(conn, title="Story: choose a board", assignee="productowner")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workflow_template_id = 'product', "
                "current_step_key = 'backlog' WHERE id = ?",
                (tid,),
            )
        claimed = kb.claim_task(conn, tid)

        ok = kb.complete_task(
            conn,
            tid,
            summary="Product Owner confirms this story is ready for Architecture.",
            expected_run_id=claimed.current_run_id,
            board=board,
        )

        task = kb.get_task(conn, tid)
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()

    assert ok is True
    assert task.status == "ready"
    assert task.assignee == "architect"
    assert task.workflow_template_id == "product"
    assert task.current_step_key == "architecture"
    assert task.completed_at is None
    advanced = [e for e in events if e["kind"] == "workflow_advanced"]
    assert len(advanced) == 1
    assert '\"from_step\": \"backlog\"' in advanced[0]["payload"]
    assert '\"to_step\": \"architecture\"' in advanced[0]["payload"]
    assert '\"assignee\": \"architect\"' in advanced[0]["payload"]


def test_product_release_measure_can_satisfy_dependencies_for_autonomous_boards(kanban_home, monkeypatch):
    """Autonomous product boards must not stall child coding at Release/Measure.

    Release / Measure remains visible as a product bucket, but boards that opt
    into autonomous dependency flow should let dependent Architecture/Developer
    work continue once a parent reaches that bucket.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    board = "autonomous-product"
    kb.create_board(board, name="Autonomous Product", preset="product")
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["release_measure_unblocks_dependents"] = True
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with kb.connect(board=board) as conn:
        parent = kb.create_task(conn, title="Story: approved prerequisite")
        child = kb.create_task(conn, title="Story: next autonomous slice", assignee="architect")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workflow_template_id = 'product', status = 'ready', "
                "current_step_key = 'release_measure', assignee = NULL WHERE id = ?",
                (parent,),
            )
            conn.execute(
                "UPDATE tasks SET workflow_template_id = 'product', status = 'todo', "
                "current_step_key = 'architecture' WHERE id = ?",
                (child,),
            )

        promoted = kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)

        child_task = kb.get_task(conn, child)
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (child,),
        ).fetchall()

    assert promoted == 1
    assert claimed is not None
    assert child_task.status == "running"
    assert child_task.assignee == "architect"
    assert not [e for e in events if e["kind"] == "claim_rejected"]


def test_product_release_measure_still_blocks_dependencies_without_autonomy_opt_in(kanban_home, monkeypatch):
    """Legacy/product boards keep the explicit human release gate by default."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    board = "manual-release-product"
    kb.create_board(board, name="Manual Release Product", preset="product")

    with kb.connect(board=board) as conn:
        parent = kb.create_task(conn, title="Story: release gate")
        child = kb.create_task(conn, title="Story: blocked child", assignee="architect")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workflow_template_id = 'product', status = 'ready', "
                "current_step_key = 'release_measure', assignee = NULL WHERE id = ?",
                (parent,),
            )
            conn.execute(
                "UPDATE tasks SET workflow_template_id = 'product', status = 'ready', "
                "current_step_key = 'architecture' WHERE id = ?",
                (child,),
            )

        promoted = kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)
        child_task = kb.get_task(conn, child)
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (child,),
        ).fetchall()

    assert promoted == 0
    assert claimed is None
    assert child_task.status == "todo"
    rejected = [e for e in events if e["kind"] == "claim_rejected"]
    assert rejected
    assert '\"reason\": \"parents_not_done\"' in rejected[-1]["payload"]


def test_connect_pragmas_applied_on_reconnect(tmp_path):
    """All three pragmas must be re-applied on every connect(), not just the first."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # First connection: write a task and close.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="reconnect-check")
    # Force re-init path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Second connection: pragmas must still be applied.
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2



def test_pragmas_not_accidentally_disabled_by_migrate_path(tmp_path):
    """Migration path must not reset connection pragmas."""
    db_path = tmp_path / "legacy.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Initialise with a fresh connect so schema + init run.
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="pre-migration-task")
    # Simulate a re-entry through the init/migration path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2

# write_txn — rollback handler must not mask the original exception
# ---------------------------------------------------------------------------


def test_write_txn_preserves_original_exception_when_rollback_fails(kanban_home):
    """When a write inside write_txn raises an OperationalError that SQLite
    has already auto-rolled-back (e.g. ``disk I/O error``,
    ``database is locked``, ``database disk image is malformed``), the
    explicit ROLLBACK in ``write_txn.__exit__`` itself raises
    ``cannot rollback - no transaction is active``. The original cause
    must NOT be masked by the secondary rollback failure — operators rely
    on the original cause to diagnose the underlying issue.
    """

    class FailingConnWrapper:
        """Delegate to a real connection, simulating an EIO during an INSERT
        that SQLite has already auto-rolled-back."""

        def __init__(self, real):
            self._real = real
            self._fail_armed = True

        def execute(self, sql, *args, **kwargs):
            if (
                self._fail_armed
                and sql.lstrip().upper().startswith("INSERT")
                and "task_events" in sql.lower()
            ):
                self._fail_armed = False  # one-shot
                # Simulate SQLite auto-rolling back the transaction by
                # issuing a real ROLLBACK now. After this, BEGIN IMMEDIATE
                # is no longer active and an explicit ROLLBACK would error.
                try:
                    self._real.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with kb.connect() as conn:
        wrapper = FailingConnWrapper(conn)
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            with kb.write_txn(wrapper):
                kb._append_event(wrapper, "t_bogus", "promoted", None)

    msg = str(excinfo.value)
    assert "disk I/O error" in msg, (
        f"write_txn masked the original exception with rollback failure; "
        f"got {msg!r} (expected to contain 'disk I/O error')"
    )
    assert "cannot rollback" not in msg, (
        f"write_txn surfaced the rollback failure instead of the original "
        f"OperationalError; got {msg!r}"
    )


def test_write_txn_check_reads_correct_header_fields(tmp_path):
    """A genuinely truncated DB is never reported as passing the invariant.

    The check no longer opens the database file to read header bytes (that
    open/close would cancel this process's POSIX advisory locks — the
    corruption route in sqlite.org/howtocorrupt.html §2.2). It asks SQLite for
    ``page_count`` instead. On a truncated file SQLite refuses that pragma, so
    the helper reports "not healthy" rather than a page-count mismatch; either
    way the file must never come back clean.
    """
    import struct
    from hermes_cli.kanban_db import connect
    from hermes_cli.sqlite_safe_read import file_length_matches_header

    db = tmp_path / "synthetic.db"
    conn = connect(db_path=db)
    conn.execute("PRAGMA journal_mode=DELETE")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()

    with open(db, "rb") as f:
        data = bytearray(f.read())
    real_page_count = struct.unpack(">I", data[28:32])[0]
    if real_page_count < 2:
        pytest.skip("DB too small for synthetic truncation test")
    truncated = bytes(data[: (real_page_count - 1) * page_size])
    with open(db, "wb") as f:
        f.write(truncated)

    raw_conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        assert file_length_matches_header(raw_conn) is not True
    finally:
        raw_conn.close()


# ---------------------------------------------------------------------------
# reap_worker_zombies() tests
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# connect_closing(): context manager that actually closes the FD
# Regression coverage for #33159 (kanban.db FD leak — gateway crashes after
# ~4 days). sqlite3.Connection's built-in __exit__ commits/rollbacks but
# does NOT close, so `with kb.connect() as conn:` leaks the FD in
# long-lived processes (gateway run_slash, dashboard decompose handler).
# `connect_closing()` is the leak-safe replacement.
# ---------------------------------------------------------------------------




def test_bare_connect_does_not_close_on_context_exit(tmp_path):
    """Document the leak that connect_closing exists to prevent.

    sqlite3.Connection's __exit__ commits/rollbacks but doesn't close.
    This is the upstream behaviour we cannot change; the regression
    guard is to make sure connect_closing() does the right thing.
    """
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        pass
    # Still usable after with-block exit (the leak).
    conn.execute("SELECT 1").fetchone()
    conn.close()  # explicit close to avoid leaking THIS test


# ---------------------------------------------------------------------------
# Product-card clean exits fail closed: only the structured completion/block
# protocol may advance or stop a product workflow. Comments that merely look
# like handoff evidence are not an authority boundary.
# ---------------------------------------------------------------------------


def _make_running_product_card(
    conn, _kb, *, step, assignee="worker-profile", worker_pid=91001,
    max_retries=None,
):
    host = _kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(
        conn,
        title=f"User story: {step}",
        assignee=assignee,
        workflow_template_id="product",
        current_step_key=step,
        max_retries=max_retries,
    )
    conn.execute(
        "UPDATE tasks SET status='running', worker_pid=?, claim_lock=? WHERE id=?",
        (worker_pid, f"{host}:w", tid),
    )
    conn.commit()
    return tid


def _add_handoff_comment(conn, tid, body="Architecture handoff — ready for development. Approved."):
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (tid, "worker-profile", body, int(time.time())),
    )
    conn.commit()


def test_product_worker_clean_exit_ignores_completion_like_prose(
    kanban_home, monkeypatch,
):
    """Completion-looking comments cannot advance a product workflow."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(_kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0))

    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = _make_running_product_card(conn, _kb, step="architecture")
        _add_handoff_comment(conn, tid)

        kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        kinds = [event.kind for event in kb.list_events(conn, tid)]

    assert task.status == "blocked"
    assert task.current_step_key == "architecture"
    assert "workflow_advanced" not in kinds
    assert "adjudicated_advance" not in kinds


def test_product_worker_clean_exit_blocks_without_protocol_completion(
    kanban_home, monkeypatch,
):
    """A product worker that omits the terminal protocol blocks on first miss."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(_kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0))

    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = _make_running_product_card(
            conn, _kb, step="architecture", max_retries=5,
        )
        # deliberately NO handoff comment
        kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)

    assert task.status == "blocked"
    assert task.current_step_key == "architecture"


def test_product_worker_nonzero_exit_retains_retry_semantics(
    kanban_home, monkeypatch,
):
    """A normal (nonzero) crash keeps the existing isolated-retry semantics."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(_kb, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 1))

    kb.create_board("prod", preset="product")
    with kb.connect(board="prod") as conn:
        tid = _make_running_product_card(conn, _kb, step="development", assignee="developer")
        _add_handoff_comment(conn, tid)  # evidence present, but this is NOT a clean exit
        kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)

    assert task.current_step_key == "development"
    assert task.status == "ready"


def test_handoff_v2_flag_defaults_off_and_reads_meta(kanban_home):
    import hermes_cli.kanban_db as kb
    assert kb._handoff_v2_enabled({}) is False
    assert kb._handoff_v2_enabled({"product_workflow": {"handoff_v2": True}}) is True
    assert kb._handoff_v2_enabled({"product_workflow": {"handoff_v2": False}}) is False


# ---------------------------------------------------------------------------
# block_task / unblock_task -- v2 flag maintenance through the REAL worker
# block seam (R2; remediation of Codex-confirmed P1c)
# ---------------------------------------------------------------------------

def test_block_task_v2_board_sets_blocked_flag_via_real_entry(kanban_home, monkeypatch):
    """The REAL worker block path (block_task) -- not a helper -- must set
    blocked=1 and clear running on the final ``blocked`` landing. First call
    (kind="needs_input") routes to the Hermes ``default`` preflight and only
    clears ``running``; the second call (same unresolved preflight) lands in
    ``blocked`` and is where the P1c flag gap lived."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-block-blocked"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None

        first = kb.block_task(
            conn, tid, reason="need API credentials", kind="needs_input", board=board,
        )
        row_after_first = conn.execute(
            "SELECT running, blocked, assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

        second = kb.block_task(
            conn, tid,
            reason="default could not find a substitute credential",
            kind="needs_input",
            board=board,
        )
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert first is True
    assert row_after_first["running"] == 0
    assert row_after_first["blocked"] == 0
    assert row_after_first["assignee"] == "default"

    assert second is True
    assert row["running"] == 0
    assert row["blocked"] == 1
    assert row["status"] == "blocked"
    assert row["status"] == kb._legacy_status(row, meta)


def test_block_task_v2_board_clears_running_flag_on_running_card(kanban_home, monkeypatch):
    """A running (flag=1) v2 card that blocks must not end up in limbo --
    running must clear when blocked is set. Uses kind="transient" (not a
    PRODUCT_HUMAN_BLOCK_KINDS member) to land directly in ``blocked``,
    bypassing the preflight detour so this isolates the blocked-landing seam."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-block-running-clears"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        pre = conn.execute(
            "SELECT running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert pre["running"] == 1

        outcome = kb.block_task(
            conn, tid, reason="rate limited", kind="transient", board=board,
        )
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert outcome is True
    assert row["running"] == 0
    assert row["blocked"] == 1
    assert row["status"] == "blocked"
    assert row["status"] == kb._legacy_status(row, meta)


def test_block_task_v2_board_dependency_lands_todo_without_clobbering_status(kanban_home, monkeypatch):
    """A dependency block must land status='todo' with flags (0, 0) -- and
    critically must NOT call the sync seam, which would clobber the explicit
    'todo' status back to a derived column status."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-block-dependency"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None

        outcome = kb.block_task(
            conn, tid, reason="waiting on parent", kind="dependency", board=board,
        )
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert outcome is True
    assert row["status"] == "todo"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_unblock_task_v2_board_clears_blocked_and_running_flags(kanban_home, monkeypatch):
    """A blocked v2 card, once unblocked through the real entry point, must
    have both flags cleared -- an unblocked card is idle, not running."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-unblock-clears"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        assert kb.block_task(
            conn, tid, reason="rate limited", kind="transient", board=board,
        ) is True
        blocked_row = conn.execute(
            "SELECT blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert blocked_row["blocked"] == 1

        assert kb.unblock_task(conn, tid) is True
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["blocked"] == 0
    assert row["running"] == 0
    assert row["status"] in ("todo", "ready")


def test_block_task_legacy_board_does_not_touch_flags(kanban_home):
    """Legacy (non-v2) boards: block_task/unblock_task must remain
    byte-for-byte unchanged -- neither flag is touched."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task", assignee="alice")
        assert kb.block_task(conn, tid, reason="need input") is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["running"] == 0
        assert row["blocked"] == 0

        assert kb.unblock_task(conn, tid) is True
        row = conn.execute(
            "SELECT running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["running"] == 0
        assert row["blocked"] == 0


# ---------------------------------------------------------------------------
# R3: reclaim/terminal paths clear the v2 running flag; done sets phase=done
#
# R1 made claim_task set running=1. Every path that ends a worker's run --
# reclaim (stale claim, crash, timeout, dead-pid reconcile), spawn failure,
# and terminal completion -- must clear it back to 0, or flags and status
# disagree. These tests drive the REAL dispatcher/reclaim entry points (not
# the _apply_v2_flags helper directly) on a v2 board, then prove the same
# paths are byte-for-byte unchanged on a legacy board.
# ---------------------------------------------------------------------------

def test_release_stale_claims_v2_board_clears_running_flag(kanban_home, monkeypatch):
    """A stale-by-TTL v2 claim whose worker PID is dead is reclaimed to
    ``ready`` with ``running`` cleared -- worker_pid IS NULL implies
    running=0."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-release-stale-clears-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = _kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, tid, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, tid),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda *a, **k: None)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert reclaimed == 1
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_release_stale_claims_legacy_board_flags_stay_zero(kanban_home, monkeypatch):
    """Legacy board: release_stale_claims behavior (reclaim to ready) is
    unchanged; running/blocked stay 0 as they always were."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 3600, t),
        )
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda *a, **k: None)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert reclaimed == 1
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_reclaim_task_v2_board_clears_running_flag(kanban_home, monkeypatch):
    """Operator-driven reclaim_task on a v2 board clears running/blocked
    alongside worker_pid."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reclaim-task-clears-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        pre = conn.execute(
            "SELECT running FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert pre["running"] == 1

        assert kb.reclaim_task(conn, tid, reason="test") is True
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_detect_crashed_workers_v2_board_clears_running_flag(kanban_home, monkeypatch):
    """A v2 card whose worker PID died is reclaimed by detect_crashed_workers
    with running cleared."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    board = "v2-crashed-clears-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = _kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, tid, 90001)
        # Past the launch-window grace period so the crash check isn't
        # skipped as "freshly claimed".
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (int(time.time()) - 3600, tid),
        )

        crashed = kb.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert crashed == [tid]
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_detect_crashed_workers_legacy_board_flags_stay_zero(kanban_home, monkeypatch):
    """Legacy board: detect_crashed_workers is unchanged; running/blocked
    stay 0 as they always were (isolated-failure retry path)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="iso", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=? "
            "WHERE id=?",
            (80000, f"{host}:w0", tid),
        )
        conn.commit()

        crashed = kb.detect_crashed_workers(conn)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert crashed == [tid]
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_detect_stale_running_v2_board_clears_running_flag(kanban_home, monkeypatch):
    """A v2 card with a stale heartbeat is reclaimed by detect_stale_running
    with running cleared."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-detect-stale-clears-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = _kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, tid, os.getpid())

        five_hours_ago = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ? WHERE id = ?", (five_hours_ago, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (five_hours_ago, tid),
            )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        stale = kb.detect_stale_running(
            conn, stale_timeout_seconds=14400, signal_fn=lambda *a, **k: None,
        )
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert tid in stale
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_enforce_max_runtime_v2_board_clears_running_flag(kanban_home, monkeypatch):
    """A v2 card past its max_runtime_seconds is timed out with running
    cleared."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-enforce-max-runtime-clears-running"
    _v2_product_board(board)
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="development",
            max_runtime_seconds=10,
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, tid, 12345)
        old_started = int(time.time()) - 20
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (old_started, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old_started, tid),
        )

        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert timed_out == [tid]
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_reconcile_v2_dead_worker_clears_running_flag(kanban_home, monkeypatch):
    """reconcile's dead-worker reclaim (step 1) clears running on a v2
    board's card whose worker PID died."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-clears-running"
    _v2_product_board(board)
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, tid, 99999)
        pre = conn.execute(
            "SELECT running FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert pre["running"] == 1
        stale_started_at = int(time.time()) - 3600
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (stale_started_at, tid),
        )

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        result = kb.reconcile(conn, board=board)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert result.reclaimed == [tid]
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_dispatch_once_v2_board_spawn_failure_clears_running_flag(
    kanban_home, tmp_path, monkeypatch,
):
    """THE key spawn-failure integration test: drive the REAL dispatch_once
    -> claim_task -> failing spawn_fn -> _record_task_failure path on a v2
    board. Closes the R1-flagged gap: claim_task sets running=1 at claim
    time, and the failed spawn must clear it back to 0 when the card reverts
    to ready (below the failure-count threshold)."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    board = "v2-dispatch-spawn-failure-clears-running"
    _v2_product_board(board)
    meta = kb.read_board_metadata(board)

    def boom(task, workspace, board=None):
        raise RuntimeError("spawn failed")

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        result = kb.dispatch_once(conn, spawn_fn=boom, board=board, failure_limit=5)

        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert result.auto_blocked == []
    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_dispatch_spawn_failure_legacy_board_flags_stay_zero(
    kanban_home, all_assignees_spawnable,
):
    """Legacy board: dispatch_once spawn-failure behavior is unchanged;
    running/blocked stay 0 as they always were."""
    def boom(task, workspace):
        raise RuntimeError("spawn failed")

    with kb.connect() as conn:
        t = kb.create_task(conn, title="boom", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=boom)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_record_task_failure_v2_board_breaker_trip_sets_blocked_clears_running(
    kanban_home, tmp_path, monkeypatch,
):
    """When the spawn-failure circuit breaker trips (failure_limit reached),
    the v2 card lands in ``blocked`` with running=0, blocked=1."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-breaker-trip-sets-blocked"
    _v2_product_board(board)
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None

        tripped = kb._record_task_failure(
            conn, tid, "boom",
            outcome="spawn_failed",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        )
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert tripped is True
    assert row["status"] == "blocked"
    assert row["running"] == 0
    assert row["blocked"] == 1
    assert row["status"] == kb._legacy_status(row, meta)


def test_complete_task_v2_terminal_done_sets_phase_and_clears_flags(kanban_home):
    """Terminal ``done`` on a v2 board must set current_step_key='done' and
    clear both running and blocked, so status/phase/flags all agree with
    ``_legacy_status``."""
    board = "v2-complete-terminal-clears-flags"
    _v2_product_board(board)
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="done",
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,),
        )
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        pre = conn.execute(
            "SELECT running FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert pre["running"] == 1

        result = kb.complete_task(
            conn, tid, summary="Released and measured", board=board,
        )
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["status"] == "done"
    assert row["current_step_key"] == "done"
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] == kb._legacy_status(row, meta)


def test_complete_task_release_measure_cannot_bypass_release_orchestration(
    kanban_home, monkeypatch,
):
    board = "v2-release-evidence-gate"
    kb.ensure_product_board_defaults(board)
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: evidence gate",
            board=board,
            workflow_template_id="product",
            current_step_key="release_measure",
        )

        original_validate = kb._validate_done_evidence

        def validate_in_terminal_transaction(inner_conn, inner_task_id, evidence):
            assert inner_conn.in_transaction is True
            return original_validate(inner_conn, inner_task_id, evidence)

        monkeypatch.setattr(
            kb, "_validate_done_evidence", validate_in_terminal_transaction
        )
        with pytest.raises(kb.ReleaseEvidenceError) as exc_info:
            kb.complete_task(conn, task_id, summary="looks done", board=board)

        assert "integrated_branch" in exc_info.value.missing
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.current_step_key == "release_measure"


def test_complete_task_legacy_board_terminal_flags_stay_zero(kanban_home):
    """Legacy board: complete_task's terminal transition is unchanged;
    running/blocked stay 0 and current_step_key is untouched (it isn't a v2
    phase field there)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Legacy task", assignee="alice",
            current_step_key="in_progress",
        )
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        assert kb.complete_task(conn, tid, summary="done", board=None) is True
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert row["status"] == "done"
    assert row["running"] == 0
    assert row["blocked"] == 0
    # current_step_key is a generic field unrelated to v2 phase on a legacy
    # board -- complete_task must not repurpose it to 'done'.
    assert row["current_step_key"] == "in_progress"


# ---------------------------------------------------------------------------
# CR2: direct/manual status writers maintain the v2 flags (state drift, P2)
#
# Dashboard drag-drop (_set_status_direct), schedule_task, and archive_task
# all write ``status`` directly instead of going through claim_task/
# complete_task/block_task's flag-maintaining seams. On a handoff_v2 board
# that left running=1 (or blocked=1) after a manual/schedule/archive
# transition off of ``running``, disagreeing with the freshly-written
# status. _apply_v2_flags_for_status is the mapping helper that fixes this:
# it sets flags to MATCH the directly-written status (no re-derivation),
# and is v2-gated so legacy boards are untouched.
# ---------------------------------------------------------------------------

def test_apply_v2_flags_for_status_running_sets_running_clears_blocked(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-flags-for-status-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute(
            "UPDATE tasks SET running = 0, blocked = 1 WHERE id = ?", (tid,),
        )
        with kb.write_txn(conn):
            kb._apply_v2_flags_for_status(conn, tid, "running", board=board)
        row = conn.execute(
            "SELECT running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["running"] == 1
    assert row["blocked"] == 0


def test_apply_v2_flags_for_status_blocked_sets_blocked_clears_running(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-flags-for-status-blocked"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute(
            "UPDATE tasks SET running = 1, blocked = 0 WHERE id = ?", (tid,),
        )
        with kb.write_txn(conn):
            kb._apply_v2_flags_for_status(conn, tid, "blocked", board=board)
        row = conn.execute(
            "SELECT running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["running"] == 0
    assert row["blocked"] == 1


@pytest.mark.parametrize(
    "new_status", ["ready", "todo", "review", "scheduled", "archived", "done", "triage"],
)
def test_apply_v2_flags_for_status_other_statuses_clear_both_flags(
    kanban_home, monkeypatch, new_status,
):
    """Any status other than running/blocked clears both flags -- these
    statuses are not flag-derivable, so the helper does not try to
    re-derive them; it only zeroes the running/blocked pair."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = f"v2-flags-for-status-other-{new_status}"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute(
            "UPDATE tasks SET running = 1, blocked = 1 WHERE id = ?", (tid,),
        )
        with kb.write_txn(conn):
            kb._apply_v2_flags_for_status(conn, tid, new_status, board=board)
        row = conn.execute(
            "SELECT running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["running"] == 0
    assert row["blocked"] == 0


def test_apply_v2_flags_for_status_legacy_board_is_noop(kanban_home):
    """meta=None (legacy board) -- flags must be untouched."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        conn.execute(
            "UPDATE tasks SET running = 1, blocked = 0 WHERE id = ?", (tid,),
        )
        before = dict(conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone())
        with kb.write_txn(conn):
            kb._apply_v2_flags_for_status(conn, tid, "ready")
        after = dict(conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert after == before


def test_apply_v2_flags_for_status_noop_when_not_handoff_v2_enabled(kanban_home, monkeypatch):
    """A product-preset board that hasn't opted into handoff_v2 also no-ops."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-no-v2-flags-for-status"
    kb.create_board(board, name="Product No V2", preset="product")
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="Story", workflow_template_id="product", current_step_key="development",
        )
        conn.execute(
            "UPDATE tasks SET running = 1, blocked = 0 WHERE id = ?", (tid,),
        )
        before = dict(conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone())
        with kb.write_txn(conn):
            kb._apply_v2_flags_for_status(conn, tid, "ready", board=board)
        after = dict(conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert after == before


@pytest.mark.parametrize("new_status", ["ready", "todo", "review"])
def test_set_status_direct_v2_board_off_running_clears_running_flag(
    kanban_home, monkeypatch, new_status,
):
    """Dashboard drag-drop running->{ready,todo,review} on a v2 card must
    clear the running flag so status and flags agree."""
    from plugins.kanban.dashboard.plugin_api import _set_status_direct

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = f"v2-set-status-direct-off-running-{new_status}"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        pre = conn.execute("SELECT running FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert pre["running"] == 1

        assert _set_status_direct(conn, tid, new_status) is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    # todo/review aren't flag-derivable (only running/blocked/ready are), so
    # the invariant here is direct: status stands as written, flags cleared.
    assert row["status"] == new_status
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_set_status_direct_v2_board_running_to_blocked_sets_blocked_flag(kanban_home, monkeypatch):
    """Dashboard drag-drop running->blocked on a v2 card must set blocked=1,
    running=0."""
    from plugins.kanban.dashboard.plugin_api import _set_status_direct

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-set-status-direct-running-to-blocked"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None

        assert _set_status_direct(conn, tid, "blocked") is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["status"] == "blocked"
    assert row["running"] == 0
    assert row["blocked"] == 1
    assert row["status"] == kb._legacy_status(row, meta)


def test_set_status_direct_legacy_board_flags_stay_zero(kanban_home):
    """Legacy board: _set_status_direct behavior is unchanged; flags stay 0."""
    from plugins.kanban.dashboard.plugin_api import _set_status_direct

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid)
        assert _set_status_direct(conn, tid, "ready") is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["status"] == "ready"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_schedule_task_v2_board_running_card_clears_flags(kanban_home, monkeypatch):
    """schedule_task on a running v2 card must clear running/blocked --
    'scheduled' is not flag-derivable, so status stands and flags follow."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-schedule-clears-flags"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        pre = conn.execute("SELECT running FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert pre["running"] == 1

        assert kb.schedule_task(conn, tid, reason="parked") is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["status"] == "scheduled"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_schedule_task_legacy_board_flags_unchanged(kanban_home):
    """Legacy board: schedule_task behavior/flags unchanged (stay 0)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert row["status"] == "scheduled"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_archive_task_v2_board_running_card_clears_flags(kanban_home, monkeypatch):
    """archive_task on a running v2 card must clear running/blocked --
    'archived' is not flag-derivable, so status stands and flags follow."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-archive-clears-flags"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kb.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None
        pre = conn.execute("SELECT running FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert pre["running"] == 1

        assert kb.archive_task(conn, tid) is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["status"] == "archived"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_archive_task_legacy_board_flags_unchanged(kanban_home):
    """Legacy board: archive_task behavior/flags unchanged (stay 0)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        assert kb.archive_task(conn, tid) is True
        row = conn.execute(
            "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert row["status"] == "archived"
    assert row["running"] == 0
    assert row["blocked"] == 0


# ---------------------------------------------------------------------------
# epic_ready -- all stories done + suite green gate (T4.2)
# ---------------------------------------------------------------------------

def _make_epic_with_children(board: str, *, n_children: int = 2) -> tuple[str, list[str]]:
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        children = []
        for i in range(n_children):
            child = kb.create_task(conn, title=f"Story {i}", board=board)
            kb.add_epic_membership(conn, epic_id=epic, task_id=child)
            children.append(child)
    return epic, children


def test_epic_ready_not_all_children_done_returns_false_verify_not_called(kanban_home):
    board = "v2-epic-ready-not-all-done"
    _v2_product_board(board)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        _set_task_status(conn, children[0], "done")
        # children[1] stays in its default (not-done) status.
        verify = unittest.mock.Mock(return_value=True)
        result = kb.epic_ready(conn, epic, board=board, verify_fn=verify)

    assert result is False
    verify.assert_not_called()


def test_epic_ready_all_done_verify_true_returns_true(kanban_home):
    board = "v2-epic-ready-all-done-true"
    _v2_product_board(board)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")
        seen_branches: list[str] = []

        def verify(eb: str) -> bool:
            seen_branches.append(eb)
            return True

        result = kb.epic_ready(conn, epic, board=board, verify_fn=verify)

    assert result is True
    assert seen_branches == [kb.epic_branch_for(epic)]


def test_epic_ready_all_done_verify_false_returns_false(kanban_home):
    board = "v2-epic-ready-all-done-false"
    _v2_product_board(board)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")
        result = kb.epic_ready(conn, epic, board=board, verify_fn=lambda eb: False)

    assert result is False


def test_epic_ready_no_children_returns_false_verify_not_called(kanban_home):
    board = "v2-epic-ready-no-children"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        epic = kb.create_task(
            conn, title="Lonely Epic", board=board, work_item_kind="epic"
        )
        verify = unittest.mock.Mock(return_value=True)
        result = kb.epic_ready(conn, epic, board=board, verify_fn=verify)

    assert result is False
    verify.assert_not_called()


def test_epic_ready_non_v2_board_returns_false_verify_not_called(kanban_home):
    board = "legacy-epic-ready"
    kb.create_board(board, name="Legacy Board", preset="product")
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        _set_task_status(conn, epic, "done")
        verify = unittest.mock.Mock(return_value=True)
        result = kb.epic_ready(conn, epic, board=board, verify_fn=verify)

    assert result is False
    verify.assert_not_called()


# ---------------------------------------------------------------------------
# merge_epic_to_main -- Hermes-run LOCAL merge of an epic into main (T4.3)
#
# THE HARD BOUNDARY: this function must never `git push` / touch origin.
# Every test below records the git subcommands actually executed (real
# subprocess, real temp git repos) and asserts none of them is "push".
# ---------------------------------------------------------------------------

def _v2_product_board_with_repo(name: str, repo: Path) -> None:
    """Like ``_v2_product_board`` but also anchors the board on a real repo
    via ``default_workdir``, which ``merge_epic_to_main`` resolves its
    ``repo_root`` from."""
    kb.create_board(name, name="V2 Board", preset="product", default_workdir=str(repo))
    meta_path = kb.board_metadata_path(name)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["handoff_v2"] = True
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _configure_candidate_verification(
    board: str, repo: Path, *, command: tuple[str, ...] = ("python", "-c", "print('ok')")
) -> kb.RepositoryContract:
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["repository"] = {
        "base_ref": "refs/heads/main",
        "target_branch": "main",
        "verification_profiles": {
            name: {
                "commands": [
                    {"argv": list(command), "workdir": ".", "timeout_seconds": 5}
                ]
            }
            for name in ("story_integration", "epic_release")
        },
        "ci_observation": {"provider": "test", "required_workflows": ["CI"]},
        "boundary_evidence": {
            "test_globs": [],
            "fixture_globs": [],
            "generated_paths": [],
        },
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    contract = kb.repository_contract_for_board(board, repo_root=repo)
    assert contract is not None
    return contract


def _make_epic_branch(repo: Path, epic_branch: str, *, from_branch: str = "main") -> str:
    """Branch ``epic_branch`` off ``from_branch`` and add a unique commit.
    Returns the new commit sha. Leaves ``from_branch`` checked out."""
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", epic_branch, from_branch],
        check=True, capture_output=True, text=True,
    )
    sha = _commit_file(repo, "epic_work.txt", "epic work\n", "epic commit")
    subprocess.run(
        ["git", "-C", str(repo), "switch", from_branch],
        check=True, capture_output=True, text=True,
    )
    return sha


def _make_fact_ready_epic(board: str, repo: Path) -> tuple[str, list[str]]:
    """Build one Epic member with current terminal authority and facts."""
    epic, children = _make_epic_with_children(board, n_children=1)
    story_id = children[0]
    story_branch = f"story/{story_id}"
    review_base_sha = _head_sha(repo)
    source_sha = _make_epic_branch(repo, story_branch)
    epic_branch = kb.epic_branch_for(epic)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", epic_branch, story_branch],
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_file(repo, "epic_tip.txt", "epic tip\n", "epic tip")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )

    with kb.connect(board=board) as conn:
        kb.set_branch_name(conn, story_id, story_branch)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workflow_template_id='product', "
                "current_step_key='done', status='done', completed_at=1, "
                "assignee=NULL, running=0, blocked=0, current_run_id=NULL "
                "WHERE id=?",
                (story_id,),
            )
            kb._synthesize_ended_run(
                conn,
                story_id,
                outcome="advanced",
                step_key="test",
                metadata={
                    "test_branch": story_branch,
                    "test_head_sha": source_sha,
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {
                        "writer": {"agent": "developer"},
                        "tester": {"agent": "tester"},
                    },
                },
            )
            review_run_id = kb._synthesize_ended_run(
                conn,
                story_id,
                outcome="advanced",
                step_key="review",
                metadata={
                    "review_branch": story_branch,
                    "review_base_sha": review_base_sha,
                    "review_head_sha": source_sha,
                    "workflow_outcome": {"verdict": "approved"},
                    "ai_provenance": {
                        "writer": {"agent": "developer"},
                        "reviewer": {"agent": "reviewer"},
                    },
                },
            )
            conn.execute(
                "INSERT INTO story_integration_intents ("
                "epic_id, story_id, source_sha, source_branch, review_run_id, "
                "review_base_sha, status, candidate_sha, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'integrated', ?, 90, 90)",
                (
                    epic,
                    story_id,
                    source_sha,
                    story_branch,
                    review_run_id,
                    review_base_sha,
                    source_sha,
                ),
            )
            conn.execute(
                "INSERT INTO epic_story_integrations "
                "(epic_id, story_id, source_sha, candidate_sha, integrated_at) "
                "VALUES (?, ?, ?, ?, 90)",
                (epic, story_id, source_sha, source_sha),
            )
    return epic, children


def _record_git_calls(monkeypatch) -> list[list[str]]:
    """Monkeypatch ``subprocess.run`` to record every argv while still
    executing real git. Returns the list calls are appended to."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    return calls


def _assert_no_push(calls: list[list[str]]) -> None:
    assert calls, "expected merge_epic_to_main to run at least one git subcommand"
    for cmd in calls:
        assert "push" not in cmd, f"git push invoked: {cmd}"


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_build_merge_candidate_keeps_checked_out_target_unchanged(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    source_sha = _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")

    candidate = kb._build_verified_merge_candidate(
        repo,
        "main",
        "wt/source",
        "test candidate",
        lambda path: (path / "epic_work.txt").read_text() == "epic work\n",
    )

    assert candidate.pre_sha == pre_sha
    assert candidate.target_worktree == repo.resolve()
    assert candidate.candidate_sha != pre_sha
    assert subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", source_sha, "main"]
    ).returncode == 1
    assert kb._fast_forward_target(candidate) is True
    assert subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", source_sha, "main"]
    ).returncode == 0


def test_build_merge_candidate_rejects_dirty_checked_out_target(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(kb.IntegrationCandidateError, match="target worktree is dirty"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", lambda _path: True
        )

    assert _git_output(repo, "rev-parse", "main") == pre_sha
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "dirty"


def test_build_merge_candidate_updates_unchecked_target_ref(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    source_sha = _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "operator"],
        check=True,
        capture_output=True,
        text=True,
    )
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    assert candidate.target_worktree is None
    assert kb._fast_forward_target(candidate) is True
    assert _git_output(repo, "branch", "--show-current") == "operator"
    assert subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", source_sha, "main"]
    ).returncode == 0


def test_build_merge_candidate_conflict_preserves_target(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_file(repo, "shared.txt", "source\n", "source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_file(repo, "shared.txt", "main\n", "main")
    pre_sha = _git_output(repo, "rev-parse", "main")
    with pytest.raises(kb.IntegrationCandidateError, match="merge conflict"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", lambda _path: True
        )
    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_verification_failure_preserves_target(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")
    with pytest.raises(kb.IntegrationCandidateError, match="verification failed"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", lambda _path: False
        )
    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_uses_configured_verification_profile(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    verifier = repo / "verify.py"
    verifier.write_text(
        "#!/usr/bin/env python3\nprint('configured-verifier')\n",
        encoding="utf-8",
    )
    verifier.chmod(0o755)
    source_sha = _commit_file(repo, "verify.py", verifier.read_text(), "verifier")
    profile = kb.VerificationProfile(
        (
            VerificationCommand(
                argv=("verify.py",),
                workdir=PurePosixPath("."),
                timeout_seconds=5,
            ),
        )
    )
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )

    candidate = kb._build_verified_merge_candidate(
        repo,
        "main",
        "wt/source",
        "configured candidate",
        verification_profile=profile,
        verification_contract_digest="contract-digest",
        verification_scope="story_integration",
        verification_subject_id="story-1",
        expected_source_sha=source_sha,
    )

    assert candidate.verification_result is not None
    assert candidate.verification_result.status == "passed"
    assert candidate.verification_result.profile == "story_integration"
    assert candidate.verification_result.contract_digest == "contract-digest"


def test_build_merge_candidate_missing_configured_profile_is_not_legacy_fallback(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    source_sha = _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")

    with pytest.raises(kb.IntegrationCandidateError) as exc_info:
        kb._build_verified_merge_candidate(
            repo,
            "main",
            "wt/source",
            "configured candidate",
            verification_profile=None,
            verification_contract_digest="contract-digest",
            verification_scope="epic_release",
            verification_subject_id="epic-1",
            expected_source_sha=source_sha,
        )

    assert exc_info.value.verification_result is not None
    assert exc_info.value.verification_result.status == "configuration_error"
    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_merge_epic_preserves_explicit_injected_candidate_verification(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-injected-verification"
    _v2_product_board_with_repo(board, repo)
    _configure_candidate_verification(
        board, repo, command=("python", "-c", "raise SystemExit(9)")
    )
    epic, children = _make_fact_ready_epic(board, repo)
    injected = unittest.mock.Mock(return_value=True)

    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, candidate_verify_fn=injected
        )

    assert result == "merged"
    injected.assert_called_once()


def _verification_run_fixture(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-verification-reuse"
    _v2_product_board_with_repo(board, repo)
    count_file = tmp_path / "verification-count.txt"
    command = (
        sys.executable,
        "-c",
        f"from pathlib import Path; p=Path({str(count_file)!r}); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')",
    )
    contract = _configure_candidate_verification(board, repo, command=command)
    candidate_sha = _git_output(repo, "rev-parse", "HEAD")
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="Story", board=board)
    return repo, board, task_id, contract, candidate_sha, count_file


def _run_reusable_verification(conn, fixture):
    repo, _board, task_id, contract, candidate_sha, _count_file = fixture
    return kb._run_or_reuse_configured_verification(
        conn,
        task_id=task_id,
        candidate_path=repo,
        source_sha=candidate_sha,
        candidate_sha=candidate_sha,
        contract=contract,
        profile_name="story_integration",
        gate_kind="story_integration",
    )


def test_parser_addition_preserves_exact_repository_verification_receipt_reuse(
    kanban_home, tmp_path, monkeypatch
):
    fixture = _verification_run_fixture(kanban_home, tmp_path, monkeypatch)
    _repo, board, task_id, _contract, _candidate_sha, count_file = fixture

    with kb.connect(board=board) as conn:
        first = _run_reusable_verification(conn, fixture)
        second = _run_reusable_verification(conn, fixture)
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='repository_verification'",
            (task_id,),
        ).fetchone()[0]

    assert first.status == second.status == "passed"
    assert first.reused is False
    assert second.reused is True
    assert second.steps == first.steps
    assert count_file.read_text(encoding="utf-8") == "x"
    assert event_count == 1


def test_configured_verification_reuses_receipt_after_connection_crash_boundary(
    kanban_home, tmp_path, monkeypatch
):
    fixture = _verification_run_fixture(kanban_home, tmp_path, monkeypatch)
    _repo, board, _task_id, _contract, _candidate_sha, count_file = fixture

    with kb.connect(board=board) as conn:
        first = _run_reusable_verification(conn, fixture)
    with kb.connect(board=board) as conn:
        recovered = _run_reusable_verification(conn, fixture)

    assert first.reused is False
    assert recovered.reused is True
    assert count_file.read_text(encoding="utf-8") == "x"


def test_verified_candidate_crash_reuses_persisted_receipt(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-verification-crash-reuse"
    _v2_product_board_with_repo(board, repo)
    count_file = tmp_path / "verification-count.txt"
    _configure_candidate_verification(
        board,
        repo,
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; p=Path({str(count_file)!r}); "
            "p.write_text(p.read_text() + 'x' if p.exists() else 'x')",
        ),
    )
    epic, children = _make_fact_ready_epic(board, repo)
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+00:00")
    apply = unittest.mock.Mock(side_effect=[False, True])
    monkeypatch.setattr(kb, "_fast_forward_target", apply)

    with kb.connect(board=board) as conn:
        first = kb.merge_epic_to_main(conn, epic, board=board)
        second = kb.merge_epic_to_main(conn, epic, board=board)
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='repository_verification'",
            (epic,),
        ).fetchone()[0]

    assert first == "verify_failed"
    assert second == "merged"
    assert apply.call_count == 2
    assert count_file.read_text(encoding="utf-8") == "x"
    assert event_count == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "candidate_sha",
        "contract_digest",
        "command_set_digest",
        "runtime_toolchain_digest",
        "generated_policy_digest",
        "gate_kind",
        "executor_policy",
        "key_digest",
        "result_digest",
        "foreign_scope",
        "foreign_subject",
        "foreign_task",
        "malformed_json",
        "failed_result",
        "missing_receipt",
    ],
)
def test_configured_verification_rejects_key_result_and_foreign_receipts(
    kanban_home, tmp_path, monkeypatch, tamper
):
    fixture = _verification_run_fixture(kanban_home, tmp_path, monkeypatch)
    _repo, board, task_id, _contract, _candidate_sha, count_file = fixture

    with kb.connect(board=board) as conn:
        first = _run_reusable_verification(conn, fixture)
        row = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id=? AND kind='repository_verification'",
            (task_id,),
        ).fetchone()
        payload = json.loads(row["payload"])
        if tamper == "foreign_task":
            foreign = kb.create_task(conn, title="Foreign", board=board)
            conn.execute("UPDATE task_events SET task_id=? WHERE id=?", (foreign, row["id"]))
        elif tamper == "malformed_json":
            conn.execute("UPDATE task_events SET payload='{' WHERE id=?", (row["id"],))
        elif tamper == "failed_result":
            payload["status"] = "failed"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "missing_receipt":
            payload.pop("receipt")
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "foreign_scope":
            payload["scope"] = "epic_release"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "foreign_subject":
            payload["subject_id"] = "foreign-story"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "result_digest":
            payload["receipt"]["result_digest"] = "0" * 64
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        else:
            key_name = "digest" if tamper == "key_digest" else tamper
            payload["receipt"]["key"][key_name] = "0" * 64
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        conn.commit()

        rerun = _run_reusable_verification(conn, fixture)

    assert first.reused is False
    assert rerun.reused is False
    assert count_file.read_text(encoding="utf-8") == "xx"


def test_merge_epic_records_configured_profile_failure_as_attention_required(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-config-attention"
    _v2_product_board_with_repo(board, repo)
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["repository"] = {
        "base_ref": "refs/heads/main",
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": {
                "commands": [
                    {
                        "argv": ["python", "-m", "unittest"],
                        "workdir": ".",
                        "timeout_seconds": 5,
                    }
                ]
            }
        },
        "ci_observation": {
            "provider": "test",
            "required_workflows": ["CI"],
        },
        "boundary_evidence": {
            "test_globs": [],
            "fixture_globs": [],
            "generated_paths": ["README.md"],
        },
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    epic, children = _make_fact_ready_epic(board, repo)

    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(conn, epic, board=board)
        task = kb.get_task(conn, epic)
        verification_events = [
            event
            for event in kb.list_events(conn, epic)
            if event.kind == "repository_verification"
        ]

    assert result == "attention_required"
    assert task is not None and task.rework_count == 0
    assert verification_events
    payload = verification_events[-1].payload
    assert isinstance(payload, dict)
    assert payload["status"] == "configuration_error"
    assert payload["rework_eligible"] is False


def test_fast_forward_rejects_target_that_moved_after_candidate(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    _commit_file(repo, "operator.txt", "new\n", "operator moved main")
    moved_sha = _git_output(repo, "rev-parse", "main")
    assert kb._fast_forward_target(candidate) is False
    assert _git_output(repo, "rev-parse", "main") == moved_sha


def test_fast_forward_rejects_checked_out_target_race_to_candidate_descendant(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    integration_git = kb._integration_git
    raced: dict[str, str] = {}

    def advance_target_before_merge(cwd, args, *, timeout=120):
        if args == ["merge", "--ff-only", candidate.candidate_sha] and not raced:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )
            raced["sha"] = _commit_file(
                repo,
                "operator-after-candidate.txt",
                "unverified\n",
                "operator advanced past candidate",
            )
        return integration_git(cwd, args, timeout=timeout)

    monkeypatch.setattr(kb, "_integration_git", advance_target_before_merge)

    assert kb._fast_forward_target(candidate) is False
    assert _git_output(repo, "rev-parse", "main") == raced["sha"]
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", candidate.candidate_ref],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_fast_forward_rejects_target_checked_out_after_candidate(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "operator"],
        check=True,
        capture_output=True,
        text=True,
    )
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    assert candidate.target_worktree is None

    late_checkout = tmp_path / "late-main"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(late_checkout), "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    pre_sha = _git_output(repo, "rev-parse", "main")

    assert kb._fast_forward_target(candidate) is False
    assert _git_output(repo, "rev-parse", "main") == pre_sha
    assert _git_output(late_checkout, "status", "--porcelain") == ""


def test_build_merge_candidate_rejects_reviewed_source_ref_drift(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    approved_sha = _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_file(repo, "after-review.txt", "drift\n", "post-review drift")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    pre_sha = _git_output(repo, "rev-parse", "main")

    with pytest.raises(kb.IntegrationCandidateError, match="source branch moved"):
        kb._build_verified_merge_candidate(
            repo,
            "main",
            "wt/source",
            "test candidate",
            lambda _path: True,
            expected_source_sha=approved_sha,
        )

    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_rejects_source_drift_during_verification(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    approved_sha = _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "post-review", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    drift_sha = _commit_file(repo, "after-review.txt", "drift\n", "post-review drift")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    pre_sha = _git_output(repo, "rev-parse", "main")

    def move_source_during_verify(_candidate: Path) -> bool:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "update-ref",
                "refs/heads/wt/source",
                drift_sha,
                approved_sha,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return True

    with pytest.raises(kb.IntegrationCandidateError, match="source branch moved"):
        kb._build_verified_merge_candidate(
            repo,
            "main",
            "wt/source",
            "test candidate",
            move_source_during_verify,
            expected_source_sha=approved_sha,
        )

    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_preserves_dirty_scratch_on_cleanup_failure(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")
    scratch: Path | None = None

    def dirty_verify(path: Path) -> bool:
        nonlocal scratch
        scratch = path
        (path / "verification-output.txt").write_text("keep", encoding="utf-8")
        return True

    with pytest.raises(kb.IntegrationCandidateError, match="scratch worktree is dirty"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", dirty_verify
        )
    assert _git_output(repo, "rev-parse", "main") == pre_sha
    assert scratch is not None and (scratch / "verification-output.txt").exists()


def test_merge_epic_to_main_happy_path_merges_and_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-happy"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    epic_sha = _make_epic_branch(repo, epic_branch)

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "merged"
    notify.assert_not_called()
    _assert_no_push(calls)

    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", epic_sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 0, "main must contain the epic's commit"

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
    )
    assert status.stdout.strip() == "", "working tree must be clean after merge"


def test_merge_epic_to_main_refuses_unignored_sibling_worktree(
    kanban_home, tmp_path, monkeypatch
):
    """All target dirt, including an unignored worktree, fails closed."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-untracked-worktree"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    epic_sha = _make_epic_branch(repo, epic_branch)

    # Real linked worktree at <repo>/.worktrees/story, exactly as v2 story
    # dispatch creates it -- untracked from main's point of view.
    worktree_branch = "wt/story-1"
    worktree_path = repo / ".worktrees" / "story-1"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", worktree_branch, str(worktree_path), "main"],
        check=True, capture_output=True, text=True,
    )

    # Sanity-check the repro premise before exercising the fix.
    dirty_status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
    )
    assert "?? .worktrees/" in dirty_status.stdout, "expected the sibling worktree to be untracked on main"
    clean_status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True,
    )
    assert clean_status.stdout.strip() == "", "tracked-only status must be clean"

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "verify_failed"
    notify.assert_called_once()
    _assert_no_push(calls)
    assert not any("reset" in cmd for cmd in calls)

    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", epic_sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 1, "dirty main must not contain the epic's commit"


def test_merge_epic_to_main_conflict_aborts_blocks_and_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-conflict"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    # Branch off main, then have BOTH main and the epic branch modify the
    # same line differently so the merge conflicts.
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", epic_branch], check=True,
        capture_output=True, text=True,
    )
    _commit_file(repo, "shared.txt", "epic version\n", "epic edits shared")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"], check=True, capture_output=True, text=True,
    )
    _commit_file(repo, "shared.txt", "main version\n", "main edits shared")
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "conflict"
    _assert_no_push(calls)
    notify.assert_called_once()

    post_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert post_sha == pre_sha, "a failed merge must never leave main mutated"

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
    )
    assert status.stdout.strip() == "", "merge --abort must leave a clean tree"

    with kb.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1


def test_merge_epic_to_main_post_merge_verify_fails_resets_and_blocks(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-verify-fail"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    _make_epic_branch(repo, epic_branch)
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    # Branch-aware: True for the epic branch (so epic_ready's own verify
    # passes) but False for main (so the post-merge check fails).
    def verify(branch: str) -> bool:
        return branch != "main"

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=verify, notify_fn=notify,
        )

    assert result == "verify_failed"
    _assert_no_push(calls)
    notify.assert_called_once()

    post_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert post_sha == pre_sha, "reset --hard must undo the merge"

    with kb.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1


def test_merge_epic_to_main_not_ready_does_not_touch_git(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-not-ready"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kb.connect(board=board) as conn:
        _set_task_status(conn, children[0], "done")
        # children[1] stays not-done.

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "not_ready"
    assert calls == []
    notify.assert_not_called()


def test_merge_epic_to_main_non_v2_board_returns_none(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "legacy-merge-board"
    kb.create_board(board, name="Legacy Board", default_workdir=str(repo))
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result is None
    assert calls == []
    notify.assert_not_called()


# ---------------------------------------------------------------------------
# deploy_epic / notify_operations -- test->preprod Ops API deploy, smoke
# gated, one #operations release notice (Phase 5, T5.1-T5.3).
#
# THE HARD BOUNDARY: test + pre-prod ONLY -- never production, never
# `git push` / any remote-or-origin verb. Every test records the git
# subcommands actually executed (real subprocess) and asserts none of them
# is "push" or touches a remote/origin.
# ---------------------------------------------------------------------------

class _RecordingOpsClient:
    """Ops client test double -- records call order, lets each env's
    build/smoke outcome be scripted independently."""

    def __init__(self, *, build_fail: set | None = None, smoke_fail: set | None = None):
        self.calls: list[tuple[str, str]] = []
        self.build_fail = build_fail or set()
        self.smoke_fail = smoke_fail or set()

    def build_roll(self, env: str):
        self.calls.append(("build_roll", env))
        if env in self.build_fail:
            raise RuntimeError(f"build failed for {env}")
        return {"env": env, "built": True}

    def smoke(self, env: str) -> bool:
        self.calls.append(("smoke", env))
        return env not in self.smoke_fail


def _seed_epic_merged_event(board: str, epic: str, repo: Path) -> str:
    """Seed an ``epic_merged`` event ({epic_branch, pre_sha}) so
    notify_operations' commit-range resolution has something to work with,
    and advance ``main`` one commit past ``pre_sha`` so the range is
    non-trivial."""
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    _commit_file(repo, "deploy_work.txt", "deploy work\n", "post-merge commit")
    with kb.connect(board=board) as conn:
        with kb.write_txn(conn):
            kb._append_event(
                conn, epic, "epic_merged",
                {"epic_branch": kb.epic_branch_for(epic), "pre_sha": pre_sha},
            )
    return pre_sha


def _make_deploy_epic(tmp_path: Path, board_name: str) -> tuple[str, Path, list[str]]:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board_name, repo)
    epic, children = _make_epic_with_children(board_name)
    with kb.connect(board=board_name) as conn:
        for child in children:
            _set_task_status(conn, child, "done")
    _seed_epic_merged_event(board_name, epic, repo)
    return epic, repo, children


def test_deploy_epic_happy_path_deploys_test_then_preprod_and_notifies(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-happy"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    calls = _record_git_calls(monkeypatch)
    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert ops.calls == [
        ("build_roll", "test"), ("smoke", "test"),
        ("build_roll", "preprod"), ("smoke", "preprod"),
    ]
    _assert_no_push(calls)
    assert not any(("remote" in cmd or "origin" in cmd) for cmd in calls)

    with kb.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 0

    notify.assert_called_once()
    message = notify.call_args[0][0]
    assert message["failure"] is False
    assert message["epic_title"] == "Epic"
    assert {s["id"] for s in message["stories"]} == set(children)
    assert message["commit_range"] and ".." in message["commit_range"]
    assert [e["env"] for e in message["envs_status"]] == ["test", "preprod"]
    assert all(e["smoke_ok"] for e in message["envs_status"])

    assert result == message


def test_deploy_epic_test_smoke_fails_stops_blocks_and_pages(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-test-smoke-fail"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient(smoke_fail={"test"})
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert ("build_roll", "preprod") not in ops.calls
    assert ("smoke", "preprod") not in ops.calls
    assert ops.calls == [("build_roll", "test"), ("smoke", "test")]

    with kb.connect(board=board) as conn:
        row = conn.execute(
            "SELECT blocked, running FROM tasks WHERE id = ?", (epic,)
        ).fetchone()
    assert row["blocked"] == 1
    assert row["running"] == 0

    notify.assert_called_once()
    message = notify.call_args[0][0]
    assert message["failure"] is True
    assert message["reason"]
    assert result == message


def test_deploy_epic_preprod_build_fails_blocks_and_pages(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-preprod-build-fail"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient(build_fail={"preprod"})
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert ops.calls == [
        ("build_roll", "test"), ("smoke", "test"), ("build_roll", "preprod"),
    ]
    assert ("smoke", "preprod") not in ops.calls

    with kb.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1

    notify.assert_called_once()
    message = notify.call_args[0][0]
    assert message["failure"] is True
    envs_status = {e["env"]: e for e in message["envs_status"]}
    assert envs_status["test"]["smoke_ok"] is True
    assert envs_status["preprod"]["built"] is False
    assert result == message


def test_deploy_epic_message_shape_contains_epic_stories_range_and_status(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-message-shape"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    message = notify.call_args[0][0]
    assert message["epic_id"] == epic
    assert message["epic_title"] == "Epic"
    story_ids = {s["id"] for s in message["stories"]}
    story_titles = {s["title"] for s in message["stories"]}
    assert story_ids == set(children)
    assert story_titles == {"Story 0", "Story 1"}
    assert message["commit_range"]
    assert len(message["envs_status"]) == 2
    assert message["reason"] is None


def test_notify_operations_failure_message_includes_reason(kanban_home, tmp_path, monkeypatch):
    board = "v2-notify-failure-reason"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    notify = unittest.mock.Mock()
    envs_status = [{"env": "test", "built": True, "smoke_ok": False, "detail": "smoke check failed"}]
    with kb.connect(board=board) as conn:
        message = kb.notify_operations(
            conn, epic, board=board, envs_status=envs_status,
            failure=True, reason="deploy: test smoke failed", notify_fn=notify,
        )

    assert message["failure"] is True
    assert message["reason"] == "deploy: test smoke failed"
    notify.assert_called_once_with(message)


def test_deploy_epic_rejects_production_env_deploys_nothing(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-boundary-prod"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        with pytest.raises(ValueError):
            kb.deploy_epic(
                conn, epic, board=board, envs=("test", "prod"),
                ops_client=ops, notify_fn=notify,
            )

    assert ops.calls == []
    notify.assert_not_called()
    with kb.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 0


def test_deploy_epic_never_touches_git_push_or_remote(kanban_home, tmp_path, monkeypatch):
    """Boundary proof (T5.3): record every subprocess call across a full
    happy-path deploy and assert the deploy path never runs `git push` and
    never invokes a remote/origin verb -- only local `rev-parse` for the
    commit range."""
    board = "v2-deploy-boundary-no-push"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert calls, "expected deploy_epic to run at least one local git subcommand"
    for cmd in calls:
        assert "push" not in cmd, f"git push invoked during deploy: {cmd}"
        assert "remote" not in cmd, f"git remote invoked during deploy: {cmd}"
        assert "origin" not in cmd, f"origin referenced during deploy: {cmd}"


def test_deploy_epic_non_v2_board_returns_none(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "legacy-deploy-board"
    kb.create_board(board, name="Legacy Board", default_workdir=str(repo))
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert result is None
    assert ops.calls == []
    notify.assert_not_called()


def test_deploy_epic_default_ops_client_raises_not_implemented(kanban_home, tmp_path, monkeypatch):
    """No ``ops_client`` injected -> the module stub is used, and it raises
    rather than silently deploying anything (real adapter deferred to
    feat/container-ops-api / PR #3)."""
    board = "v2-deploy-default-client"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, notify_fn=notify)

    # build_roll("test") raises NotImplementedError inside the loop, which
    # deploy_epic treats like any other build failure: block + page.
    assert result is not None
    assert result["failure"] is True
    with kb.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1
    notify.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 6 T6.1: migrate_cards_to_v2_flags -- reconcile existing cards' flags
# to their legacy status when a board flips to handoff_v2.
#
# A board's existing cards have real statuses (running/blocked/ready/...) but
# the running/blocked flag columns default to 0, so an already-running card
# would read status='running', running=0 -- a direct disagreement with
# _legacy_status. migrate_cards_to_v2_flags is the inverse of _legacy_status:
# it sets every card's flags to MATCH its status, using the same mapping as
# CR2's _apply_v2_flags_for_status (now shared via _v2_flags_for_status).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected_running,expected_blocked",
    [
        ("running", 1, 0),
        ("blocked", 0, 1),
        ("ready", 0, 0),
        ("todo", 0, 0),
        ("review", 0, 0),
        ("scheduled", 0, 0),
        ("archived", 0, 0),
        ("done", 0, 0),
        ("triage", 0, 0),
    ],
)
def test_v2_flags_for_status_mapping(status, expected_running, expected_blocked):
    assert kb._v2_flags_for_status(status) == (expected_running, expected_blocked)


def test_migrate_cards_to_v2_flags_reconciles_mixed_board(kanban_home, monkeypatch):
    """Seed a mixed-status board with flags left at their 0 default (as if
    handoff_v2 had just been flipped on), migrate, and assert every card's
    flags now agree with its status via _legacy_status.

    For the non-flag-derivable statuses (ready/todo/review/done/scheduled/
    archived), ``_legacy_status`` falls through to the column status for the
    card's ``current_step_key`` -- so each such card is seeded with a custom
    column whose status matches, letting ``_legacy_status`` round-trip back
    to the original status once flags are reconciled. running/blocked cards
    round-trip via flag precedence regardless of column.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-mixed"
    kb.create_board(board, name="Migrate Mixed", preset="product")
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["columns"] = [
        {"name": "development", "status": "ready"},
        {"name": "ready", "status": "ready"},
        {"name": "todo", "status": "todo"},
        {"name": "review", "status": "review"},
        {"name": "done", "status": "done"},
        {"name": "scheduled", "status": "scheduled"},
        {"name": "archived", "status": "archived"},
    ]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    meta = kb.read_board_metadata(board)

    statuses = ["running", "blocked", "ready", "todo", "review", "done", "scheduled", "archived"]
    with kb.connect(board=board) as conn:
        ids = []
        for status in statuses:
            step_key = "development" if status in ("running", "blocked") else status
            tid = kb.create_task(
                conn, title=f"card-{status}", workflow_template_id="custom",
                current_step_key=step_key,
            )
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            ids.append(tid)

        count = kb.migrate_cards_to_v2_flags(conn, board=board)

        rows = conn.execute(
            "SELECT id, status, running, blocked, current_step_key FROM tasks "
            "WHERE id IN ({})".format(",".join("?" * len(ids))),
            ids,
        ).fetchall()

    assert count == len(statuses)
    assert len(rows) == len(statuses)
    for row in rows:
        expected_running, expected_blocked = kb._v2_flags_for_status(row["status"])
        assert row["running"] == expected_running
        assert row["blocked"] == expected_blocked
        assert kb._legacy_status(row, meta) == row["status"]


def test_migrate_cards_to_v2_flags_idempotent(kanban_home, monkeypatch):
    """Running the migration a second time leaves flags unchanged and the
    consistency invariant intact."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-idempotent"
    kb.create_board(board, name="Migrate Idempotent", preset="product")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        tid_running = kb.create_task(
            conn, title="running-card", workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid_running,))
        tid_blocked = kb.create_task(
            conn, title="blocked-card", workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid_blocked,))

        kb.migrate_cards_to_v2_flags(conn, board=board)
        first = {
            tid: dict(conn.execute(
                "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
            ).fetchone())
            for tid in (tid_running, tid_blocked)
        }

        kb.migrate_cards_to_v2_flags(conn, board=board)
        second = {
            tid: dict(conn.execute(
                "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
            ).fetchone())
            for tid in (tid_running, tid_blocked)
        }

    assert second == first
    for tid in (tid_running, tid_blocked):
        row = second[tid]
        assert kb._legacy_status(row, meta) == row["status"]


def test_migrate_cards_to_v2_flags_does_not_touch_status_or_phase(kanban_home, monkeypatch):
    """The migration only ever writes running/blocked -- status and
    current_step_key must be byte-for-byte unchanged."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-preserves-status"
    kb.create_board(board, name="Migrate Preserves Status", preset="product")

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="card", workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        before = dict(conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

        kb.migrate_cards_to_v2_flags(conn, board=board)

        after = dict(conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert after == before


# ---------------------------------------------------------------------------
# Phase 6 T6.1 (extended): migrate_cards_to_v2_flags also reconciles PHASE for
# terminal 'done' cards -- a dry run on a copy of a production board found
# real cards with status='done' still parked at a non-done phase (legacy
# completions predating the "done ⟹ phase=done" rule), whose _legacy_status
# read something other than 'done'.
# ---------------------------------------------------------------------------

def test_migrate_cards_to_v2_flags_reconciles_phase_for_done(kanban_home, monkeypatch):
    """A legacy 'done' card stuck at a non-done phase (the real dry-run
    finding) gets its current_step_key advanced to 'done' too, not just its
    flags -- so _legacy_status agrees with the stored status again."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-done-phase"
    kb.create_board(board, name="Migrate Done Phase", preset="product")
    meta = kb.read_board_metadata(board)

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="legacy-done-card", workflow_template_id="product",
            current_step_key="release_measure",
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))

        kb.migrate_cards_to_v2_flags(conn, board=board)

        row = dict(conn.execute(
            "SELECT status, current_step_key, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert row["status"] == "done"
    assert row["current_step_key"] == "done"
    assert (row["running"], row["blocked"]) == (0, 0)
    assert kb._legacy_status(row, meta) == "done"


@pytest.mark.parametrize(
    "status,step_key",
    [
        ("ready", "development"),
        ("running", "development"),
        ("blocked", "development"),
        ("review", "review"),
        ("todo", "development"),
        ("archived", "release_measure"),
    ],
)
def test_migrate_cards_to_v2_flags_leaves_non_done_phase_untouched(
    kanban_home, monkeypatch, status, step_key
):
    """Only status='done' cards get their phase moved -- every other status
    (including archived, which is out-of-band, not a workflow phase) keeps
    its current_step_key exactly as-is."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = f"migrate-cards-phase-untouched-{status}"
    kb.create_board(board, name="Migrate Phase Untouched", preset="product")

    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title=f"card-{status}", workflow_template_id="product",
            current_step_key=step_key,
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))

        kb.migrate_cards_to_v2_flags(conn, board=board)

        row = dict(conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert row["status"] == status
    assert row["current_step_key"] == step_key


def test_migrate_cards_to_v2_flags_phase_for_done_idempotent(kanban_home, monkeypatch):
    """A done card already at phase 'done' is unchanged, and running the
    migration a second time is a no-op for the phase fixup too."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-done-phase-idempotent"
    kb.create_board(board, name="Migrate Done Phase Idempotent", preset="product")

    with kb.connect(board=board) as conn:
        tid_already_done = kb.create_task(
            conn, title="already-done-card", workflow_template_id="product",
            current_step_key="done",
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid_already_done,))
        tid_legacy_done = kb.create_task(
            conn, title="legacy-done-card", workflow_template_id="product",
            current_step_key="release_measure",
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid_legacy_done,))

        kb.migrate_cards_to_v2_flags(conn, board=board)
        first = {
            tid: dict(conn.execute(
                "SELECT status, current_step_key, running, blocked FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone())
            for tid in (tid_already_done, tid_legacy_done)
        }

        kb.migrate_cards_to_v2_flags(conn, board=board)
        second = {
            tid: dict(conn.execute(
                "SELECT status, current_step_key, running, blocked FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone())
            for tid in (tid_already_done, tid_legacy_done)
        }

    assert second == first
    assert second[tid_already_done]["current_step_key"] == "done"
    assert second[tid_legacy_done]["current_step_key"] == "done"

def test_product_board_defaults_helper_enables_handoff_v2_and_gitignore(kanban_home, tmp_path):
    repo = tmp_path / "product-repo"
    _init_git_repo(repo)

    meta = kb.ensure_product_board_defaults(
        "product-defaults",
        name="Product Defaults",
        default_workdir=str(repo),
    )

    assert meta["preset"] == "product"
    assert meta["columns"] == kb.PRODUCT_BOARD_COLUMNS
    assert meta["product_workflow"]["handoff_v2"] is True
    assert meta["product_workflow"]["assignees"] == kb.PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES
    assert ".worktrees/" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_project_bound_product_task_defaults_to_product_backlog_and_worktree(kanban_home, tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "product-repo"
    _init_git_repo(repo)
    kb.ensure_product_board_defaults("prod", name="Product", default_workdir=str(repo))

    home = kanban_home
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(
            pconn,
            name="Product Repo",
            primary_path=str(repo),
            board_slug="prod",
        )

    with kb.connect(board="prod") as conn:
        tid = kb.create_task(conn, title="User story: isolated work", project_id=project_id, board="prod")
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.workflow_template_id == "product"
    assert task.current_step_key == "backlog"
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(repo / ".worktrees" / tid)
    assert task.branch_name.startswith("product-repo/")
    assert any(event.kind == "workflow_defaulted" for event in events)
    assert ".worktrees/" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_generic_board_task_without_metadata_stays_plain(kanban_home):
    kb.create_board("generic", name="Generic")

    with kb.connect(board="generic") as conn:
        tid = kb.create_task(conn, title="plain", board="generic")
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.workflow_template_id is None
    assert task.current_step_key is None
    assert task.workspace_kind == "scratch"
    assert not any(event.kind == "workflow_defaulted" for event in events)


def test_project_bound_task_explicit_non_product_metadata_not_overwritten(kanban_home, tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "product-repo"
    _init_git_repo(repo)
    kb.ensure_product_board_defaults("prod", name="Product", default_workdir=str(repo))
    monkeypatch.setenv("HERMES_HOME", str(kanban_home))
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(
            pconn,
            name="Product Repo",
            primary_path=str(repo),
            board_slug="prod",
        )

    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="custom workflow",
            project_id=project_id,
            board="prod",
            workflow_template_id="custom",
            current_step_key="intake",
        )
        task = kb.get_task(conn, tid)

    assert task.workflow_template_id == "custom"
    assert task.current_step_key == "intake"


# --- Product-workflow enforcement guards (re-applied from f55580879) ---

def _write_product_board_enf(
    board: str,
    default_workdir: Path,
    *,
    release_assignee: str | None = None,
) -> None:
    kb.create_board(board, name="Product Board", default_workdir=str(default_workdir))
    meta = kb.read_board_metadata(board)
    meta.pop("db_path", None)
    meta["preset"] = "product"
    meta["columns"] = [
        {"name": "backlog", "status": "ready"},
        {"name": "architecture", "status": "ready"},
        {"name": "development", "status": "ready"},
        {"name": "test", "status": "ready"},
        {"name": "review", "status": "review"},
        {"name": "release_measure", "status": "ready"},
        {"name": "done", "status": "done"},
    ]
    assignees = {
        "productowner": "productowner",
        "architect": "architect",
        "developer": "developer",
        "tester": "tester",
        "reviewer": "reviewer",
    }
    if release_assignee:
        assignees["release_measure"] = release_assignee
    meta["product_workflow"] = {"assignees": assignees}
    kb.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")


def test_product_board_role_story_creation_gets_workflow_metadata(kanban_home, tmp_path):
    board = "product-board-enf-create"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_product_board_enf(board, repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="User story: Safe paper order evidence",
            assignee="architect",
            board=board,
        )
        task = kb.get_task(conn, tid)
    # masquerade fix: a plain architect card on a product board becomes a real
    # product story anchored to the correct step (architecture), not stuck.
    assert task.workflow_template_id == kb.PRODUCT_WORKFLOW_TEMPLATE_ID
    assert task.current_step_key == "architecture"
    assert task.status == "ready"


def test_product_board_claim_repairs_legacy_plain_architect_story(kanban_home, tmp_path):
    board = "product-board-enf-claim"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_product_board_enf(board, repo)
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="User story: Legacy card",
            assignee="architect",
            board=board,
        )
        # Simulate the Trading Company regression: a plain architect card with
        # NULL workflow fields slipped onto a product board before this fix.
        conn.execute(
            "UPDATE tasks SET workflow_template_id=NULL, current_step_key=NULL, "
            "workspace_kind='scratch', workspace_path=NULL WHERE id=?",
            (tid,),
        )
        conn.commit()
        claimed = kb.claim_task(conn, tid, board=board)
        repaired = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert claimed is not None
    assert repaired.workflow_template_id == kb.PRODUCT_WORKFLOW_TEMPLATE_ID
    assert repaired.current_step_key == "architecture"
    assert any(e.kind == "workflow_repaired" for e in events)


# ---------------------------------------------------------------------------
# Merge-back (Phase 5): a Done standalone product story reaches LOCAL main.
# Mirrors the merge_epic_to_main tests; LOCAL-only, never pushes, policy-gated.
# ---------------------------------------------------------------------------

def _enable_merge_after_green(board: str) -> None:
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["merge_after_green"] = True
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _make_done_standalone_story(board: str, repo, branch: str = "wt/story-1"):
    """Create a Done, epic-less product story whose branch (off main, one
    commit) exists in ``repo``. Returns (story_id, story_branch_sha)."""
    sha = _make_epic_branch(repo, branch)  # generic: branch off main + 1 commit
    with kb.connect(board=board) as conn:
        story = kb.create_task(
            conn, title="Story: standalone merge-back", board=board,
            branch_name=branch, workspace_kind="worktree", workspace_path=str(repo),
        )
        _set_task_status(conn, story, "done")
    return story, sha


def test_merge_standalone_story_to_main_happy_merges_and_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-merge-happy"
    _v2_product_board_with_repo(board, repo)
    story, sha = _make_done_standalone_story(board, repo)

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn, story, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "merged"
    notify.assert_not_called()
    _assert_no_push(calls)
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 0, "main must contain the story's commit"


def test_release_reverifies_already_merged_standalone_story(
    kanban_home, tmp_path,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-already-merged-release"
    _v2_product_board_with_repo(board, repo)
    story, source_sha = _make_done_standalone_story(board, repo)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--ff-only", "wt/story-1"],
        check=True,
        capture_output=True,
        text=True,
    )
    observed: list[str] = []

    def verify(candidate: Path) -> bool:
        observed.append((candidate / "epic_work.txt").read_text(encoding="utf-8"))
        return True

    with kb.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn,
            story,
            board=board,
            candidate_verify_fn=verify,
            expected_source_sha=source_sha,
            allow_release_measure=True,
        )
        event = next(
            event
            for event in kb.list_events(conn, story)
            if event.kind == "story_merged_to_main"
        )

    assert result == "already_merged"
    assert observed == ["epic work\n"]
    assert event.payload["source_sha"] == source_sha


def test_merge_standalone_story_with_epic_returns_none(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-has-epic"
    _v2_product_board_with_repo(board, repo)
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(
            conn, title="Story", board=board,
            branch_name="wt/s", workspace_kind="worktree", workspace_path=str(repo),
        )
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        _set_task_status(conn, story, "done")
        result = kb._merge_standalone_story_to_main(conn, story, board=board, verify_fn=lambda b: True)
    assert result is None  # epic'd stories go through the epic path, not here


def test_merge_standalone_story_conflict_aborts_blocks_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-merge-conflict"
    _v2_product_board_with_repo(board, repo)
    story, _ = _make_done_standalone_story(board, repo)
    # make main touch the SAME file the story branch changed -> conflict
    _commit_file(repo, "epic_work.txt", "conflicting main content\n", "main change")
    pre_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kb.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn, story, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )
        blocked = kb.get_task(conn, story).blocked

    assert result == "conflict"
    assert blocked, "story must be blocked on conflict"
    notify.assert_called()
    _assert_no_push(calls)
    post_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert post_main == pre_main, "main must be untouched after an aborted conflict"


def test_merge_standalone_story_verify_failure_resets_and_blocks(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-merge-verifyfail"
    _v2_product_board_with_repo(board, repo)
    story, _ = _make_done_standalone_story(board, repo)
    pre_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()

    _record_git_calls(monkeypatch)
    with kb.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn, story, board=board, verify_fn=lambda b: False,  # suite red
        )
        blocked = kb.get_task(conn, story).blocked

    assert result == "verify_failed"
    assert blocked
    post_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert post_main == pre_main, "main must be reset to pre-merge sha on verify failure"


def test_reconcile_merge_after_green_OFF_does_not_merge(kanban_home, tmp_path, monkeypatch):
    """CRITICAL safety: with the default (merge_after_green unset), reconcile
    must NOT merge a done standalone story to main."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-reconcile-mergeback-off"
    _v2_product_board_with_repo(board, repo)  # NOTE: merge_after_green NOT set
    story, sha = _make_done_standalone_story(board, repo)
    pre_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()

    with kb.connect(board=board) as conn:
        result = kb.reconcile(conn, board=board, spawn_fn=lambda *a, **k: None)

    assert result.merged_to_main == [], "must not merge when policy is off"
    post_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert post_main == pre_main, "main must be untouched when merge_after_green is off"


def test_reconcile_merge_after_green_ON_merges_one_standalone_per_pass(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "scripts").mkdir()
    test_script = repo / "scripts" / "run_tests.sh"
    test_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    test_script.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(repo), "add", "scripts/run_tests.sh"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add test gate"],
        check=True, capture_output=True, text=True,
    )
    board = "v2-reconcile-mergeback-on"
    _v2_product_board_with_repo(board, repo)
    _enable_merge_after_green(board)
    story, sha = _make_done_standalone_story(board, repo)
    with kb.connect(board=board) as conn:
        result = kb.reconcile(conn, board=board, spawn_fn=lambda *a, **k: None)

    assert story in result.merged_to_main
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 0, "reconcile must carry the story into main when opted in"


def _set_human_escalation_profile(board: str, profile: str) -> None:
    metadata = kb.read_board_metadata(board)
    metadata.setdefault("product_workflow", {})["human_escalation_profile"] = profile
    kb.board_metadata_path(board).write_text(
        json.dumps(metadata), encoding="utf-8"
    )


def test_block_task_uses_connection_board_for_omitted_escalation(
    kanban_home, monkeypatch
):
    board_a = "block-connection-a"
    board_b = "block-connection-b"
    kb.ensure_product_board_defaults(board_a)
    kb.ensure_product_board_defaults(board_b)
    _set_human_escalation_profile(board_a, "resolver")
    _set_human_escalation_profile(board_b, "wrong-profile")
    kb.set_current_board(board_b)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board_a)))

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="connection-board escalation",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, task_id, board=board_a)
        assert claimed is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Need a human decision",
            kind="needs_input",
            attempted_resolutions=["checked the documented alternatives"],
            expected_run_id=claimed.current_run_id,
        )
        task = kb.get_task(conn, task_id)
        preflight = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "human_input_preflight"
        ][-1]

    assert task.assignee == "resolver"
    assert preflight.payload["hermes_assignee"] == "resolver"


def test_human_cli_block_uses_connection_board_for_omitted_escalation(
    kanban_home, monkeypatch, capsys
):
    board_a = "cli-connection-a"
    board_b = "cli-connection-b"
    kb.ensure_product_board_defaults(board_a)
    kb.ensure_product_board_defaults(board_b)
    _set_human_escalation_profile(board_a, "resolver")
    _set_human_escalation_profile(board_b, "wrong-profile")
    kb.set_current_board(board_b)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board_a)))

    with kb.connect(board=board_a) as conn:
        task_id = kb.create_task(
            conn,
            title="CLI connection-board escalation",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, task_id, board=board_a)
        assert claimed is not None

    from hermes_cli import kanban

    args = types.SimpleNamespace(
        task_id=task_id,
        ids=[],
        reason=["Need", "a", "human", "decision"],
        kind="needs_input",
    )
    assert kanban._cmd_block(args) == 0
    capsys.readouterr()

    with kb.connect(board=board_a) as conn:
        task = kb.get_task(conn, task_id)
        preflight = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "human_input_preflight"
        ][-1]

    assert task.assignee == "resolver"
    assert preflight.payload["hermes_assignee"] == "resolver"


# ---------------------------------------------------------------------------
# D4 resolver escalation answer/re-entry contract
# ---------------------------------------------------------------------------


def _d4_expected_snapshot(conn, task_id: str) -> dict:
    task = kb.get_task(conn, task_id)
    assert task is not None
    events = kb.list_events(conn, task_id)
    blocked = [event for event in events if event.kind == "blocked"][-1]
    resolved = next(event for event in events if event.id == blocked.id - 1)
    preflights = [
        event for event in events
        if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT and event.id < resolved.id
    ]
    assert preflights
    return {
        "escalation_event_id": blocked.id,
        "preflight_event_id": preflights[-1].id,
        "run_id": blocked.run_id,
        "status": task.status,
        "phase": task.current_step_key,
        "assignee": task.assignee,
        "project_id": task.project_id,
        "workflow_template_id": task.workflow_template_id,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "running": task.running,
        "blocked": task.blocked,
    }


def _d4_escalated(conn, board: str, *, step: str = "development") -> tuple[str, int, dict]:
    if step in {"development", "test", "review"}:
        tid, run_id = _route_task_to_resolver(conn, board, step=step)
    else:
        original = "productowner" if step == "backlog" else "measure"
        tid = kb.create_task(
            conn,
            title=f"Story: {step} resolver",
            assignee=original,
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        first = kb.claim_task(conn, tid, board=board)
        assert first is not None and first.current_run_id is not None
        assert kb.block_task(
            conn, tid, reason="Need a decision", kind="needs_input",
            attempted_resolutions=["read docs"],
            expected_run_id=first.current_run_id, board=board,
            human_escalation_assignee="resolver",
        )
        resolver = kb.claim_task(conn, tid, board=board)
        assert resolver is not None and resolver.current_run_id is not None
        run_id = resolver.current_run_id
    assert _resolve_preflight(
        conn, tid, run_id, board, decision="escalate",
        fault_domain="framework", reason="Need operator context",
    )
    return tid, run_id, _d4_expected_snapshot(conn, tid)


def test_d4_answer_reentry_happy_path_and_comments_are_tolerated(kanban_home):
    board = "d4-board"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        kb.add_comment(conn, tid, "operator", "I am checking the fixture.")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (tid, "audit", json.dumps({"note": "read-only observation"}), int(time.time())),
        )
        conn.commit()
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board,
            answer="Use the vendored fixture token.", answered_by="operator",
            expected=expected,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        comments = kb.list_comments(conn, tid)
    assert task is not None
    assert task.assignee == "resolver"
    assert task.status == "ready"
    assert task.blocked is False
    assert task.current_run_id is None
    answer_events = [event for event in events if event.id == event_id]
    assert len(answer_events) == 1
    assert answer_events[0].payload["human_answer"] == "Use the vendored fixture token."
    assert answer_events[0].payload["answered_by"] == "operator"
    assert not [comment for comment in comments if "Use the vendored" in comment.body]


def test_d4_expected_snapshot_is_a_db_boundary_cas(kanban_home):
    board = "d4-cas"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        stale = dict(expected)
        stale["escalation_event_id"] += 1
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=stale,
            )


def test_d4_lifecycle_mutation_makes_escalation_stale(kanban_home):
    board = "d4-stale-lifecycle"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        conn.execute("UPDATE tasks SET current_step_key='review' WHERE id=?", (tid,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (tid, "workflow_step", json.dumps({"current_step_key": "review"}), int(time.time())),
        )
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="stale", answered_by="operator",
                expected=expected,
            )


def test_d4_provenance_rejects_malformed_cross_task_and_wrong_profile(kanban_home):
    board = "d4-provenance"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        resolved_id = expected["escalation_event_id"] - 1
        conn.execute("UPDATE task_events SET payload='not-json' WHERE id=?", (resolved_id,))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )

    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        resolved_id = expected["escalation_event_id"] - 1
        payload = {"action": "escalate", "preflight_event_id": expected["preflight_event_id"]}
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), resolved_id))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )

    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload["hermes_assignee"] = "not-resolver"
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_cross_task_preflight_reference_is_rejected(kanban_home):
    board = "d4-cross-task"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        _, _, other_expected = _d4_escalated(conn, board)
        resolved_id = expected["escalation_event_id"] - 1
        resolved = next(event for event in kb.list_events(conn, tid) if event.id == resolved_id)
        payload = dict(resolved.payload or {})
        payload["preflight_event_id"] = other_expected["preflight_event_id"]
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), resolved_id),
        )
        conn.commit()
        current = kb.resolver_escalation_expected_snapshot(conn, tid)
        assert current is not None
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="cross-task", answered_by="operator",
                expected=current,
            )


def test_d4_rejects_same_task_historical_run_substitution(kanban_home):
    board = "d4-historical-run"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, first_run, first_expected = _d4_escalated(conn, board)
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="first answer", answered_by="operator",
            expected=first_expected,
        )
        fresh = kb.claim_task(conn, tid, board=board)
        assert fresh is not None and fresh.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, fresh.current_run_id, board, decision="escalate",
            fault_domain="framework", reason="Second escalation",
        )
        current = _d4_expected_snapshot(conn, tid)
        conn.execute("UPDATE task_events SET run_id=? WHERE id=?", (first_run, current["escalation_event_id"]))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="stale second", answered_by="operator",
                expected=current,
            )


@pytest.mark.parametrize("bad_identity", ["", "   ", None, 0, [], {}, "x" * 257])
def test_d4_rejects_blank_or_non_string_answered_by(kanban_home, bad_identity):
    board = "d4-identity"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        with pytest.raises(ValueError, match="answered_by"):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by=bad_identity,
                expected=expected,
            )


@pytest.mark.parametrize("bad_answer", ["", "   ", None, 0, [], {}, "x" * 2049])
def test_d4_rejects_blank_non_string_or_oversized_answer(kanban_home, bad_answer):
    board = "d4-answer"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        with pytest.raises(ValueError, match="answer"):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer=bad_answer, answered_by="operator",
                expected=expected,
            )


@pytest.mark.parametrize("raw_assignee", [{}, [], 0, False, "", "   ", "x" * 257])
def test_d4_source_assignee_is_validated_before_coercion(kanban_home, raw_assignee):
    board = "d4-source-assignee"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload["original_assignee"] = raw_assignee
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_missing_or_none_assignee_uses_governed_backlog_derivation(kanban_home):
    board = "d4-backlog"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board, step="backlog")
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload.pop("original_assignee", None)
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="backlog answer", answered_by="operator",
            expected=expected,
        )
        events = kb.list_events(conn, tid)
    reentry = [event for event in events if event.payload.get("kind") == "resolver_reentry"][-1]
    assert reentry.payload["original_assignee"] == "productowner"


def test_d4_release_measure_resume_preserves_canonical_none(kanban_home):
    board = "d4-release-measure"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board, step="release_measure")
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload["original_assignee"] = None
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="measure answer", answered_by="operator",
            expected=expected,
        )
        event = [event for event in kb.list_events(conn, tid) if event.payload.get("kind") == "resolver_reentry"][-1]
    assert event.payload["original_assignee"] is None


def test_d4_release_measure_resume_restores_none_after_fresh_resolver(kanban_home):
    board = "d4-release-measure-resume"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board, step="release_measure")
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        payload = dict(preflight.payload)
        payload["original_assignee"] = None
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), preflight.id),
        )
        conn.commit()
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="measure answer", answered_by="operator",
            expected=expected,
        )
        fresh = kb.claim_task(conn, tid, board=board)
        assert fresh is not None and fresh.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, fresh.current_run_id, board, decision="resume",
            fault_domain="task_state", reason="Apply measure answer",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee is None
    assert task.current_step_key == "release_measure"


@pytest.mark.parametrize("bad_configured_assignee", ["x" * 257, "", "   ", False, 0, [], {}])
def test_d4_configured_assignee_is_validated_before_helper_coercion(
    kanban_home, bad_configured_assignee,
):
    board = "d4-configured-assignee"
    _v2_product_board(board)
    metadata = kb.read_board_metadata(board)
    workflow = metadata.setdefault("product_workflow", {})
    workflow.setdefault("assignees", dict(kb.PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES))
    workflow["assignees"]["developer"] = bad_configured_assignee
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        payload = dict(preflight.payload)
        payload["original_assignee"] = None
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), preflight.id),
        )
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_non_resolver_block_fails_closed(kanban_home):
    board = "d4-non-resolver-block"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["escalation_event_id"]
        )
        payload = dict(blocked.payload)
        payload["kind"] = "needs_input"
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), blocked.id),
        )
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_attempted_resolutions_are_bounded_and_one_event_is_written(kanban_home):
    board = "d4-bounds"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(event for event in kb.list_events(conn, tid) if event.id == expected["escalation_event_id"])
        payload = dict(blocked.payload)
        payload["attempted_resolutions"] = ["x" * 1000 for _ in range(100)]
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), blocked.id))
        conn.commit()
        before = len(kb.list_events(conn, tid))
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="bounded", answered_by="operator",
            expected=expected,
        )
        events = kb.list_events(conn, tid)
    assert len(events) == before + 1
    answered = next(event for event in events if event.id == event_id)
    assert len(answered.payload["attempted_resolutions"]) <= 20
    assert sum(len(item) for item in answered.payload["attempted_resolutions"]) <= 4096


def test_d4_second_escalation_reentry_cycle_works(kanban_home):
    board = "d4-repeat"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, first_expected = _d4_escalated(conn, board)
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="first", answered_by="operator",
            expected=first_expected,
        )
        fresh = kb.claim_task(conn, tid, board=board)
        assert fresh is not None and fresh.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, fresh.current_run_id, board, decision="resume",
            fault_domain="task_state", reason="First answer applied",
        )
        worker = kb.claim_task(conn, tid, board=board)
        assert worker is not None and worker.current_run_id is not None
        assert kb.block_task(
            conn, tid, reason="Need another decision", kind="needs_input",
            expected_run_id=worker.current_run_id, board=board,
            human_escalation_assignee="resolver",
        )
        second = kb.claim_task(conn, tid, board=board)
        assert second is not None and second.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, second.current_run_id, board, decision="escalate",
            fault_domain="framework", reason="Second question",
        )
        second_expected = _d4_expected_snapshot(conn, tid)
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="second", answered_by="operator",
            expected=second_expected,
        )
        events = kb.list_events(conn, tid)
    assert event_id > 0
    assert len([event for event in events if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT]) == 4


def test_d4_concurrent_answers_have_exactly_one_winner_and_public_conflict(kanban_home):
    board = "d4-concurrent"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
    barrier = threading.Barrier(2)
    results = []

    def answer(answer_text):
        with kb.connect(board=board) as conn:
            barrier.wait(timeout=5)
            try:
                event_id = kb.reenter_resolver_escalation(
                    conn, tid, board=board, answer=answer_text, answered_by="operator",
                    expected=dict(expected),
                )
                results.append(("ok", event_id))
            except Exception as exc:
                results.append(("error", exc))

    threads = [threading.Thread(target=answer, args=(f"answer-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len([result for result in results if result[0] == "ok"]) == 1, results
    errors = [result[1] for result in results if result[0] == "error"]
    assert len(errors) == 1, results
    assert isinstance(errors[0], kb.TaskSnapshotConflict), errors
    with kb.connect(board=board) as conn:
        answer_events = [
            event for event in kb.list_events(conn, tid)
            if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT
            and event.payload.get("kind") == "resolver_reentry"
        ]
    assert len(answer_events) == 1


def test_d4_real_dispatcher_claims_development_and_review_reentry(kanban_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    original_claim_review = kb.claim_review_task
    for step, board in [("development", "d4-dispatch-dev"), ("review", "d4-dispatch-review")]:
        _v2_product_board(board)
        with kb.connect(board=board) as conn:
            tid, _, expected = _d4_escalated(conn, board, step=step)
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="dispatch answer", answered_by="operator",
                expected=expected,
            )

        calls = []
        monkeypatch.setattr(
            kb, "claim_review_task",
            lambda conn, task_id, _claim=original_claim_review, **kwargs: (
                calls.append(task_id), _claim(conn, task_id, **kwargs)
            )[1],
        )
        with kb.connect(board=board) as conn:
            result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: 9001, board=board)
            task = kb.get_task(conn, tid)
            run = kb.get_run(conn, task.current_run_id) if task and task.current_run_id else None
        assert tid in [spawn[0] for spawn in result.spawned]
        assert task is not None and task.current_run_id is not None
        assert run is not None and run.profile == "resolver"


@pytest.mark.parametrize(
    "field",
    [
        "assignee",
        "project_id",
        "workflow_template_id",
        "current_step_key",
        "workspace_path",
        "branch_name",
    ],
)
def test_d3_rejects_oversized_cas_metadata_before_create_mutation(kanban_home, field):
    kwargs = {
        "title": "D3 oversized metadata",
        "workspace_kind": "worktree",
        field: "值🙂é" * 30_000,
    }
    if field in {"workflow_template_id", "current_step_key"}:
        kwargs["workflow_template_id"] = "product"
        kwargs["current_step_key"] = "development"
        kwargs[field] = "值🙂é" * 30_000
    if field == "project_id":
        # The bound must be checked before project lookup, so a huge ID cannot
        # be hidden behind the normal unknown-project error.
        kwargs["workspace_kind"] = "scratch"

    with kb.connect() as conn:
        with pytest.raises(ValueError, match=field):
            kb.create_task(conn, **kwargs)
        assert kb.list_tasks(conn) == []


def test_d4_rejects_coherently_forged_non_resolver_cycle_without_mutation(kanban_home):
    board = "d4-forged-resolver"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked_id = expected["escalation_event_id"]
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        resolved_id = blocked_id - 1
        resolved = next(event for event in kb.list_events(conn, tid) if event.id == resolved_id)
        forged_preflight = dict(preflight.payload)
        forged_preflight["hermes_assignee"] = "developer"
        forged_resolved = dict(resolved.payload)
        forged_resolved["resolver_profile"] = "developer"
        conn.execute(
            "UPDATE task_runs SET profile='developer' WHERE id=?",
            (expected["run_id"],),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_preflight), preflight.id),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_resolved), resolved.id),
        )
        conn.commit()
        before_task = kb.get_task(conn, tid)
        before_events = kb.list_events(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert kb.get_task(conn, tid) == before_task
        assert kb.list_events(conn, tid) == before_events


def test_d4_default_human_escalation_profile_accepts_resolver_cycle(kanban_home):
    board = "d4-default-human-profile"
    _v2_product_board(board)
    _set_human_escalation_profile(board, "default")
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="answer", answered_by="operator",
            expected=expected,
        )
        task = kb.get_task(conn, tid)
        answer_event = next(event for event in kb.list_events(conn, tid) if event.id == event_id)
    assert task is not None and task.assignee == "resolver"
    assert answer_event.payload["hermes_assignee"] == "resolver"


def test_d4_rejects_coherently_forged_default_cycle_without_mutation(kanban_home):
    board = "d4-forged-default"
    _v2_product_board(board)
    _set_human_escalation_profile(board, "default")
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked_id = expected["escalation_event_id"]
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        resolved_id = blocked_id - 1
        resolved = next(event for event in kb.list_events(conn, tid) if event.id == resolved_id)
        forged_preflight = dict(preflight.payload)
        forged_preflight["hermes_assignee"] = "default"
        forged_resolved = dict(resolved.payload)
        forged_resolved["resolver_profile"] = "default"
        conn.execute(
            "UPDATE task_runs SET profile='default' WHERE id=?",
            (expected["run_id"],),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_preflight), preflight.id),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_resolved), resolved.id),
        )
        conn.commit()
        before = _full_tables_state(conn)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert _full_tables_state(conn) == before


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("reason", 123),
        ("reason", None),
        ("resolution", {}),
        ("resolution", False),
        ("attempted_resolutions", {}),
        ("attempted_resolutions", None),
        ("attempted_resolutions", ["valid", 7]),
    ],
)
def test_d4_rejects_present_malformed_copied_evidence_atomically(
    kanban_home, field, bad_value
):
    board = f"d4-evidence-{field}-{type(bad_value).__name__}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(event for event in kb.list_events(conn, tid) if event.id == expected["escalation_event_id"])
        payload = dict(blocked.payload)
        payload[field] = bad_value
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), blocked.id),
        )
        conn.commit()
        before_task = kb.get_task(conn, tid)
        before_events = kb.list_events(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert kb.get_task(conn, tid) == before_task
        assert kb.list_events(conn, tid) == before_events


def test_d4_absent_optional_copied_evidence_remains_distinct_from_malformed(kanban_home):
    board = "d4-evidence-absent"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(event for event in kb.list_events(conn, tid) if event.id == expected["escalation_event_id"])
        payload = dict(blocked.payload)
        payload.pop("reason")
        payload.pop("resolution")
        payload.pop("attempted_resolutions")
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), blocked.id),
        )
        conn.commit()
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="answer", answered_by="operator",
            expected=expected,
        )
        answer_event = next(event for event in kb.list_events(conn, tid) if event.id == event_id)
    assert answer_event.payload["reason"] == ""
    assert answer_event.payload["resolver_escalation_reason"] == ""
    assert answer_event.payload["attempted_resolutions"] == []


@pytest.mark.parametrize(
    "event_kind",
    [
        "dispatcher_metadata_conflict",
        "diagnostic",
        "heartbeat",
        "traceability",
        "unknown",
        "workflow_step",
        "status_changed",
    ],
)
def test_d4_non_audit_events_invalidate_escalation_without_mutation(kanban_home, event_kind):
    board = f"d4-event-{event_kind}"
    _v2_product_board(board)
    with kb.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (tid, event_kind, json.dumps({"note": "later event"}), int(time.time())),
        )
        conn.commit()
        before_task = kb.get_task(conn, tid)
        before_events = kb.list_events(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert kb.get_task(conn, tid) == before_task
        assert kb.list_events(conn, tid) == before_events
























def test_qualification_attempt_budget_counts_all_historical_runs_and_preserves_history(
    kanban_home,
):
    board = "qualification-attempt-budget"
    kb.ensure_product_board_defaults(board)
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["max_total_attempts"] = 3
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with kb.connect(board=board) as conn:
        intake_id = kb.create_qualification_intake(
            conn, raw_request="budget me", source="chat", created_at=10
        )
        runtime = {"provider": "test", "model": "test", "effort": "low"}
        runs = []
        for now in (20, 30, 40):
            run = kb.claim_qualification_intake(
                conn,
                intake_id,
                profile="productowner",
                runtime_identity=runtime,
                now=now,
            )
            assert run is not None
            runs.append(run)
            assert kb.finish_qualification_intake_run(
                conn,
                intake_id=intake_id,
                run_id=run["id"],
                claim_lock=run["claim_lock"],
                intake_status="attention_required",
                outcome="attention_required",
                now=now + 1,
            )
            if now != 40:
                assert kb.retry_qualification_intake(conn, intake_id, now=now + 2)
        state = kb.qualification_retry_state(conn, intake_id, 3)
        assert state.attempts_used == 3
        assert state.attempts_limit == 3
        assert state.allowed is False
        assert state.reason == "attempt_budget_exhausted"
        before_runs = conn.execute(
            "SELECT id, status, outcome FROM qualification_intake_runs "
            "WHERE intake_id = ? ORDER BY id",
            (intake_id,),
        ).fetchall()
        with pytest.raises(ValueError, match="attempt_budget_exhausted"):
            kb.retry_qualification_intake(conn, intake_id, now=50)
        after_runs = conn.execute(
            "SELECT id, status, outcome FROM qualification_intake_runs "
            "WHERE intake_id = ? ORDER BY id",
            (intake_id,),
        ).fetchall()
    assert [tuple(row) for row in after_runs] == [tuple(row) for row in before_runs]


def _configure_task_expected(task):
    if task.source_commit_required:
        source_policy = "required"
    elif task.source_commit_forbidden:
        source_policy = "forbidden"
    else:
        source_policy = "none"
    return {
        "status": task.status,
        "title": task.title,
        "assignee": task.assignee,
        "current_step_key": task.current_step_key,
        "current_run_id": task.current_run_id,
        "source_policy": source_policy,
        "max_retries": task.max_retries,
        "max_runtime_seconds": task.max_runtime_seconds,
        "goal_mode": task.goal_mode,
    }


def _configure_task_row_and_events(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row), [
        (event.kind, event.payload) for event in kb.list_events(conn, task_id)
    ]


def test_configure_task_atomically_clears_contract_and_records_exact_event(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="configure existing card",
            assignee="developer",
            source_commit_required=True,
            max_retries=3,
            max_runtime_seconds=900,
            goal_mode=True,
        )
        before = kb.get_task(conn, task_id)
        assert before is not None

        assert kb.configure_task(
            conn,
            task_id,
            expected=_configure_task_expected(before),
            source_policy="none",
            max_retries=None,
            max_runtime_seconds=None,
            goal_mode=False,
        ) is True

        after = kb.get_task(conn, task_id)
        configured = kb.list_events(conn, task_id)[-1]

    assert after is not None
    assert after.source_commit_required is False
    assert after.source_commit_forbidden is False
    assert after.max_retries is None
    assert after.max_runtime_seconds is None
    assert after.goal_mode is False
    assert configured.kind == "execution_contract_configured"
    assert configured.payload == {
        "before": {
            "source_policy": "required",
            "max_retries": 3,
            "max_runtime_seconds": 900,
            "goal_mode": True,
        },
        "after": {
            "source_policy": "none",
            "max_retries": None,
            "max_runtime_seconds": None,
            "goal_mode": False,
        },
    }


@pytest.mark.parametrize(
    ("status", "blocked"),
    [
        ("triage", 0),
        ("todo", 0),
        ("scheduled", 0),
        ("ready", 0),
        ("blocked", 1),
        ("review", 0),
    ],
)
def test_configure_task_allows_each_eligible_status(kanban_home, status, blocked):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"eligible {status}")
        conn.execute(
            "UPDATE tasks SET status = ?, blocked = ? WHERE id = ?",
            (status, blocked, task_id),
        )
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None

        assert kb.configure_task(
            conn,
            task_id,
            expected=_configure_task_expected(task),
            source_policy="forbidden",
            max_retries=2,
            max_runtime_seconds=120,
            goal_mode=True,
        ) is True


@pytest.mark.parametrize("stale_field", ["title", "max_retries"])
def test_configure_task_cas_rejects_stale_lifecycle_and_execution_fields(
    kanban_home, stale_field
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale card")
        task = kb.get_task(conn, task_id)
        assert task is not None
        expected = _configure_task_expected(task)
        if stale_field == "title":
            conn.execute(
                "UPDATE tasks SET title = ? WHERE id = ?", ("changed", task_id)
            )
        else:
            conn.execute(
                "UPDATE tasks SET max_retries = ? WHERE id = ?", (4, task_id)
            )
        conn.commit()
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(kb.TaskSnapshotConflict):
            kb.configure_task(
                conn,
                task_id,
                expected=expected,
                source_policy="required",
                max_retries=1,
                max_runtime_seconds=300,
                goal_mode=True,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_configure_task_refuses_second_write_with_same_expectation(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="single CAS write")
        task = kb.get_task(conn, task_id)
        assert task is not None
        expected = _configure_task_expected(task)
        values = {
            "source_policy": "required",
            "max_retries": 1,
            "max_runtime_seconds": 300,
            "goal_mode": True,
        }
        assert kb.configure_task(
            conn, task_id, expected=expected, **values
        ) is True
        before_replay = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(kb.TaskSnapshotConflict):
            kb.configure_task(conn, task_id, expected=expected, **values)

        assert _configure_task_row_and_events(conn, task_id) == before_replay


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_policy", "sometimes"),
        ("max_retries", 0),
        ("max_retries", True),
        ("max_runtime_seconds", 0),
        ("max_runtime_seconds", True),
        ("goal_mode", "false"),
    ],
)
def test_configure_task_rejects_invalid_values_without_mutation(
    kanban_home, field, value
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="invalid contract")
        task = kb.get_task(conn, task_id)
        assert task is not None
        values = {
            "source_policy": "none",
            "max_retries": None,
            "max_runtime_seconds": None,
            "goal_mode": False,
        }
        values[field] = value
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(ValueError):
            kb.configure_task(
                conn,
                task_id,
                expected=_configure_task_expected(task),
                **values,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_configure_task_rejects_incomplete_expected_snapshot_without_mutation(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="incomplete expectation")
        task = kb.get_task(conn, task_id)
        assert task is not None
        expected = _configure_task_expected(task)
        expected.pop("goal_mode")
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(ValueError, match="expected"):
            kb.configure_task(
                conn,
                task_id,
                expected=expected,
                source_policy="none",
                max_retries=None,
                max_runtime_seconds=None,
                goal_mode=False,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


@pytest.mark.parametrize("status", ["running", "done", "archived"])
def test_configure_task_refuses_terminal_status_without_mutation(
    kanban_home, status
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"terminal {status}")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(RuntimeError, match="status"):
            kb.configure_task(
                conn,
                task_id,
                expected=_configure_task_expected(task),
                source_policy="required",
                max_retries=1,
                max_runtime_seconds=300,
                goal_mode=True,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_configure_task_refuses_active_current_run_without_mutation(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="active run")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(RuntimeError, match="active/current run"):
            kb.configure_task(
                conn,
                task_id,
                expected=_configure_task_expected(claimed),
                source_policy="required",
                max_retries=1,
                max_runtime_seconds=300,
                goal_mode=True,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_engine_owned_integration_pending_refuses_public_lifecycle_paths(
    kanban_home, monkeypatch
):
    board = "engine-owned-guards"
    _v2_product_board(board)
    monkeypatch.setattr(
        kb, "_integration_git",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not call Git"),
    )
    monkeypatch.setattr(
        kb.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not spawn Git"),
    )
    with kb.connect(board=board) as conn:
        story_id = kb.create_task(
            conn, title="Story", board=board,
            workflow_template_id="product", current_step_key="review",
        )
        conn.execute(
            "UPDATE tasks SET current_step_key='integration_pending', status='ready', "
            "assignee=NULL WHERE id=?", (story_id,),
        )
        assert kb.claim_task(conn, story_id, board=board) is None
        assert kb.complete_task(conn, story_id, board=board) is False
        assert kb.set_phase(conn, story_id, "development", board=board) is False
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (story_id,))
        promoted, reason = kb.promote_task(conn, story_id, actor="operator", force=True)
        assert promoted is False
        assert reason == "engine-owned integration state cannot be promoted"
        assert kb.recompute_ready(conn) == 0
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (story_id,))
        assert kb.unblock_task(conn, story_id) is False
        with pytest.raises(kb.ReleaseEvidenceError) as exc:
            kb.release_product_task(
                conn, story_id, board,
                candidate_verify_fn=None, release_adapter=None,
            )
        assert exc.value.missing == ["engine_owned_state"]
        with pytest.raises(ValueError, match="engine-owned"):
            kb.create_task(
                conn, title="Fabricated inbox delivery", board=board,
                workflow_template_id="product", current_step_key="integration_pending",
            )


def test_product_epic_and_legacy_reconcile_are_structurally_refused_without_git(
    kanban_home, monkeypatch
):
    board = "product-epic-guards"
    _v2_product_board(board)
    monkeypatch.setattr(
        kb, "_integration_git",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not call Git"),
    )
    monkeypatch.setattr(
        kb.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not spawn Git"),
    )
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story_id = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product_epic', "
            "current_step_key='collecting_members', status='todo' WHERE id=?", (epic_id,),
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product', current_step_key='done', "
            "status='done', completed_at=1 WHERE id=?", (story_id,),
        )
        assert kb.merge_epic_to_main(conn, epic_id, board=board) == "not_ready"
        assert kb.complete_task(conn, epic_id, board=board) is False
        assert kb.promote_task(conn, epic_id, actor="operator", force=True)[0] is False
        with pytest.raises(kb.ReleaseEvidenceError) as exc:
            kb.release_product_task(
                conn, epic_id, board,
                candidate_verify_fn=lambda _path: True,
                release_adapter=None,
                completion_metadata={
                    "workflow_outcome": {"verdict": "approved"},
                    "candidate_sha": "f" * 40,
                },
            )
        assert exc.value.missing == ["engine_owned_state"]
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        facts = conn.execute(
            "SELECT COUNT(*) FROM epic_story_integrations WHERE story_id=?", (story_id,),
        ).fetchone()[0]
    assert result.integrated == []
    assert facts == 0


def test_integration_enqueued_complete_task_routes_approved_member(
    kanban_home, tmp_path
):
    board = "member-review-enqueue"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    base_sha = _git_output(repo, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "story/member"],
        check=True, capture_output=True, text=True,
    )
    source_sha = _commit_file(repo, "member.txt", "member\n", "member")
    _v2_product_board(board)
    now = int(time.time())
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "tester": {"agent": "tester", "result": "passed"},
        },
        "test_branch": "story/member",
        "test_head_sha": source_sha,
    }
    review_pins = {
        "review_branch": "story/member",
        "review_base_sha": base_sha,
        "review_head_sha": source_sha,
    }
    completion_metadata = {
        "workflow_outcome": {"verdict": "approved"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "reviewer": {"agent": "reviewer"},
        },
    }
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story_id = kb.create_task(
            conn, title="Story", board=board, assignee="reviewer",
            workflow_template_id="product", current_step_key="review",
            workspace_kind="worktree", workspace_path=str(repo),
            branch_name="story/member",
        )
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(test_metadata), now - 2, now - 1),
        )
        review_run_id = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, step_key, status, metadata, started_at) "
            "VALUES (?, 'reviewer', 'review', 'running', ?, ?)",
            (story_id, json.dumps(review_pins), now),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status='running', running=1, current_run_id=? WHERE id=?",
            (review_run_id, story_id),
        )

        assert kb.complete_task(
            conn, story_id, board=board, expected_run_id=review_run_id,
            summary="approved", metadata=completion_metadata,
        ) is True
        task = kb.get_task(conn, story_id)
        intents = conn.execute(
            "SELECT * FROM story_integration_intents WHERE story_id=?", (story_id,),
        ).fetchall()

    assert task is not None
    assert task.current_step_key == "integration_pending"
    assert task.status == "review"
    assert task.assignee is None
    assert len(intents) == 1
    assert intents[0]["source_sha"] == source_sha
