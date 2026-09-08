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
import hermes_state_wal
from hermes_cli import kanban_db as kb
import hermes_cli.kanban_db_connect as kanban_db_connect
import hermes_cli.kanban_db_workspace as kanban_db_workspace
import shutil as shutil
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
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

    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect() as conn:
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

    with kanban_db_connect.connect() as conn:
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

    with kanban_db_connect.connect(db_path) as conn:
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
    with kanban_db_connect.connect(db_path) as conn:
        migrated_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert migrated_tables - (modern_tables - expected) == expected
    assert migrated_tables == modern_tables

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kanban_db_connect.connect(db_path) as conn:
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




@pytest.mark.windows_only
def test_cross_process_init_lock_uses_windows_byte_range_lock(tmp_path, monkeypatch):
    """Windows must use a real (non-blocking) process lock, not a no-op open.

    The init lock acquires with LK_NBLCK in a bounded retry loop (#36644) so a
    wedged holder can never block connect() forever; a clean acquire takes the
    lock once and releases it once.

    ``windows_only``: ``msvcrt`` does not exist off Windows, so faking
    ``_IS_WINDOWS`` on Linux meant injecting a fake ``msvcrt`` module too —
    the test then asserted against its own stub rather than the byte-range
    locking API. Here the platform is real; only ``msvcrt.locking`` is
    instrumented so the call sequence is observable.
    """
    calls: list[tuple[int, int, int]] = []
    import msvcrt as _msvcrt

    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=_msvcrt.LK_NBLCK,
        LK_UNLCK=_msvcrt.LK_UNLCK,
        locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    db_path = tmp_path / "kanban.db"
    with kbc._cross_process_init_lock(db_path):
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

    with kbc.connect(db_path) as migrated:
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
    with kanban_db_connect.connect() as conn:
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

    with kanban_db_connect.connect(db_path) as migrated:
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
    with kanban_db_connect.connect(db_path) as migrated_again:
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
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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
    workspace = kanban_db_workspace.resolve_workspace(task, board=board)
    kanban_db_workspace.set_workspace_path(conn, tid, workspace)
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


# ---------------------------------------------------------------------------
# _commit_worker_diff (Phase 2 atomic commit-first handoff)
# ---------------------------------------------------------------------------

def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# handoff() -- atomic commit-first advance (T2.2-T2.4)
# ---------------------------------------------------------------------------

def _card_snapshot(conn: sqlite3.Connection, task_id: str) -> dict:
    return dict(conn.execute(
        "SELECT current_step_key, running, blocked, status, assignee, result "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone())




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
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(
            conn, title="dir child", workspace_kind="dir",
            workspace_path=str(child_dir),
        )
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kbw.resolve_workspace(p_task)
        kbw.set_workspace_path(conn, parent, parent_ws)

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
    assert not kbw._is_managed_scratch_path(kanban_root)

    logs_dir = kanban_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    assert not kbw._is_managed_scratch_path(logs_dir)

    board_root = kanban_root / "boards" / "my-board"
    board_root.mkdir(parents=True, exist_ok=True)
    # The board root itself is NOT a managed scratch dir — only the
    # ``workspaces/`` child (and its descendants) are.
    assert not kbw._is_managed_scratch_path(board_root)

    # Sibling subtrees of ``workspaces/`` under a board (e.g. its kanban.db
    # or board.json living next to ``workspaces/``) are also not managed.
    board_logs = board_root / "logs"
    board_logs.mkdir(parents=True, exist_ok=True)
    assert not kbw._is_managed_scratch_path(board_logs)

    # Now create the board's workspaces dir and a task scratch dir under it —
    # the latter is the only thing the guard should allow.
    board_workspaces = board_root / "workspaces"
    board_workspaces.mkdir(parents=True, exist_ok=True)
    # The workspaces root itself is also NOT managed — deleting it would
    # wipe every task's scratch dir at once.
    assert not kbw._is_managed_scratch_path(board_workspaces)
    task_dir = board_workspaces / "task-42"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert kbw._is_managed_scratch_path(task_dir)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------





def test_product_completion_advances_card_to_next_role(kanban_home):
    kb.create_board("prod", preset="product")
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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


@pytest.mark.parametrize("entrypoint", ["legacy", "v2", "handoff"])
@pytest.mark.parametrize("verdict", ["passed", "changes_requested"])
def test_ordinary_test_cannot_forge_recovery_purpose(kanban_home, tmp_path, entrypoint, verdict):
    board = "reserved-recovery-purpose"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    path = kb.board_metadata_path(board)
    board_metadata = json.loads(path.read_text())
    board_metadata["product_workflow"]["handoff_v2"] = entrypoint != "legacy"
    board_metadata["product_workflow"]["ai_provenance_required"] = False
    path.write_text(json.dumps(board_metadata))
    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, _ = _seed_product_test_worktree(conn, board, repo)
        claimed = kb.claim_task(conn, tid, board=board)
        assert claimed and claimed.current_run_id
        pins = kb._prepare_test_target(conn, tid, workspace, board=board)
        kb._synthesize_ended_run(conn, tid, outcome="advanced", step_key="test", metadata={
            **pins, "workflow_outcome": {"verdict": "passed"},
            "ai_provenance": {"writer": {"agent": "codex"}, "tester": {"agent": "codex"}},
        })
        kb._synthesize_ended_run(conn, tid, outcome="crashed", step_key="test", metadata=pins)
        assert not kb._latest_test_target(conn, tid)
        before = kb.get_run(conn, claimed.current_run_id)
        outcome = {"verdict": verdict}
        if verdict == "changes_requested":
            outcome.update({"target_step": "development", "findings": ["Fixture failed"]})
        finish = kb.handoff if entrypoint == "handoff" else kb.complete_task
        with pytest.raises(kb.ProductProvenanceError, match="resolver.*reserved|reserved.*resolver"):
            finish(
                conn, tid, board=board, expected_run_id=claimed.current_run_id,
                summary="Ordinary Test cannot opt out of product evidence",
                metadata={"resolver": {}, "workflow_outcome": outcome, **pins,
                          "ai_provenance": {"tester": {"agent": "codex"}, "writer": {"agent": "codex"}}},
            )
        assert kb.get_run(conn, claimed.current_run_id) == before
        assert kb.get_task(conn, tid).current_step_key == "test"
        assert not kb._latest_test_target(conn, tid)


def test_product_development_completion_requires_writer_provenance(kanban_home):
    kb.create_board("prod", preset="product")
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect() as conn:
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
        result = kbd.dispatch_once(
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect(board="prod") as conn:
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
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
    with kanban_db_connect.connect() as conn:
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
        with kbc.connect() as conn:
            task_id = kb.create_task(conn, title="cross-profile")

        # Worker switches to the profile HERMES_HOME and reads.
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        with kbc.connect() as conn:
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
        kbd._default_spawn(task, str(tmp_path / "ws"))

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
# NFS / network-filesystem fallback (see hermes_state_wal.apply_wal_with_fallback)
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
    import hermes_state_wal as _hermes_state_wal
    monkeypatch.setattr(
        _hermes_state_wal, "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )
    _hermes_state_wal._wal_fallback_warned_paths.clear()

    # Clear module cache so a fresh connect() is attempted
    kb._INITIALIZED_PATHS.clear()
    hermes_state_wal._wal_fallback_warned_paths.clear()

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
            conn = kbc.connect()

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
    hermes_state_wal._wal_fallback_warned_paths.clear()
    # Assume a fixed SQLite so the WAL-reset gate doesn't short-circuit.
    monkeypatch.setattr(
        hermes_state_wal, "is_sqlite_wal_reset_vulnerable",
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
            conn = kbc.connect()

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


def test_sqlite_connect_closes_tracked_conn_on_setup_failure(tmp_path, monkeypatch):
    """A PRAGMA failure after connect must not abandon a tracked kanban fd."""
    from hermes_cli import sqlite_safe_read

    db_path = tmp_path / "kanban.db"
    real_connect = sqlite3.connect
    opened = []

    class _BusyTimeoutFailure(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if str(sql).startswith("PRAGMA busy_timeout="):
                raise sqlite3.OperationalError("simulated setup failure")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(*args, **kwargs):
        kwargs.pop("factory", None)
        conn = real_connect(*args, factory=_BusyTimeoutFailure, **kwargs)
        opened.append(conn)
        return conn

    key = sqlite_safe_read._key(db_path)
    with sqlite_safe_read._live_lock:
        before = sqlite_safe_read._live_connections.get(key, 0)
    monkeypatch.setattr(kb.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError, match="simulated setup failure"):
        kbc._sqlite_connect(db_path)

    with sqlite_safe_read._live_lock:
        after = sqlite_safe_read._live_connections.get(key, 0)
    assert after == before


def test_unlink_tasks_promotes_only_named_child(kanban_home):
    """Regression test for issue #22459.

    Removing a dependency via unlink_tasks must immediately promote the child
    to ready when all remaining parents are done — same contract as
    complete_task and unblock_task.

    Before the fix, child stayed 'todo' indefinitely after unlink; only the
    next dispatcher tick or a manual 'hermes kanban recompute' would promote it.
    """
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect() as conn:
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

    from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )

    # First call adds the column — returns True.
    added = _add_column_if_missing(conn, "tasks", "extra_col", "extra_col TEXT")
    assert added is True
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "extra_col" in cols

    # Second call on same connection — column already exists — must return
    # False without raising, simulating the race the dispatcher hits.
    added_again = _add_column_if_missing(
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
    kbc._migrate_add_optional_columns(conn)
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
    import hermes_cli.kanban_db_connect as kanban_db_connect
    import hermes_cli.kanban_db_workspace as kanban_db_workspace
    import shutil as shutil
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = kbd._resolve_hermes_argv()
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
    import hermes_cli.kanban_db_connect as kanban_db_connect
    import hermes_cli.kanban_db_workspace as kanban_db_workspace
    import shutil as shutil
    from hermes_cli import kanban_db_dispatch as kbd
    import shutil
    import unittest.mock as mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kbd._resolve_hermes_argv()
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


def test_dispatch_max_in_progress_blocks_review_when_at_limit(
    kanban_home, all_assignees_spawnable,
):
    """Review-only backlog must still respect max_in_progress."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, running)
        review = kb.create_task(conn, title="review", assignee="bob")
        _set_task_status(conn, review, "review")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=1)
        review_task = kb.get_task(conn, review)

    assert not res.spawned
    assert not spawns
    assert review_task is not None
    assert review_task.status == "review"

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
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kbd.dispatch_once(conn, dry_run=True)
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == t
    # Dry run must NOT mutate status.
    with kanban_db_connect.connect() as conn:
        assert kb.get_task(conn, t).status == "review"


def test_dispatch_review_does_not_force_profile_scoped_skill(
    kanban_home, all_assignees_spawnable,
):
    """Review lifecycle guidance comes from the reviewer profile."""
    spawned_tasks = []

    def capture_spawn(task, workspace, board=None):
        spawned_tasks.append(task)
        return 42  # fake PID

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kbd.dispatch_once(conn, spawn_fn=capture_spawn)
    assert len(res.spawned) == 1
    assert len(spawned_tasks) == 1
    assert spawned_tasks[0].skills is None


def test_dispatch_review_skips_unassigned(kanban_home):
    """Unassigned review tasks go to skipped_unassigned, not spawned."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="review floater")
        _set_task_status(conn, t, "review")
        res = kbd.dispatch_once(conn, dry_run=True)
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

    with kanban_db_connect.connect() as conn:
        # Create 2 ready tasks + 1 review task, max_spawn=2
        t1 = kb.create_task(conn, title="ready 1", assignee="alice")
        t2 = kb.create_task(conn, title="ready 2", assignee="bob")
        t3 = kb.create_task(conn, title="review", assignee="alice")
        _set_task_status(conn, t3, "review")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)
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

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="alice")
        _set_task_status(conn, t, "review")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
    assert len(res.spawned) == 1
    assert spawns[0] == t


def _seed_product_review_worktree(
    conn, board: str, repo: Path, *, dirty=False, base_ref="main", tested=True
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
    kanban_db_workspace.set_workspace_path(conn, tid, str(workspace))
    kanban_db_workspace.set_branch_name(conn, tid, branch)
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
    if tested:
        kb._synthesize_ended_run(
            conn, tid, outcome="advanced", step_key="test",
            metadata={"test_branch": branch, "test_head_sha": head_sha,
                      "workflow_outcome": {"verdict": "passed"},
                      "ai_provenance": {"writer": {"agent": "fixture-writer"},
                                        "tester": {"agent": "fixture-tester"}}},
        )
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
    kanban_db_workspace.set_workspace_path(conn, tid, str(workspace))
    kanban_db_workspace.set_branch_name(conn, tid, branch)
    return tid, workspace, branch


def test_dispatch_pins_test_target_before_tester_spawn(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "test-target-pinned"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    observed = []

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, branch = _seed_product_test_worktree(conn, board, repo)

        def capture_spawn(task, launched_workspace, board=None):
            run = kb.get_run(conn, task.current_run_id)
            observed.append((launched_workspace, run.metadata))
            return 5252

        result = kbd.dispatch_once(conn, board=board, spawn_fn=capture_spawn)

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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo
        )

        def capture_spawn(task, launched_workspace, board=None):
            run = kb.get_run(conn, task.current_run_id)
            observed.append((launched_workspace, run.metadata))
            return 4242

        result = kbd.dispatch_once(conn, board=board, spawn_fn=capture_spawn)

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

    with kanban_db_connect.connect(board=board) as conn:
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

        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        predecessor_id = kb.create_task(
            conn,
            title="Completed test gate",
            board=board,
            assignee="tester",
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        kanban_db_workspace.set_branch_name(conn, predecessor_id, "review-candidate")
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
        kanban_db_workspace.set_branch_name(conn, tid, "review-candidate")
        conn.execute(
            "UPDATE tasks SET status='review', current_step_key=? WHERE id=?",
            (current_step_key, tid),
        )
        conn.commit()

        def capture_spawn(task, launched_workspace, board=None):
            run = kb.get_run(conn, task.current_run_id)
            observed.append((launched_workspace, run.step_key, run.metadata))
            return 4242

        result = kbd.dispatch_once(conn, board=board, spawn_fn=capture_spawn)
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

    with kanban_db_connect.connect(board=board) as conn:
        predecessor_id = kb.create_task(
            conn,
            title="Completed Tester gate",
            board=board,
            assignee="tester",
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        kanban_db_workspace.set_branch_name(
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
        kanban_db_workspace.set_branch_name(conn, tid, "review-candidate")
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

        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Read-only inspection",
            board=board,
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo
        )
        result = kbd.dispatch_once(
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


@pytest.mark.parametrize("recovery_signal", ["metadata", "preflight", "preflight_resolved", "profile"])
def test_recovery_runs_never_replace_product_writer_or_test_authority(
    kanban_home, recovery_signal,
):
    def executor(profile, provider):
        return {"profile": profile, "provider": provider, "model": "fixture",
                "effort": "high", "surface": "hermes-primary"}

    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Recovery evidence", assignee="custom-developer")
        writer = executor("custom-developer", "codex")
        kb._synthesize_ended_run(conn, tid, outcome="advanced", step_key="development",
                                 metadata={"executor": writer, "ai_provenance": {"writer": {"agent": "codex"}}})
        recovery = {"executor": executor("custom-recovery", "claude-cli"),
                    "ai_provenance": {"writer": {"agent": "claude-cli"}}}
        if recovery_signal == "metadata":
            recovery["resolver"] = {}  # Presence, not truthiness, is the durable stamp.
        recovery_outcome = recovery_signal if recovery_signal.startswith("preflight") else "advanced"
        recovery_dev = kb._synthesize_ended_run(
            conn, tid, outcome=recovery_outcome, step_key="development", metadata=recovery,
        )
        kb.assign_task(conn, tid, "custom-tester")
        test_metadata = {
            "workflow_outcome": {"verdict": "passed"},
            "ai_provenance": {"tester": {"agent": "codex"}},
            "test_branch": "story/fixture", "test_head_sha": "a" * 40,
        }
        tested_run = kb._synthesize_ended_run(
            conn, tid, outcome="advanced", step_key="test", metadata=test_metadata,
        )
        recovery_test = kb._synthesize_ended_run(
            conn, tid, outcome=recovery_outcome, step_key="test", metadata=recovery,
        )
        if recovery_signal == "profile":
            # Reconstruct historical records predating durable stamps. Modern
            # routing correctly refuses creating such an unstamped Resolver run.
            conn.execute("UPDATE task_runs SET profile='resolver' WHERE id IN (?, ?)",
                         (recovery_dev, recovery_test))
        before = [tuple(row) for row in conn.execute("SELECT * FROM task_runs ORDER BY id")]
        selected_writer = kb._latest_product_step_executor(conn, tid, "development")
        selected_test = kb._latest_test_target(conn, tid)
        records = kb._terminal_run_records(conn, tid)

        assert selected_writer["provider"] == "codex"
        assert kb._latest_product_writer_agent(conn, tid) == "codex"
        assert selected_test == {"test_branch": "story/fixture", "test_head_sha": "a" * 40}
        assert not {recovery_dev, recovery_test} & {record.run_id for record in records}
        passed = kb.latest_test_authority(records, "a" * 40)
        assert passed is not None and passed.run_id == tested_run
        assert passed.writer_provider == passed.tester_provider == "codex"
        assert [tuple(row) for row in conn.execute("SELECT * FROM task_runs ORDER BY id")] == before


@pytest.mark.parametrize("step", ["development", "test", "review"])
@pytest.mark.parametrize("failure", ["identity", "crash", "limit"])
def test_custom_recovery_purpose_survives_failed_claim(
    kanban_home, monkeypatch, step, failure,
):
    """A failed recovery claim must never displace ordinary writer/Test evidence."""
    board = "custom-recovery-failure"
    _v2_product_board(board)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    ordinary_profile = {"development": "developer", "test": "tester", "review": "reviewer"}[step]
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="Recovery purpose", assignee=ordinary_profile,
            workflow_template_id="product", current_step_key=step, board=board,
        )
        writer_id = kb._synthesize_ended_run(
            conn, tid, outcome="advanced", step_key="development",
            metadata={"executor": {"profile": "developer", "provider": "codex", "model": "fixture",
                                   "effort": "high", "surface": "hermes-primary"}},
        )
        pins = {"test_branch": "story/fixture", "test_head_sha": "a" * 40}
        test_id = kb._synthesize_ended_run(
            conn, tid, outcome="advanced", step_key="test",
            metadata={**pins, "workflow_outcome": {"verdict": "passed"},
                      "ai_provenance": {"tester": {"agent": "codex"}}},
        )
        assert kb.block_task(
            conn, tid, board=board, kind="needs_input", reason="Needs diagnosis",
            human_escalation_assignee="custom-recovery",
        )
        history = [tuple(row) for row in conn.execute(
            "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (tid,),
        )]
        at_identity = []

        def identity(task):
            at_identity.append(kb.get_run(conn, task.current_run_id).metadata)
            if failure in {"identity", "limit"}:
                raise kb.WorkerRuntimeIdentityError("fixture identity unavailable")
            return {"profile": task.assignee, "provider": "claude-cli", "model": "fixture",
                    "effort": "high", "surface": "hermes-primary", "source": "dispatcher", "version": 1}

        monkeypatch.setattr(kb, "_resolve_worker_runtime_identity", identity)
        if failure == "crash":
            claimed = (kb.claim_review_task(conn, tid) if step == "review"
                       else kb.claim_task(conn, tid, board=board))
            assert claimed is not None
            kb._stamp_run_executor_identity(conn, claimed)
            kbd._set_worker_pid(conn, tid, 987654321)
            monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
            monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
            monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: ("nonzero_exit", 1))
            assert kbd.detect_crashed_workers(conn) == [tid]
        else:
            kbd.dispatch_once(conn, board=board, failure_limit=1 if failure == "limit" else 5,
                             spawn_fn=lambda *args, **kwargs: pytest.fail("identity failure must prevent spawn"))

        run = max(kb.list_runs(conn, tid, include_active=True), key=lambda item: item.id)
        assert run.outcome == {"identity": "spawn_failed", "crash": "crashed", "limit": "gave_up"}[failure]
        assert at_identity == [{"resolver": {"profile": "custom-recovery"}}]
        assert run.metadata["resolver"] == {"profile": "custom-recovery"}
        assert kb._latest_product_step_executor(conn, tid, "development")["provider"] == "codex"
        assert kb._latest_test_target(conn, tid) == pins
        records = kb._terminal_run_records(conn, tid)
        assert run.id not in {record.run_id for record in records}
        assert kb.latest_test_authority(records, "a" * 40).run_id == test_id
        assert [tuple(row) for row in conn.execute(
            "SELECT * FROM task_runs WHERE task_id=? AND id<=? ORDER BY id", (tid, history[-1][0]),
        )] == history
        assert kb.get_run(conn, writer_id).outcome == "advanced"


@pytest.mark.parametrize("decision", ["resume", "repair", "escalate"])
def test_same_assignee_recovery_purpose_ends_at_canonical_resolution(kanban_home, decision):
    board = "same-assignee-purpose"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(conn, title="Same worker, different purpose", assignee="developer",
                             workflow_template_id="product", current_step_key="development", board=board)
        ordinary = kb.claim_task(conn, tid, board=board)
        assert not (kb.get_run(conn, ordinary.current_run_id).metadata or {}).get("resolver")
        assert kb.block_task(conn, tid, board=board, kind="needs_input", reason="Needs diagnosis",
                             expected_run_id=ordinary.current_run_id, human_escalation_assignee="developer")
        recovery = kb.claim_task(conn, tid, board=board)
        assert kb.get_run(conn, recovery.current_run_id).metadata == {"resolver": {"profile": "developer"}}
        with pytest.raises(ValueError, match="preflight"):
            kb.complete_task(conn, tid, board=board, expected_run_id=recovery.current_run_id, summary="Bypass")
        request = _resolver_request(kb.resolver_expected_snapshot(conn, tid), decision=decision)
        if decision == "repair":
            request["repair"] = {"workflow": {"phase": "development", "assignee": "developer"}}
        assert kb.resolve_product_preflight(conn, tid, board=board, request=request,
                                            resolver_profile="developer", resolver_model="test-model")
        ended = kb.get_run(conn, recovery.current_run_id)
        assert ended.metadata["resolver"] == {"profile": "developer", "model": "test-model"}
        assert not any(e.kind == "dispatcher_metadata_conflict" for e in kb.list_events(conn, tid))
        if decision != "escalate":
            next_run = kb.claim_task(conn, tid, board=board)
            assert "resolver" not in (kb.get_run(conn, next_run.current_run_id).metadata or {})
            assert kb.get_run(conn, recovery.current_run_id) == ended


@pytest.mark.parametrize("preflight", ["absent", "different_assignee", "different_phase"])
def test_unmatched_recovery_preflight_does_not_hide_ordinary_failure(kanban_home, preflight):
    board = "ordinary-purpose"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(conn, title="Ordinary custom worker", assignee="custom-tester",
                             workflow_template_id="product", current_step_key="test", board=board)
        kb._synthesize_ended_run(conn, tid, outcome="advanced", step_key="test", metadata={
            "workflow_outcome": {"verdict": "passed"}, "test_branch": "story/fixture", "test_head_sha": "a" * 40,
        })
        if preflight != "absent":
            assert kb.block_task(conn, tid, board=board, kind="needs_input", reason="Needs diagnosis",
                                 human_escalation_assignee="custom-recovery")
            kb.assign_task(conn, tid, "custom-tester")
            if preflight == "different_phase":
                # Model a stale persisted preflight after a phase change.
                conn.execute("UPDATE tasks SET assignee='custom-recovery', current_step_key='development' WHERE id=?", (tid,))
                conn.commit()
        claimed = kb.claim_task(conn, tid, board=board)
        assert "resolver" not in (kb.get_run(conn, claimed.current_run_id).metadata or {})
        kb._record_spawn_failure(conn, tid, "ordinary failure", failure_limit=5)
        failed = kb.get_run(conn, claimed.current_run_id)
        assert "resolver" not in (failed.metadata or {})
        assert failed.id in {r.run_id for r in kb._terminal_run_records(conn, tid)}
        if preflight != "different_phase":
            assert kb._latest_test_target(conn, tid) == {}


@pytest.mark.parametrize("optimistic_metadata", [False, True])
def test_newer_failed_ordinary_test_invalidates_an_older_pin(kanban_home, optimistic_metadata):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Failed later Test", assignee="tester")
        pins = {"test_branch": "story/fixture", "test_head_sha": "a" * 40}
        kb._synthesize_ended_run(conn, tid, outcome="advanced", step_key="test", metadata={
            **pins, "workflow_outcome": {"verdict": "passed"},
            "ai_provenance": {"writer": {"agent": "codex"}, "tester": {"agent": "codex"}},
        })
        failed_metadata = dict(pins)
        if optimistic_metadata:
            failed_metadata.update({"workflow_outcome": {"verdict": "passed"},
                                    "ai_provenance": {"writer": {"agent": "codex"}, "tester": {"agent": "codex"}}})
        kb._synthesize_ended_run(conn, tid, outcome="crashed", step_key="test", metadata=failed_metadata)
        assert not kb._latest_test_target(conn, tid)
        assert kb.latest_test_authority(kb._terminal_run_records(conn, tid), "a" * 40) is None


@pytest.mark.parametrize("missing_test", [True, False])
def test_product_review_requires_a_successful_applicable_test_pin(
    kanban_home, tmp_path, missing_test,
):
    board = "review-needs-test"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, _, _ = _seed_product_review_worktree(conn, board, repo, tested=False)
        if not missing_test:
            kb._synthesize_ended_run(conn, tid, outcome="advanced", step_key="test",
                                     metadata={"workflow_outcome": {"verdict": "passed"}})
        assert kb.claim_task(conn, tid, board=board) is not None
        with pytest.raises(kb.ReviewTargetPreparationError, match="successful.*Test|Test.*pin"):
            kb._prepare_review_target(conn, tid, workspace, board=board)


def test_terminal_run_records_fill_test_writer_from_preceding_development_executor(
    kanban_home,
):
    first_writer = {
        "profile": "developer",
        "provider": "openrouter",
        "model": "model-a",
        "effort": "high",
        "surface": "hermes-primary",
    }
    later_writer = {
        "profile": "developer",
        "provider": "claude-cli",
        "model": "model-b",
        "effort": "high",
        "surface": "hermes-primary",
    }
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {"tester": {"agent": "openrouter"}},
        "test_branch": "story/example",
        "test_head_sha": "b" * 40,
    }
    explicit_test_metadata = {
        **test_metadata,
        "ai_provenance": {
            "writer": {"agent": "recorded-writer"},
            "tester": {"agent": "openrouter"},
        },
    }

    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title="Chronological test writer")
        kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="development",
            metadata={"executor": first_writer},
        )
        first_test_run_id = kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="test",
            metadata=test_metadata,
        )
        kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="development",
            metadata={"executor": later_writer},
        )
        explicit_test_run_id = kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="test",
            metadata=explicit_test_metadata,
        )

        test_records = {
            record.run_id: record
            for record in kb._terminal_run_records(conn, task_id)
            if record.phase == "test"
        }

    assert test_records[first_test_run_id].writer_provider == "openrouter"
    assert test_records[explicit_test_run_id].writer_provider == "recorded-writer"


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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
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
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
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
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
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
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
        tid, _workspace, _base_sha, _head_sha = _seed_product_review_worktree(
            conn, board, repo, dirty=True
        )
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
        tid, workspace, base_sha, head_sha = _seed_product_review_worktree(
            conn, board, repo, base_ref="develop"
        )

        def capture_spawn(task, launched_workspace, board=None):
            observed.append(kb.get_run(conn, task.current_run_id).metadata)
            return 4242

        result = kbd.dispatch_once(conn, board=board, spawn_fn=capture_spawn)

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

    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="custom non-Claude review",
            assignee="alice",
            workflow_template_id="product",
            current_step_key="review",
        )
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        result = kbd.dispatch_once(
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

    with kanban_db_connect.connect(board=board) as conn:
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

        result = kbd.dispatch_once(
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

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(kb.subprocess, "run", fake_run)

    assert kb._review_git_output(tmp_path, "status") == "ok"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"


def test_has_spawnable_review_true(kanban_home):
    """has_spawnable_review returns True when review tasks exist with real profiles."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="review me", assignee="default")
        _set_task_status(conn, t, "review")
        # default profile should exist in the test env
        assert kbd.has_spawnable_review(conn) is True


def test_has_spawnable_review_false_on_empty(kanban_home):
    """has_spawnable_review returns False when no review tasks exist."""
    with kanban_db_connect.connect() as conn:
        assert kbd.has_spawnable_review(conn) is False


def test_has_spawnable_review_false_when_only_terminal_lanes(
    kanban_home, monkeypatch,
):
    """has_spawnable_review returns False when review tasks are terminal lanes."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        assert kbd.has_spawnable_review(conn) is False


def test_dispatch_review_skips_nonspawnable(kanban_home, monkeypatch):
    """Review tasks with non-existent profiles go to skipped_nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="review", assignee="orion-cc")
        _set_task_status(conn, t, "review")
        res = kbd.dispatch_once(conn, dry_run=True)
    assert t in res.skipped_nonspawnable
    assert not res.spawned


def test_review_status_in_valid_statuses():
    """'review' is a valid task status."""
    assert "review" in kb.VALID_STATUSES


def test_dispatch_review_does_not_claim_ready_tasks(
    kanban_home, all_assignees_spawnable,
):
    """Review dispatch uses claim_review_task, which only claims review tasks."""
    with kanban_db_connect.connect() as conn:
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
        with pytest.raises(kbc.KanbanDbCorruptError) as excinfo:
            kbc.connect(db_path=db_path)
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
    with pytest.raises(kbc.KanbanDbCorruptError) as excinfo2:
        kbc.connect(db_path=db_path)
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
        kbc.connect(db_path=db_path)

    # No .corrupt backup may be produced for a healthy-but-locked DB.
    backups = list(tmp_path.glob("*.corrupt.*"))
    assert backups == [], f"unexpected corrupt backups: {backups}"

    # And once the lock clears, normal access still works.
    monkeypatch.setattr(kb.sqlite3, "connect", real_connect)
    with kbc.connect(db_path=db_path) as conn:
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

    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="first scratch")
        t2 = kb.create_task(conn, title="second scratch")

    # Sentinel must not exist yet on a fresh install.
    assert not kbw._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kbc.connect() as conn:
            kbw._maybe_emit_scratch_tip(conn, t1, "scratch")

    # Sentinel is now set.
    assert kbw._scratch_tip_shown()
    assert kbw._scratch_tip_sentinel_path().exists()

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
    with kbc.connect() as conn:
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
        with kbc.connect() as conn:
            kbw._maybe_emit_scratch_tip(conn, t2, "scratch")
    tip_records2 = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records2 == [], (
        f"Tip should not re-fire after sentinel is set; got "
        f"{[r.getMessage() for r in tip_records2]!r}"
    )
    with kbc.connect() as conn:
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
    with kbc.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA secure_delete").fetchone()
    assert row[0] == 1, f"expected secure_delete=1, got {row[0]}"




def test_connect_sets_synchronous_full(tmp_path):
    """synchronous must be FULL (=2), not NORMAL (=1)."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kanban_db_connect.connect(db_path=db_path) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="reconnect-check")
    # Force re-init path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Second connection: pragmas must still be applied.
    with kanban_db_connect.connect(db_path=db_path) as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA cell_size_check").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2



def test_pragmas_not_accidentally_disabled_by_migrate_path(tmp_path):
    """Migration path must not reset connection pragmas."""
    db_path = tmp_path / "legacy.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    # Initialise with a fresh connect so schema + init run.
    with kanban_db_connect.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="pre-migration-task")
    # Simulate a re-entry through the init/migration path by discarding path cache.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kanban_db_connect.connect(db_path=db_path) as conn:
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

    with kbc.connect() as conn:
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
    from hermes_cli.kanban_db_connect import connect
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
# does NOT close, so `with kbc.connect() as conn:` leaks the FD in
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
    with kbc.connect(db_path=db_path) as conn:
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
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda _pid: ("clean_exit", 0))

    kb.create_board("prod", preset="product")
    with kanban_db_connect.connect(board="prod") as conn:
        tid = _make_running_product_card(conn, _kb, step="architecture")
        _add_handoff_comment(conn, tid)

        kbd.detect_crashed_workers(conn)

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
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda _pid: ("clean_exit", 0))

    kb.create_board("prod", preset="product")
    with kanban_db_connect.connect(board="prod") as conn:
        tid = _make_running_product_card(
            conn, _kb, step="architecture", max_retries=5,
        )
        # deliberately NO handoff comment
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)

    assert task.status == "blocked"
    assert task.current_step_key == "architecture"


def test_product_worker_nonzero_exit_retains_retry_semantics(
    kanban_home, monkeypatch,
):
    """A normal (nonzero) crash keeps the existing isolated-retry semantics."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 1))

    kb.create_board("prod", preset="product")
    with kanban_db_connect.connect(board="prod") as conn:
        tid = _make_running_product_card(conn, _kb, step="development", assignee="developer")
        _add_handoff_comment(conn, tid)  # evidence present, but this is NOT a clean exit
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)

    assert task.current_step_key == "development"
    assert task.status == "ready"


def test_handoff_v2_flag_defaults_off_and_reads_meta(kanban_home):
    import hermes_cli.kanban_db as kb
    import hermes_cli.kanban_db_connect as kanban_db_connect
    import hermes_cli.kanban_db_workspace as kanban_db_workspace
    import shutil as shutil
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect() as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = _kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kbd._set_worker_pid(conn, tid, 12345)
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

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kbd._set_worker_pid(conn, t, 12345)
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = _kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kbd._set_worker_pid(conn, tid, 90001)
        # Past the launch-window grace period so the crash check isn't
        # skipped as "freshly claimed".
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (int(time.time()) - 3600, tid),
        )

        crashed = kbd.detect_crashed_workers(conn)
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

    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="iso", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=? "
            "WHERE id=?",
            (80000, f"{host}:w0", tid),
        )
        conn.commit()

        crashed = kbd.detect_crashed_workers(conn)
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

    with kanban_db_connect.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = _kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kbd._set_worker_pid(conn, tid, os.getpid())

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
        stale = kbd.detect_stale_running(
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

    with kanban_db_connect.connect(board=board) as conn:
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
        kbd._set_worker_pid(conn, tid, 12345)
        old_started = int(time.time()) - 20
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (old_started, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old_started, tid),
        )

        timed_out = kbd.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
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

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None
        kbd._set_worker_pid(conn, tid, 99999)
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

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        result = kbd.dispatch_once(conn, spawn_fn=boom, board=board, failure_limit=5)

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

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="boom", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=boom)
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

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="host:1") is not None

        tripped = kbd._record_task_failure(
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

    with kanban_db_connect.connect(board=board) as conn:
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


def test_complete_task_product_card_without_product_board_metadata_fails_closed(
    kanban_home,
):
    """A product-stamped nonterminal card must never use generic completion
    when its board cannot supply product lifecycle policy.
    """
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Story: require product board metadata",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None

        with pytest.raises(
            kb.ProductWorkflowStateError,
            match="product board metadata",
        ):
            kb.complete_task(
                conn,
                task_id,
                summary="Implementation is ready.",
                board="missing-product-board",
            )

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert task.status == "running"
    assert task.current_step_key == "development"
    blocked = [
        event
        for event in events
        if event.kind == "completion_blocked_product_board_resolution"
    ]
    assert len(blocked) == 1
    assert blocked[0].payload["board"] == "missing-product-board"
    assert blocked[0].payload["connection_board"] == "default"
    assert blocked[0].payload["database_path"].endswith("kanban.db")


def test_complete_task_release_measure_cannot_bypass_release_orchestration(
    kanban_home, monkeypatch,
):
    board = "v2-release-evidence-gate"
    kb.ensure_product_board_defaults(board)
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect() as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect() as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect() as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect() as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")
        result = kb.epic_ready(conn, epic, board=board, verify_fn=lambda eb: False)

    assert result is False


def test_epic_ready_no_children_returns_false_verify_not_called(kanban_home):
    board = "v2-epic-ready-no-children"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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

    with kanban_db_connect.connect(board=board) as conn:
        kanban_db_workspace.set_branch_name(conn, story_id, story_branch)
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
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(board=board) as conn:
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
    with kanban_db_connect.connect(board=board_name) as conn:
        for child in children:
            _set_task_status(conn, child, "done")
    _seed_epic_merged_event(board_name, epic, repo)
    return epic, repo, children


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
    with kanban_db_connect.connect(board=board) as conn:
        story = kb.create_task(
            conn, title="Story: standalone merge-back", board=board,
            branch_name=branch, workspace_kind="worktree", workspace_path=str(repo),
        )
        _set_task_status(conn, story, "done")
    return story, sha


def _set_human_escalation_profile(board: str, profile: str) -> None:
    metadata = kb.read_board_metadata(board)
    metadata.setdefault("product_workflow", {})["human_escalation_profile"] = profile
    kb.board_metadata_path(board).write_text(
        json.dumps(metadata), encoding="utf-8"
    )


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
