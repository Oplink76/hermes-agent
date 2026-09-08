"""Tests for kanban worker/runs read endpoints.

Covers:
  GET /workers/active
  GET /runs/{run_id}
  GET /runs/{run_id}/inspect
  POST /runs/{run_id}/terminate
"""

from __future__ import annotations

import importlib.util
import secrets
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
import hermes_cli.kanban_db_connect as kanban_db_connect


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    mod_name = "hermes_dashboard_plugin_kanban_worker_runs_test"
    # Re-use a cached module if already loaded to avoid duplicate-router issues.
    if mod_name in sys.modules:
        return sys.modules[mod_name].router

    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _insert_run(conn, task_id, *, worker_pid=None, ended_at=None):
    """Insert a task_runs row directly (bypassing claim machinery) and return run_id."""
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at, ended_at) "
        "VALUES (?, 'running', ?, ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time()), ended_at),
    )
    conn.commit()
    return cur.lastrowid


def _expected_snapshot(task_id: str) -> dict:
    with kanban_db_connect.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return {
        f"expected_{field}": value
        for field, value in kb.task_snapshot_from_row(row).items()
    }


# ---------------------------------------------------------------------------
# GET /workers/active
# ---------------------------------------------------------------------------

def test_workers_active_empty_board(client):
    """Board with no running tasks returns an empty workers list."""
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    body = r.json()
    assert body["workers"] == []
    assert body["count"] == 0
    assert "checked_at" in body


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------

def test_get_run_404_unknown_id(client):
    """Non-existent run_id returns 404."""
    r = client.get("/api/plugins/kanban/runs/999999")
    assert r.status_code == 404
    assert "999999" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/inspect
# ---------------------------------------------------------------------------

def test_inspect_run_404(client):
    """Non-existent run_id returns 404."""
    r = client.get("/api/plugins/kanban/runs/888888/inspect")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/terminate
# ---------------------------------------------------------------------------

def _setup_running_task_with_run(conn, *, title, assignee, worker_pid):
    """Create a task in 'running' state with a matching open task_runs row.

    Mirrors what dispatcher_claim does: stamps tasks.status='running',
    tasks.claim_lock, tasks.worker_pid; inserts task_runs row with the
    same claim_lock so reclaim_task's preconditions are satisfied.
    """
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, "
        "claim_expires=?, worker_pid=? WHERE id=?",
        (lock, future, worker_pid, task_id),
    )
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'running', ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time())),
    )
    conn.execute(
        "UPDATE tasks SET current_run_id = ? WHERE id = ?",
        (cur.lastrowid, task_id),
    )
    conn.commit()
    return task_id, cur.lastrowid


def test_terminate_run_404_unknown_id(client):
    """POST to unknown run_id returns 404."""
    r = client.post(
        "/api/plugins/kanban/runs/777777/terminate",
        json={"reason": "test"},
    )
    assert r.status_code == 404
    assert "777777" in r.json()["detail"]


def test_terminate_run_404_unknown_id_without_body(client):
    response = client.post("/api/plugins/kanban/runs/777778/terminate")
    assert response.status_code == 404


def test_terminate_run_409_already_ended(client):
    """POST against a run with ended_at set returns 409."""
    conn = kanban_db_connect.connect()
    try:
        task_id = kb.create_task(conn, title="ended-terminate", assignee="ivy")
        run_id = _insert_run(
            conn, task_id, worker_pid=22222, ended_at=int(time.time()) - 30,
        )
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/runs/{run_id}/terminate",
        json={"reason": "too late", **_expected_snapshot(task_id)},
    )
    assert r.status_code == 409
    assert "already ended" in r.json()["detail"]


def test_terminate_run_ok(client, monkeypatch):
    """Happy path: live run is terminated, signal fn invoked, reason recorded."""
    conn = kanban_db_connect.connect()
    try:
        task_id, run_id = _setup_running_task_with_run(
            conn, title="kill-me", assignee="jane", worker_pid=33333,
        )
    finally:
        conn.close()

    # Capture signal calls so we don't actually SIGTERM a random PID.
    sent = []

    def _fake_terminate(pid, prev_lock, *, signal_fn=None):
        sent.append((pid, prev_lock))
        return {"signal": "SIGTERM", "delivered": True}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _fake_terminate)

    r = client.post(
        f"/api/plugins/kanban/runs/{run_id}/terminate",
        json={"reason": "operator abort", **_expected_snapshot(task_id)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "run_id": run_id, "task_id": task_id}
    assert sent == [(33333, sent[0][1])]
    assert sent[0][1] is not None  # claim_lock was non-null

    # Task is back to ready, claim cleared.
    conn = kanban_db_connect.connect()
    try:
        row = conn.execute(
            "SELECT status, claim_lock, worker_pid FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None


def test_terminate_run_409_task_not_reclaimable(client, monkeypatch):
    """Open run row whose task is no longer claimable returns 409."""
    conn = kanban_db_connect.connect()
    try:
        task_id = kb.create_task(conn, title="ghost-run", assignee="ken")
        # Task left in default 'ready' state with no claim_lock — task_run
        # exists but reclaim_task will refuse because status != 'running'
        # and claim_lock is NULL.
        run_id = _insert_run(conn, task_id, worker_pid=44444)
    finally:
        conn.close()

    # Make sure no signal is ever sent on this code path.
    def _boom(*a, **k):
        raise AssertionError("_terminate_reclaimed_worker should not be called")

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _boom)

    r = client.post(
        f"/api/plugins/kanban/runs/{run_id}/terminate",
        json={"reason": "stale", **_expected_snapshot(task_id)},
    )
    assert r.status_code == 409
    assert "reclaimable" in r.json()["detail"]


def test_terminate_run_accepts_empty_body(client):
    """Empty JSON body (no reason) is still accepted; falls through to 404."""
    r = client.post(
        "/api/plugins/kanban/runs/666666/terminate",
        json={},
    )
    # 404 because run doesn't exist — what we're asserting here is that
    # the endpoint doesn't 422 on a missing 'reason' field.
    assert r.status_code == 404


def test_terminate_existing_run_without_snapshot_is_422(client):
    with kanban_db_connect.connect() as conn:
        task_id, run_id = _setup_running_task_with_run(
            conn, title="snapshot-required", assignee="ivy", worker_pid=55555,
        )
    response = client.post(
        f"/api/plugins/kanban/runs/{run_id}/terminate",
        json={"reason": "missing snapshot"},
    )
    assert response.status_code == 422
