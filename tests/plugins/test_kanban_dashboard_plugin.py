"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake as intake


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
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
    test_client = TestClient(app)
    original_request = test_client.request

    def snapshot(task_id, board=None):
        with kb.connect(board=board) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {
                f"expected_{field}": "" if field in {"status", "title"} else None
                for field in kb.TASK_SNAPSHOT_FIELDS
            }
        return {
            f"expected_{field}": value
            for field, value in kb.task_snapshot_from_row(row).items()
        }

    def request_with_snapshot(method, url, **kwargs):
        parsed = urlparse(str(url))
        path = parsed.path.removeprefix("/api/plugins/kanban")
        query = parse_qs(parsed.query)
        for key, value in (kwargs.get("params") or {}).items():
            query[key] = [str(value)]
        board = query.get("board", [None])[0]
        method = method.upper()
        body = kwargs.get("json")

        if method != "GET" and path == "/tasks/bulk" and isinstance(body, dict):
            body = dict(body)
            body.setdefault(
                "expected_snapshots",
                {task_id: snapshot(task_id, board) for task_id in body.get("ids", [])},
            )
            kwargs["json"] = body
        elif method == "POST" and path == "/links":
            body = dict(body or {})
            task_id = body.get("expected_task_id") or body.get("child_id")
            body.setdefault("expected_task_id", task_id)
            if task_id:
                for key, value in snapshot(task_id, board).items():
                    body.setdefault(key, value)
            kwargs["json"] = body
        elif method == "DELETE" and path == "/links":
            body = dict(body or {})
            task_id = body.get("expected_task_id") or query.get("child_id", [None])[0]
            body.setdefault("expected_task_id", task_id)
            if task_id:
                for key, value in snapshot(task_id, board).items():
                    body.setdefault(key, value)
            kwargs["json"] = body
        elif method == "DELETE" and path.startswith("/attachments/"):
            attachment_id = int(path.rsplit("/", 1)[-1])
            with kb.connect(board=board) as conn:
                attachment = kb.get_attachment(conn, attachment_id)
            task_id = attachment.task_id if attachment else "t_missing"
            kwargs["json"] = {**snapshot(task_id, board), **(body or {})}
        elif method != "GET" and path.startswith("/runs/"):
            run_id = int(path.split("/")[2])
            with kb.connect(board=board) as conn:
                run = kb.get_run(conn, run_id)
            task_id = run.task_id if run else "t_missing"
            kwargs["json"] = {**snapshot(task_id, board), **(body or {})}
        elif method != "GET" and path.startswith("/tasks/"):
            task_id = path.split("/")[2]
            if path.endswith("/attachments"):
                data = dict(kwargs.get("data") or {})
                data.setdefault("expected_snapshot", json.dumps(snapshot(task_id, board)))
                kwargs["data"] = data
            else:
                kwargs["json"] = {**snapshot(task_id, board), **(body or {})}

        return original_request(method, url, **kwargs)

    test_client.request = request_with_snapshot
    return test_client


# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------


def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == kb.VALID_STATUSES - {"archived"}
    for expected in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == []
    assert data["latest_event_id"] == 0


def test_product_board_uses_relay_style_columns_and_step_grouping(client):
    kb.create_board("prod", name="Product", preset="product")
    with kb.connect(board="prod") as conn:
        story_id = kb.create_task(
            conn,
            title="User story: visible quorum state",
            initial_status="running",
            workflow_template_id="product",
            current_step_key="backlog",
        )
        arch_id = kb.create_task(
            conn,
            title="Architecture: quorum state model",
            initial_status="running",
            workflow_template_id="product",
            current_step_key="architecture",
        )

    r = client.get("/api/plugins/kanban/board?board=prod")
    assert r.status_code == 200
    columns = r.json()["columns"]
    assert [c["label"] for c in columns] == [
        "Backlog",
        "Architecture",
        "Development",
        "Test",
        "Review",
        "Release / Measure",
        "Done",
        "Blocked",
    ]
    by_name = {c["name"]: c for c in columns}
    assert [t["id"] for t in by_name["backlog"]["tasks"]] == [story_id]
    assert [t["id"] for t in by_name["architecture"]["tasks"]] == [arch_id]
    assert all(t["id"] != story_id for t in by_name["development"]["tasks"])


def test_product_board_exposes_ai_provenance_on_cards_and_detail(client):
    kb.create_board("prod", name="Product", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: audit trail",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented audit trail",
            metadata={
                "ai_provenance": {
                    "writer": {
                        "agent": "claude-code",
                        "model": "opus-4.8",
                        "toolchain": "claude-code",
                        "branch": "feature/audit-trail",
                    }
                }
            },
            board="prod",
            product_role_assignees={"tester": "tester"},
        )

    board = client.get("/api/plugins/kanban/board?board=prod")
    assert board.status_code == 200
    cards = [task for col in board.json()["columns"] for task in col["tasks"]]
    card = next(task for task in cards if task["id"] == tid)
    assert card["ai_provenance"]["writer_agent"] == "claude-code"
    assert card["ai_provenance"]["branch"] == "feature/audit-trail"
    assert card["ai_provenance"]["by_step"]["development"]["model"] == "opus-4.8"
    assert card["ai_provenance"]["by_step"]["development"]["toolchain"] == "claude-code"

    detail = client.get(f"/api/plugins/kanban/tasks/{tid}?board=prod")
    assert detail.status_code == 200
    task = detail.json()["task"]
    assert task["ai_provenance"]["by_step"]["development"]["writer_agent"] == "claude-code"


def test_product_task_detail_ai_provenance_includes_read_only_evidence(client):
    kb.create_board("prod", name="Product", preset="product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: provenance evidence",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented provenance evidence panel",
            metadata={
                "ai_provenance": {
                    "writer": {
                        "agent": "claude-code",
                        "model": "claude-opus-4.8",
                        "toolchain": "claude-code",
                        "branch": "feature/provenance-evidence",
                        "commit": "abc1234",
                    }
                }
            },
            board="prod",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Verification: pytest tests/plugins/test_kanban_dashboard_plugin.py -q passed",
            metadata={
                "ai_provenance": {
                    "tester": {
                        "agent": "codex",
                        "model": "gpt-5",
                        "toolchain": "codex-cli",
                        "result": "passed",
                    }
                }
            },
            board="prod",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Review: no blocking findings",
            metadata={
                "ai_provenance": {
                    "reviewer": {
                        "agent": "codex-review",
                        "model": "gpt-5",
                        "toolchain": "codex-cli",
                        "verdict": "approved",
                    }
                }
            },
            board="prod",
        )

    detail = client.get(f"/api/plugins/kanban/tasks/{tid}?board=prod")
    assert detail.status_code == 200
    evidence = detail.json()["task"]["ai_provenance"]

    assert evidence["writer_agent"] == "claude-code"
    assert evidence["tester_agent"] == "codex"
    assert evidence["reviewer_agent"] == "codex-review"
    assert evidence["model"] == "gpt-5"
    assert evidence["toolchain"] == "codex-cli"
    assert evidence["branch"] == "feature/provenance-evidence"
    assert evidence["commit"] == "abc1234"
    assert evidence["verification_summary"] == (
        "Verification: pytest tests/plugins/test_kanban_dashboard_plugin.py -q passed"
    )
    assert evidence["by_step"]["development"]["summary"] == (
        "Implemented provenance evidence panel"
    )
    assert evidence["by_step"]["development"]["model"] == "claude-opus-4.8"
    assert evidence["by_step"]["development"]["toolchain"] == "claude-code"
    assert evidence["by_step"]["test"]["verification_summary"] == (
        "Verification: pytest tests/plugins/test_kanban_dashboard_plugin.py -q passed"
    )
    assert evidence["by_step"]["test"]["model"] == "gpt-5"
    assert evidence["by_step"]["test"]["toolchain"] == "codex-cli"
    assert evidence["by_step"]["review"]["summary"] == "Review: no blocking findings"
    assert evidence["by_step"]["review"]["model"] == "gpt-5"
    assert evidence["by_step"]["review"]["toolchain"] == "codex-cli"


def test_approve_unblock_endpoint_validates_snapshot_and_writes_trace(client):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Approve from dashboard",
            body="Preserve body",
            assignee="developer",
            initial_status="blocked",
        )

    response = client.post(
        f"/api/plugins/kanban/tasks/{tid}/approve-unblock",
        json={
            "confirmed": True,
            "expected_status": "blocked",
            "expected_title": "Approve from dashboard",
            "comment_author": "agentic-os-cockpit/developer",
            "comment_source": "Agentic OS Cockpit approve/unblock control",
        },
    )

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "ready"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        comments = kb.list_comments(conn, tid)
    assert task is not None
    assert task.status == "ready"
    assert task.body == "Preserve body"
    assert task.assignee == "developer"
    assert len(comments) == 1
    assert comments[0].author == "agentic-os-cockpit/developer"
    assert "Decision: approved_unblock" in comments[0].body
    assert "Resulting status: ready" in comments[0].body


def test_approve_unblock_endpoint_stale_snapshot_returns_409_without_trace(client):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Current title", initial_status="blocked")

    response = client.post(
        f"/api/plugins/kanban/tasks/{tid}/approve-unblock",
        json={
            "confirmed": True,
            "expected_status": "blocked",
            "expected_title": "Old title",
            "comment_author": "agentic-os-cockpit/developer",
            "comment_source": "Agentic OS Cockpit approve/unblock control",
        },
    )

    assert response.status_code == 409
    assert "refresh" in response.json()["detail"]
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert kb.list_comments(conn, tid) == []


# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------


def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "researcher",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "researcher"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "researcher" in data["assignees"]


def test_board_and_detail_keep_epics_separate_from_dependency_relations(client):
    with kb.connect() as conn:
        epic_id = kb.create_task(
            conn, title="Portfolio outcome", work_item_kind="epic"
        )
        dependency_id = kb.create_task(conn, title="Acceptance dependency")
        member_id = kb.create_task(conn, title="Qualified member")
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=member_id)
        kb.link_tasks(conn, dependency_id, member_id)

    board = client.get("/api/plugins/kanban/board").json()
    column_tasks = [
        task for column in board["columns"] for task in column["tasks"]
    ]
    assert epic_id not in {task["id"] for task in column_tasks}
    assert board["epics"] == [
        {
            "id": epic_id,
            "title": "Portfolio outcome",
            "workItemKind": "epic",
            "progress": {"done": 0, "total": 1, "release_state": "pending"},
        }
    ]
    member = next(task for task in column_tasks if task["id"] == member_id)
    assert member["workItemKind"] == "card"
    assert member["epic"] == {"id": epic_id, "title": "Portfolio outcome"}
    assert member["dependencies"] == [dependency_id]
    assert member["dependents"] == []

    detail = client.get(f"/api/plugins/kanban/tasks/{member_id}").json()
    assert detail["relations"] == {
        "epic": {"id": epic_id, "title": "Portfolio outcome"},
        "dependencies": [dependency_id],
        "dependents": [],
    }

    epic_detail = client.get(f"/api/plugins/kanban/tasks/{epic_id}").json()
    assert epic_detail["task"]["workItemKind"] == "epic"
    assert epic_detail["members"] == [member_id]
    assert epic_detail["progress"] == {
        "done": 0,
        "total": 1,
        "release_state": "pending",
    }


def test_strict_board_post_tasks_returns_intake_without_task(client):
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    response = client.post(
        "/api/plugins/kanban/tasks?board=strict",
        json={
            "title": "dashboard request",
            "assignee": "reviewer",
            "current_step_key": "review",
            "parents": ["t_missing"],
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "qualification_required"
    assert body["intake_status"] == "pending"
    assert body["intake_id"].startswith("qi_")
    assert "task" not in body
    with kb.connect(board="strict") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        record = kb.get_qualification_intake(conn, body["intake_id"])
    assert "dashboard request" in record["raw_request"]


def test_official_intake_api_returns_receipt_filtered_inbox_and_detail(client):
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    response = client.post(
        "/api/plugins/kanban/intake?board=strict",
        json={
            "request": {"title": "Official intake", "body": "Keep intent"},
            "session_id": "cockpit-session",
            "attachments": [{"name": "brief.md"}],
        },
    )

    assert response.status_code == 202, response.text
    receipt = response.json()
    assert receipt["status"] == "qualification_required"
    intake_id = receipt["intake_id"]

    with kb.connect(board="strict") as conn:
        intake.submit_intake(
            conn,
            request={"title": "Migrated intake"},
            source="hermes-migration",
        )
        intake.submit_intake(
            conn,
            request={"title": "Reconciled intake"},
            source="hermes-reconcile",
        )

    inbox = client.get(
        "/api/plugins/kanban/intake?board=strict&status=pending"
    )
    assert inbox.status_code == 200
    assert [item["id"] for item in inbox.json()["items"]] == [intake_id]

    normal = client.get("/api/plugins/kanban/intake?board=strict")
    assert [item["source"] for item in normal.json()["items"]] == ["dashboard-api"]

    migration = client.get(
        "/api/plugins/kanban/intake?board=strict&source=hermes-migration"
    )
    assert migration.json()["count"] == 1
    assert migration.json()["items"][0]["source"] == "hermes-migration"

    reconcile = client.get(
        "/api/plugins/kanban/intake?board=strict&source=hermes-reconcile"
    )
    assert reconcile.json()["count"] == 1
    assert reconcile.json()["items"][0]["source"] == "hermes-reconcile"

    detail = client.get(
        f"/api/plugins/kanban/intake/{intake_id}?board=strict"
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["intake"]["id"] == intake_id
    assert "Official intake" in body["intake"]["raw_request"]
    assert body["decision"] is None
    assert body["contract_summary"] is None
    assert body["materialized_item"] is None
    assert "signature" not in json.dumps(body).lower()
    assert "canonical_json" not in json.dumps(body).lower()
    assert "internal_prompt" not in json.dumps(body).lower()


def test_task_and_epic_detail_expose_safe_work_contract_views(client):
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    secret = b"test-only-secret"

    def contract(request_id, *, kind="card"):
        is_epic = kind == "epic"
        return intake.sign_work_contract(
            {
                "version": 1,
                "policy_version": metadata["qualification"]["policy_version"],
                "qualification_path": "hermes",
                "request_id": request_id,
                "work": {
                    "item_kind": kind,
                    "work_type": "maintenance",
                    "title": "Release outcome" if is_epic else "Governed member",
                    "outcome": "Customers receive the governed outcome",
                    "scope": ["Hermes"],
                    "out_of_scope": ["Unrelated systems"],
                },
                "routing": {
                    "entry_phase": None if is_epic else "backlog",
                    "assignee": None if is_epic else "productowner",
                    "epic_id": None,
                    "dependencies": [],
                },
                "entry_assessment": {
                    "reason": "Explicit governed entry",
                    "skipped_phases": [],
                    "evidence": [],
                },
                "handover": {
                    "deliverables": ["working outcome"],
                    "required_evidence": ["release evidence"],
                    "done_when": ["outcome measured"],
                    "next_phase": None if is_epic else "architecture",
                    "next_role": None if is_epic else "architect",
                },
                "rules": {
                    "allowed": ["scoped implementation"],
                    "forbidden": ["bypass release evidence"],
                },
                "classification": ["framework:maintenance"],
                "issuer": {"profile": "hermes", "run_id": None, "issued_at": 10},
            },
            secret=secret,
        )

    with kb.connect(board="strict") as conn:
        epic_intake = kb.create_qualification_intake(
            conn, raw_request="Epic request", source="hermes"
        )
        epic_id = intake.materialize_contract(
            conn,
            board="strict",
            signed_contract=contract(epic_intake, kind="epic"),
            secret=secret,
        )
        card_intake = kb.create_qualification_intake(
            conn, raw_request="Card request", source="hermes"
        )
        signed_card = contract(card_intake)
        signed_card["contract"]["routing"]["epic_id"] = epic_id
        signed_card = intake.sign_work_contract(signed_card["contract"], secret=secret)
        card_id = intake.materialize_contract(
            conn,
            board="strict",
            signed_contract=signed_card,
            secret=secret,
        )

    intake_detail = client.get(
        f"/api/plugins/kanban/intake/{card_intake}?board=strict"
    ).json()
    assert intake_detail["decision"]["decision"] == "qualified"
    assert intake_detail["contract_summary"]["work"]["title"] == "Governed member"
    assert intake_detail["materialized_item"]["id"] == card_id

    card = client.get(
        f"/api/plugins/kanban/tasks/{card_id}?board=strict"
    ).json()
    assert card["work_contract"]["entry_assessment"]["reason"] == "Explicit governed entry"
    assert card["work_contract"]["handover"]["done_when"] == ["outcome measured"]
    assert card["work_contract"]["rules"]["forbidden"] == ["bypass release evidence"]
    assert card["relations"]["epic"]["id"] == epic_id

    epic = client.get(
        f"/api/plugins/kanban/tasks/{epic_id}?board=strict"
    ).json()
    assert epic["epic_detail"] == {
        "outcome": "Customers receive the governed outcome",
        "scope": ["Hermes"],
        "constraints": ["bypass release evidence"],
        "definition_of_done": ["outcome measured"],
        "members": [card_id],
        "progress": {"done": 0, "total": 1, "release_state": "pending"},
        "release_state": "pending",
    }
    serialized = json.dumps(epic).lower()
    assert "signature" not in serialized
    assert "canonical_json" not in serialized
    assert "signing" not in serialized


def test_strict_board_rejects_client_contract_and_routing_mutations(client):
    kb.ensure_product_board_defaults("strict")
    with kb.connect(board="strict") as conn:
        first = kb.create_task(conn, title="Legacy first", assignee="developer")
        second = kb.create_task(conn, title="Legacy second", assignee="developer")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    forbidden_create = client.post(
        "/api/plugins/kanban/tasks?board=strict",
        json={"title": "bypass", "contract": {"signature": "caller"}},
    )
    assert forbidden_create.status_code == 422

    routing = client.patch(
        f"/api/plugins/kanban/tasks/{first}?board=strict",
        json={"assignee": "reviewer", "current_step_key": "review"},
    )
    assert routing.status_code == 409
    assert "Work Contract" in routing.text

    lifecycle = client.patch(
        f"/api/plugins/kanban/tasks/{first}?board=strict",
        json={"status": "done", "summary": "caller-forged completion"},
    )
    assert lifecycle.status_code == 409
    assert "run-scoped" in lifecycle.text

    bulk_lifecycle = client.post(
        "/api/plugins/kanban/tasks/bulk?board=strict",
        json={
            "ids": [first],
            "status": "done",
            "summary": "bulk-forged completion",
        },
    )
    assert bulk_lifecycle.status_code == 409
    assert "run-scoped" in bulk_lifecycle.text
    with kb.connect(board="strict") as conn:
        assert kb.get_task(conn, first).status != "done"

    deletion = client.delete(
        f"/api/plugins/kanban/tasks/{first}?board=strict",
    )
    assert deletion.status_code == 409
    assert "Work Contract" in deletion.text
    with kb.connect(board="strict") as conn:
        assert kb.get_task(conn, first) is not None

    dependency = client.post(
        "/api/plugins/kanban/links?board=strict",
        json={"parent_id": first, "child_id": second, "expected_task_id": second},
    )
    assert dependency.status_code == 409
    assert "Work Contract" in dependency.text

    comment = client.post(
        f"/api/plugins/kanban/tasks/{first}/comments?board=strict",
        json={"author": "tester", "body": "Evidence remains writable"},
    )
    assert comment.status_code == 200


def test_board_list_recommends_persistent_workspace_for_configured_workdir(
    client, tmp_path
):
    """Board metadata should tell the UI which safe task default to use."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    kb.write_board_metadata("default", default_workdir=str(repo))

    plain_dir = tmp_path / "notes"
    plain_dir.mkdir()
    kb.create_board("notes", default_workdir=str(plain_dir))
    kb.create_board("disposable")

    response = client.get("/api/plugins/kanban/boards")

    assert response.status_code == 200
    boards = {board["slug"]: board for board in response.json()["boards"]}
    assert boards["default"]["default_workspace_kind"] == "worktree"
    assert boards["notes"]["default_workspace_kind"] == "dir"
    assert boards["disposable"]["default_workspace_kind"] == "scratch"


def test_create_board_persists_project_directory(client, tmp_path):
    """The dashboard board form should anchor future tasks to its project."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    response = client.post(
        "/api/plugins/kanban/boards",
        json={
            "slug": "project-board",
            "name": "Project Board",
            "default_workdir": str(project_dir),
        },
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("project-board")["default_workdir"] == str(
        project_dir.resolve()
    )


@pytest.mark.parametrize("path", ["relative/project", "~/missing-project"])
def test_create_board_rejects_invalid_project_directory(client, path):
    """A board must not persist a path that cannot anchor worker output."""
    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "invalid-project", "default_workdir": path},
    )

    assert response.status_code == 400
    assert "project directory" in response.json()["detail"].lower()


def test_new_board_dialog_collects_project_directory():
    """Board creation should expose the setting that controls safe task defaults."""
    bundle = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "dist"
        / "index.js"
    ).read_text(encoding="utf-8")

    assert 'const [projectDirectory, setProjectDirectory] = useState("");' in bundle
    assert "Project directory" in bundle
    assert "Absolute path to the project folder" in bundle
    assert "default_workdir: projectDirectory.trim() || undefined" in bundle


def test_dashboard_workspace_picker_explains_persistence_contract():
    """Task creation must make scratch deletion visible without a hover."""
    bundle = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "dist"
        / "index.js"
    ).read_text(encoding="utf-8")

    assert "Temporary — deleted on completion" in bundle
    assert "Git worktree — preserved" in bundle
    assert "Directory — preserved" in bundle
    assert "defaultWorkspacePath: (props.boardMeta && props.boardMeta.default_workdir) || \"\"" in bundle
    assert (
        "This workspace and any files left in it are deleted when the task completes."
        in bundle
    )


def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])


def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1


def test_board_query_param_default_overrides_current_board_pointer(client):
    """Dashboard ``?board=default`` must win even if the CLI's current-board
    pointer targets a non-default board.

    Regression: selecting the Default board in the dashboard must not fall
    through to whichever board ``hermes kanban boards switch`` last pinned.
    """
    default_task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "default-only"},
    ).json()["task"]

    kb.create_board("other")
    other_conn = kb.connect(board="other")
    try:
        kb.create_task(other_conn, title="other-only")
    finally:
        other_conn.close()

    kb.set_current_board("other")

    current_board = client.get("/api/plugins/kanban/board").json()
    current_ids = {
        task["id"]
        for column in current_board["columns"]
        for task in column["tasks"]
    }
    assert default_task["id"] not in current_ids

    pinned_default = client.get("/api/plugins/kanban/board?board=default").json()
    pinned_ids = {
        task["id"]
        for column in pinned_default["columns"]
        for task in column["tasks"]
    }
    assert pinned_ids == {default_task["id"]}


def test_dashboard_select_filters_use_sdk_value_change_handler():
    """Tenant/assignee filters must work with the dashboard SDK Select API.

    The dashboard Select component is shadcn-like and calls
    ``onValueChange(value)`` instead of native ``onChange(event)``. A native-only
    handler leaves the tenant dropdown visually selectable but never updates the
    filtered board query.
    """

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "function selectChangeHandler(setter)" in js
    assert "onValueChange: function (v)" in js
    assert "onChange: function (e)" in js
    assert "selectChangeHandler(props.setTenantFilter)" in js
    assert "selectChangeHandler(props.setAssigneeFilter)" in js


def test_dashboard_client_side_filtering_includes_tenant_filter():
    """The rendered board must also filter by tenant.

    The API request includes ``?tenant=...``, but the dashboard also filters the
    locally cached board for search/assignee changes. Without checking
    ``tenantFilter`` here, switching tenants can leave stale cards visible until a
    full reload finishes.
    """

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "if (tenantFilter && t.tenant !== tenantFilter) return false;" in js
    assert "[boardData, tenantFilter, assigneeFilter, search]" in js


def test_dashboard_initial_board_uses_backend_current_when_unpinned():
    """Fresh browsers should open the backend current board, not default.

    Explicit dashboard selections are stored in localStorage and should still
    win, but an empty localStorage state must adopt the API's ``current`` board
    so multi-board installs do not look empty on first load.
    """

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert 'useState(() => readSelectedBoard() || null)' in js
    assert "const storedBoard = readSelectedBoard();" in js
    assert "if (!storedBoard && !board && data && data.current)" in js
    assert "setBoard(data.current);" in js
    assert 'readSelectedBoard() || "default"' not in js


def test_dashboard_column_header_prefers_backend_labels_for_product_boards():
    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "props.column.help || getColumnHelp(t, props.column.name)" in js
    assert "props.column.label || getColumnLabel(t, props.column.name)" in js


def test_dashboard_markdown_html_is_sanitized_before_render():
    """Markdown rendering must sanitize HTML before dangerouslySetInnerHTML."""

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "function sanitizeMarkdownHtml(html)" in js
    assert "MARKDOWN_ALLOWED_TAGS" in js
    assert "sanitizeMarkdownHtml(renderMarkdown(props.source || \"\"))" in js
    assert "dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || \"\") }" not in js


# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------


def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]

    # Events exist from creation.
    assert len(data["events"]) >= 1


def test_task_detail_404_on_unknown(client):
    r = client.get("/api/plugins/kanban/tasks/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------


def test_patch_status_complete(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done", "result": "shipped"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "done"

    # Board reflects the move.
    done = next(
        c for c in client.get("/api/plugins/kanban/board").json()["columns"]
        if c["name"] == "done"
    )
    assert any(x["id"] == t["id"] for x in done["tasks"])


def test_patch_block_then_unblock(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "blocked", "block_reason": "need input"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "blocked"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "ready"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "ready"


def test_patch_schedule_then_unblock(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "scheduled", "block_reason": "run tomorrow"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "scheduled"

    columns = client.get("/api/plugins/kanban/board").json()["columns"]
    assert "scheduled" in [c["name"] for c in columns]
    scheduled = next(c for c in columns if c["name"] == "scheduled")
    assert any(x["id"] == t["id"] for x in scheduled["tasks"])

    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "ready"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "ready"


def test_patch_drag_drop_move_todo_to_ready(client):
    """Direct status write: the drag-drop path for statuses without a
    dedicated verb (e.g. manually promoting todo -> ready).

    Promoting a child whose parent is not done is rejected (409).
    Promoting a child whose parent IS done is accepted (200)."""
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    # Rejected: parent not done yet.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{child['id']}",
        json={"status": "ready"},
    )
    assert r.status_code == 409
    assert r.json()["current"]["status"] == "todo"

    # The 409 detail must name the blocking parent so the dashboard can
    # render an actionable toast instead of a silent no-op (#26744).
    detail = r.json()["detail"]
    assert "Cannot move to 'ready'" in detail
    assert parent["id"] in detail
    assert "'p'" in detail
    assert "status=" in detail
    # Whatever non-``done`` status the parent currently has must show up
    # so the operator knows what to fix.
    assert f"status={parent['status']}" in detail
    assert parent["status"] != "done"

    # Complete the parent.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    # Now child auto-promoted by recompute_ready — already ready.
    child_after = client.get(f"/api/plugins/kanban/tasks/{child['id']}").json()["task"]
    assert child_after["status"] == "ready"


def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_reopen["status"] == "todo"


def test_patch_reassign(client):
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "x", "assignee": "a"},
    ).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"assignee": "b"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["assignee"] == "b"


def test_patch_priority_and_edit(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"priority": 5, "title": "renamed"},
    )
    assert r.status_code == 200
    data = r.json()["task"]
    assert data["priority"] == 5
    assert data["title"] == "renamed"


def test_patch_invalid_status(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "banana"},
    )
    assert r.status_code == 400


def test_patch_status_running_rejected(client):
    """Dashboard PATCH cannot transition a task directly to 'running'.

    The only legitimate path into 'running' is through the dispatcher's
    ``claim_task`` — which atomically creates a ``task_runs`` row,
    claim_lock, expiry, and worker-PID metadata. Allowing a direct set
    creates orphaned 'running' tasks with no run row or claim, which
    violate the board's run-history invariants. See issue #19535.
    """
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "running"},
    )
    assert r.status_code == 400
    assert "running" in r.json()["detail"]
    # Task's status should still be its pre-request value — the direct-set
    # was rejected before any mutation.
    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"]
        for col in board["columns"]
        for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404


def test_delete_task_not_found(client):
    r = client.delete("/api/plugins/kanban/tasks/t_nonexistent")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------


def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"


def test_add_comment_empty_rejected(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "   "},
    )
    assert r.status_code == 400


def test_add_link_and_delete_link(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/links",
        json={"parent_id": a["id"], "child_id": b["id"]},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{b['id']}")
    assert a["id"] in r.json()["links"]["parents"]

    r = client.delete(
        "/api/plugins/kanban/links",
        params={"parent_id": a["id"], "child_id": b["id"]},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_add_link_cycle_rejected(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    client.post(
        "/api/plugins/kanban/links",
        json={"parent_id": a["id"], "child_id": b["id"]},
    )
    r = client.post(
        "/api/plugins/kanban/links",
        json={"parent_id": b["id"], "child_id": a["id"]},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------


def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------


def test_create_triage_lands_in_triage_column(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "rough idea, spec me", "triage": True},
    )
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["status"] == "triage"

    r = client.get("/api/plugins/kanban/board")
    triage = next(c for c in r.json()["columns"] if c["name"] == "triage")
    assert len(triage["tasks"]) == 1
    assert triage["tasks"][0]["title"] == "rough idea, spec me"


def test_triage_task_not_promoted_to_ready(client):
    """Triage tasks must stay in triage even when they have no parents."""
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "must stay put", "triage": True},
    )
    # Run the dispatcher — it should NOT promote the triage task.
    client.post("/api/plugins/kanban/dispatch?dry_run=false&max=4")
    r = client.get("/api/plugins/kanban/board")
    triage = next(c for c in r.json()["columns"] if c["name"] == "triage")
    ready = next(c for c in r.json()["columns"] if c["name"] == "ready")
    assert len(triage["tasks"]) == 1
    assert len(ready["tasks"]) == 0


def test_patch_status_triage_works(client):
    """A user (or specifier) can push a task back into triage, and out of it."""
    t = client.post(
        "/api/plugins/kanban/tasks", json={"title": "x"},
    ).json()["task"]
    # Normal creation is 'ready'; push to triage.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}", json={"status": "triage"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "triage"

    # Now promote to todo.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}", json={"status": "todo"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "todo"


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


def test_board_progress_rollup(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child_a = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "a", "parents": [parent["id"]]},
    ).json()["task"]
    child_b = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "b", "parents": [parent["id"]]},
    ).json()["task"]
    # Children start as "todo" because the parent isn't done yet.  Set the
    # parent to done so children auto-promote to ready via recompute_ready.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200
    # Verify children are now ready.
    for cid in (child_a["id"], child_b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{cid}").json()["task"]
        assert t["status"] == "ready", f"{cid} should be ready after parent done"

    # 0/2 done.
    r = client.get("/api/plugins/kanban/board")
    parent_row = next(
        t for col in r.json()["columns"] for t in col["tasks"]
        if t["id"] == parent["id"]
    )
    assert parent_row["progress"] == {"done": 0, "total": 2}

    # Complete one child. 1/2.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{child_a['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200
    r = client.get("/api/plugins/kanban/board")
    parent_row = next(
        t for col in r.json()["columns"] for t in col["tasks"]
        if t["id"] == parent["id"]
    )
    assert parent_row["progress"] == {"done": 1, "total": 2}

    # Childless tasks report progress=None, not {0/0}.
    assert next(
        t for col in r.json()["columns"] for t in col["tasks"]
        if t["id"] == child_b["id"]
    )["progress"] is None


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


def test_board_auto_initializes_missing_db(tmp_path, monkeypatch):
    """If kanban.db doesn't exist yet, GET /board must create it, not 500."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Deliberately DO NOT call kb.init_db().

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)
    r = c.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    assert (home / "kanban.db").exists(), "init_db wasn't invoked by /board"


# ---------------------------------------------------------------------------
# WebSocket auth (query-param token)
# ---------------------------------------------------------------------------


def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    # No token → policy violation close.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    # Wrong token → policy violation close.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    # Correct token → accepted (connect then close cleanly from our side).
    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz"
    ) as ws:
        assert ws is not None  # handshake succeeded


def test_ws_events_accepts_gated_ticket(tmp_path, monkeypatch):
    """Gated OAuth mode: the WS must accept a single-use ?ticket= (and reject
    a bare ?token=, even one matching _SESSION_TOKEN). This is the regression
    for the hosted-dashboard bug where the kanban live-events WS 1008'd on
    every gated deployment because its bespoke check only knew _SESSION_TOKEN.
    We stub _ws_auth_ok with the real gated semantics (ticket-only)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        # Gated mode: only a known ticket is accepted; token path rejected.
        return ws.query_params.get("ticket", "") == "good-ticket"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    from starlette.websockets import WebSocketDisconnect

    # Legacy token is rejected in gated mode, even if it's the real one.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=secret-xyz"):
            pass
    assert exc.value.code == 1008

    # A valid ticket is accepted.
    with c.websocket_connect(
        "/api/plugins/kanban/events?ticket=good-ticket"
    ) as ws:
        assert ws is not None


def test_ws_events_board_query_param_default_overrides_current_board_pointer(tmp_path, monkeypatch):
    """The event stream must honor ``board=default`` even when the global
    current-board pointer targets a different board.

    This is the live-update half of the dashboard regression: after the UI
    selects Default, the websocket must not subscribe to the CLI's current
    non-default board.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    default_conn = kb.connect()
    try:
        default_task = kb.create_task(default_conn, title="default-live")
    finally:
        default_conn.close()

    kb.create_board("other")
    other_conn = kb.connect(board="other")
    try:
        other_task = kb.create_task(other_conn, title="other-live")
    finally:
        other_conn.close()

    kb.set_current_board("other")

    import hermes_cli
    import types

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=lambda ws: ws.query_params.get("token", "") == "secret-xyz",
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz&board=default&since=0"
    ) as ws:
        payload = ws.receive_json()

    task_ids = {event["task_id"] for event in payload["events"]}
    assert default_task in task_ids
    assert other_task not in task_ids


def test_ws_events_swallows_cancellation_on_shutdown(tmp_path, monkeypatch):
    """``asyncio.CancelledError`` while sleeping in the poll loop is the
    normal uvicorn-shutdown path (``BaseException``, so the bare
    ``except Exception:`` does NOT catch it). Without the explicit
    clause the cancellation surfaces as an application traceback.

    Regression test for #20790 (fix in #20938). Drives the coroutine
    directly (rather than through FastAPI TestClient) so we can observe
    the cancellation outcome deterministically.
    """
    import asyncio

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Short-circuit the auth check — this test is about the cancellation
    # path, not auth.
    import plugins.kanban.dashboard.plugin_api as pa
    monkeypatch.setattr(pa, "_ws_upgrade_authorized", lambda ws: True)

    class _FakeWS:
        def __init__(self):
            self.query_params = {"token": "x", "since": "0"}
            self.accepted = False
            self.closed = False

        async def accept(self):
            self.accepted = True

        async def send_json(self, data):
            pass

        async def close(self, code=None):
            self.closed = True

    async def _run():
        ws = _FakeWS()
        task = asyncio.create_task(pa.stream_events(ws))
        # Give the handler a tick to accept + start polling.
        await asyncio.sleep(0.05)
        assert ws.accepted is True
        task.cancel()
        # stream_events should swallow CancelledError and return cleanly.
        # If it doesn't, this await re-raises the CancelledError.
        result = await task
        return result, ws

    result, ws = asyncio.run(_run())
    assert result is None, (
        f"stream_events should return cleanly after cancellation, got {result!r}"
    )
    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(f"/api/plugins/kanban/tasks/{tid}",
                     json={"status": "blocked", "block_reason": "wait"})

    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert all(r["ok"] for r in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {t["id"] for t in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


def test_bulk_status_done_forwards_completion_summary(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [a["id"], b["id"]],
            "status": "done",
            "result": "DECIDED: ship it",
            "summary": "DECIDED: ship it",
            "metadata": {"source": "dashboard"},
        },
    )

    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    conn = kb.connect()
    try:
        for tid in (a["id"], b["id"]):
            task = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            assert task.status == "done"
            assert task.result == "DECIDED: ship it"
            assert run.summary == "DECIDED: ship it"
            assert run.metadata == {"source": "dashboard"}
    finally:
        conn.close()


def test_bulk_status_running_rejected(client):
    """Bulk updates must match single-task PATCH: direct 'running' is invalid."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [t["id"]], "status": "running"},
    )

    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["id"] == t["id"]
    assert results[0]["ok"] is False
    assert "running" in results[0]["error"]

    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"]
        for col in board["columns"]
        for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"


def test_dashboard_done_actions_prompt_for_completion_summary():
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert "withCompletionSummary" in bundle
    assert "Completion summary" in bundle
    assert "result: summary" in bundle


def test_dashboard_client_mutation_request_contract():
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(repo_root / "tests" / "plugins" / "kanban_dashboard_client_contract.js"),
            str(repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_ai_provenance_detail_section_lists_evidence_fields():
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    assert "function roleStepModelToolchainRows" in bundle
    assert "Development model / toolchain" in bundle
    assert "Test model / toolchain" in bundle
    assert "Review model / toolchain" in bundle
    assert "Branch / commit" in bundle
    assert "Verification summary" in bundle
    assert "evidenceByStep" in bundle


def test_dashboard_surfaces_ready_blocked_error_inline():
    """Regression for #26744: failed status transitions must be surfaced
    inline, not swallowed.  The drag/drop banner and the drawer's action
    row each render the parsed API ``detail`` so operators see *why*
    their click did nothing.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    # Helper that strips ``"409: {\"detail\":\"…\"}"`` down to the
    # human-readable message before it lands in any banner.
    assert "function parseApiErrorMessage(err)" in bundle
    assert "parsed.detail" in bundle

    # Drag/drop banner now uses the parsed message instead of raw
    # ``err.message`` so it no longer leaks HTTP plumbing.
    assert "setError(tx(t, \"moveFailed\", \"Move failed: \") + parseApiErrorMessage(err))" in bundle

    # Drawer action row has its own visible error surface and clears it
    # on success/refresh so stale failures don't follow the operator
    # around.
    assert "const [patchErr, setPatchErr] = useState(null);" in bundle
    assert "setPatchErr(parseApiErrorMessage(e))" in bundle
    assert "setPatchErr(null)" in bundle


def test_dashboard_dependency_selects_use_value_change_handler():
    """Regression for the dependency selects in the task drawer: the
    add-parent / add-child dropdowns must wire through the shared
    selectChangeHandler helper so their value actually lands on the
    underlying React state. Salvaged from #20019 @LeonSGP43.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    parent_select = (
        'value: newParent,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewParent))'
    )
    child_select = (
        'value: newChild,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewChild))'
    )

    assert parent_select in bundle
    assert child_select in bundle


def test_bulk_archive(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "archive": True})
    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    # Default board (archived hidden) — both gone.
    board = client.get("/api/plugins/kanban/board").json()
    ids = {t["id"] for col in board["columns"] for t in col["tasks"]}
    assert a["id"] not in ids
    assert b["id"] not in ids


def test_bulk_reassign(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "old"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks",
                    json={"title": "b", "assignee": "old"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "assignee": "new"})
    assert r.status_code == 200
    for tid in (a["id"], b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["assignee"] == "new"


def test_bulk_unassign_via_empty_string(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "x"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"]], "assignee": ""})
    assert r.status_code == 200
    t = client.get(f"/api/plugins/kanban/tasks/{a['id']}").json()["task"]
    assert t["assignee"] is None


def test_bulk_partial_failure_doesnt_abort_siblings(client):
    """One bad id in the middle of a batch must not prevent others from
    applying."""
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], "bogus-id", c2["id"]], "priority": 7})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    ok_ids = {r["id"] for r in results if r["ok"]}
    assert a["id"] in ok_ids
    assert c2["id"] in ok_ids
    assert any(not r["ok"] and r["id"] == "bogus-id" for r in results)
    # Good siblings actually got the priority bump.
    for tid in (a["id"], c2["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["priority"] == 7


def test_bulk_empty_ids_400(client):
    r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


def test_config_returns_defaults_when_section_missing(client):
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    # Defaults when dashboard.kanban is missing.
    assert data["default_tenant"] == ""
    assert data["lane_by_profile"] is True
    assert data["include_archived_by_default"] is False
    assert data["render_markdown"] is True


def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False


# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------

def test_task_detail_includes_runs(client):
    """GET /tasks/:id carries a runs[] array with the attempt history."""
    r = client.post("/api/plugins/kanban/tasks",
                    json={"title": "port x", "assignee": "worker"}).json()
    tid = r["task"]["id"]

    # Drive status running to force a run creation: PATCH to running
    # doesn't call claim_task (the PATCH path uses _set_status_direct),
    # so use the bulk/claim indirection via the kernel.
    import hermes_cli.kanban_db as _kb
    conn = _kb.connect()
    try:
        _kb.claim_task(conn, tid)
        _kb.complete_task(
            conn, tid,
            result="done",
            summary="tested on rate limiter",
            metadata={"changed_files": ["limiter.py"]},
        )
    finally:
        conn.close()

    d = client.get(f"/api/plugins/kanban/tasks/{tid}").json()
    assert "runs" in d
    assert len(d["runs"]) == 1
    run = d["runs"][0]
    assert run["outcome"] == "completed"
    assert run["profile"] == "worker"
    assert run["summary"] == "tested on rate limiter"
    assert run["metadata"] == {"changed_files": ["limiter.py"]}
    assert run["ended_at"] is not None


def test_task_detail_runs_empty_before_claim(client):
    """A task that's never been claimed has an empty runs[] list, not
    a missing key."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "fresh"}).json()
    d = client.get(f"/api/plugins/kanban/tasks/{r['task']['id']}").json()
    assert d["runs"] == []


def test_patch_status_done_with_summary_and_metadata(client):
    """PATCH /tasks/:id with status=done + summary + metadata must
    reach complete_task, so the dashboard has CLI parity."""
    # Create + claim.
    r = client.post("/api/plugins/kanban/tasks", json={"title": "x", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
    finally:
        conn.close()

    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={
            "status": "done",
            "summary": "shipped the thing",
            "metadata": {"changed_files": ["a.py", "b.py"], "tests_run": 7},
        },
    )
    assert r.status_code == 200, r.text

    # The run must have the summary + metadata attached.
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, tid)
        assert run.outcome == "completed"
        assert run.summary == "shipped the thing"
        assert run.metadata == {"changed_files": ["a.py", "b.py"], "tests_run": 7}
    finally:
        conn.close()


def test_patch_status_done_without_summary_still_works(client):
    """Back-compat: PATCH without the new fields still completes."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "y", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"status": "done", "result": "legacy shape"},
    )
    assert r.status_code == 200, r.text
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, tid)
        assert run.outcome == "completed"
        assert run.summary == "legacy shape"  # falls back to result
    finally:
        conn.close()


def test_patch_status_archive_closes_running_run(client):
    """PATCH to archived while running must close the in-flight run."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "z", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        open_run = kb.latest_run(conn, tid)
        assert open_run.ended_at is None
    finally:
        conn.close()
    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}",
        json={"status": "archived"},
    )
    assert r.status_code == 200, r.text
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "archived"
        assert task.current_run_id is None
        assert kb.latest_run(conn, tid).outcome == "reclaimed"
    finally:
        conn.close()


def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id



# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------

def test_create_task_with_skills_roundtrips(client):
    """POST /tasks accepts `skills: [...]`, GET /tasks/:id returns it."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "translate docs",
            "assignee": "linguist",
            "skills": ["translation", "github-code-review"],
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["skills"] == ["translation", "github-code-review"]

    # Fetch via GET /tasks/:id as the drawer does.
    got = client.get(f"/api/plugins/kanban/tasks/{task['id']}").json()
    assert got["task"]["skills"] == ["translation", "github-code-review"]


def test_create_task_without_skills_defaults_to_empty_list(client):
    """_task_dict serializes Task.skills=None as [] so the drawer can
    always .length check without guarding against null."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "no skills", "assignee": "x"},
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    # Task.skills is None in-memory; _task_dict serializes via
    # dataclasses.asdict which keeps it None. The drawer's
    # `t.skills && t.skills.length > 0` guard handles both null and [].
    assert task.get("skills") in (None, [])


def test_create_task_with_toolset_name_in_skills_is_rejected(client):
    """POST /tasks fails fast when callers confuse toolsets with skills."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "bad skills payload",
            "assignee": "linguist",
            "skills": ["web"],
        },
    )
    assert r.status_code == 400, r.text
    assert "toolset name" in r.json()["detail"]



# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------

def test_create_task_includes_warning_when_no_dispatcher(client, monkeypatch):
    """ready+assigned task + no gateway -> response has `warning` field
    so the dashboard UI can surface a banner."""
    # Force the dispatcher probe to report "not running".
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence",
        lambda: (False, "No gateway is running — start `hermes gateway start`."),
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "warn-me", "assignee": "worker"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("warning")
    assert "gateway" in data["warning"].lower()


def test_create_task_no_warning_when_dispatcher_up(client, monkeypatch):
    """Dispatcher running -> no `warning` field in the response."""
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence",
        lambda: (True, ""),
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "silent", "assignee": "worker"},
    )
    assert r.status_code == 200
    assert "warning" not in r.json() or not r.json()["warning"]


def test_create_task_no_warning_on_triage(client, monkeypatch):
    """Triage tasks never get the warning (they can't be dispatched
    anyway until promoted)."""
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence",
        lambda: (False, "oh no"),
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "triage-task", "assignee": "worker", "triage": True},
    )
    assert r.status_code == 200
    assert "warning" not in r.json() or not r.json()["warning"]


# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------


_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}


def test_board_endpoint_survives_task_age_exception(client, monkeypatch):
    """If task_age raises for any reason, GET /board must NOT 500.

    Pre-fix behavior (without the try/except in _task_dict): a single corrupt
    row turned the entire board response into a 500. The fallback dict lets
    the dashboard render every other card normally.
    """
    create = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "doomed", "assignee": "alice"},
    )
    assert create.status_code == 200, create.text

    # Force task_age to raise an exception type _safe_int does NOT handle —
    # simulates a future regression where someone re-introduces an unguarded
    # operation in task_age. ValueError on '%s' would be absorbed by _safe_int
    # and never reach the outer try/except, so it would not exercise the
    # contract this test pins.
    def _boom(_task):
        raise RuntimeError("simulated future task_age bug")
    monkeypatch.setattr("hermes_cli.kanban_db.task_age", _boom)

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text

    payload = r.json()
    # /board returns columns as a list of {name, tasks} — not a dict — so
    # flatten across all columns to find our seeded task.
    tasks = [t for col in payload["columns"] for t in col["tasks"]]
    assert len(tasks) == 1, f"expected exactly the seeded task, got {tasks!r}"
    # Strict equality: the literal fallback dict from plugin_api._task_dict
    # is the published contract the dashboard UI relies on. Key renames or
    # silent additions should fail this test on purpose.
    assert tasks[0]["age"] == _FALLBACK_AGE


def test_single_task_endpoint_survives_task_age_exception(client, monkeypatch):
    """GET /tasks/:id also calls _task_dict — same fallback should kick in.

    This is the "drawer view" path: the user clicks one card and we serialize
    just that task. A corrupt timestamp on a single task should not block the
    user from opening its drawer.
    """
    create = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "drawer-target", "assignee": "bob"},
    )
    task_id = create.json()["task"]["id"]

    def _boom(_task):
        raise RuntimeError("simulated future task_age bug")
    monkeypatch.setattr("hermes_cli.kanban_db.task_age", _boom)

    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200, r.text
    assert r.json()["task"]["age"] == _FALLBACK_AGE


def test_create_task_probe_error_does_not_break_create(client, monkeypatch):
    """Probe failure must never break task creation."""
    def _raise():
        raise RuntimeError("probe crashed")
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence", _raise,
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "resilient", "assignee": "worker"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["title"] == "resilient"



# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


def test_home_channels_no_task_id_all_unsubscribed(client, with_home_channels):
    """Without task_id, every entry's subscribed=false (UI "no task" state)."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    assert all(not h["subscribed"] for h in r.json()["home_channels"])


def test_home_subscribe_creates_notify_sub_row(client, with_home_channels):
    """POST .../home-subscribe/telegram writes a kanban_notify_subs row
    keyed to the telegram home's (chat_id, thread_id)."""
    from hermes_cli import kanban_db as kb
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    r = client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, t["id"])
    finally:
        conn.close()
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "1234567"
    assert subs[0]["thread_id"] == "42"
    assert subs[0]["notifier_profile"] == "default"


def test_home_subscribe_flips_subscribed_flag_in_subsequent_get(client, with_home_channels):
    """After subscribe, the GET endpoint reports subscribed=true for that
    platform and false for the others."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")

    r = client.get(f"/api/plugins/kanban/home-channels?task_id={t['id']}")
    flags = {h["platform"]: h["subscribed"] for h in r.json()["home_channels"]}
    assert flags == {"telegram": True, "discord": False}


def test_home_subscribe_is_idempotent(client, with_home_channels):
    """Re-subscribing keeps a single row at the DB layer."""
    from hermes_cli import kanban_db as kb
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, t["id"])) == 1
    finally:
        conn.close()


def test_home_subscribe_backfills_owner_on_legacy_row(client, with_home_channels):
    """Re-subscribing should backfill notifier ownership on ownerless rows."""
    from hermes_cli import kanban_db as kb
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    conn = kb.connect()
    try:
        kb.add_notify_sub(
            conn,
            task_id=t["id"],
            platform="telegram",
            chat_id="1234567",
            thread_id="42",
        )
    finally:
        conn.close()

    r = client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    assert r.status_code == 200

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, t["id"])
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["notifier_profile"] == "default"


def test_home_subscribe_unknown_platform_returns_404(client, with_home_channels):
    """Platforms without a home configured (slack in the fixture) return 404."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/slack")
    assert r.status_code == 404
    assert "slack" in r.json()["detail"]


def test_home_subscribe_unknown_task_returns_404(client, with_home_channels):
    r = client.post("/api/plugins/kanban/tasks/t_nonexistent/home-subscribe/telegram")
    assert r.status_code == 404


def test_home_unsubscribe_removes_notify_sub_row(client, with_home_channels):
    """DELETE .../home-subscribe/telegram removes the matching row."""
    from hermes_cli import kanban_db as kb
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    assert r.status_code == 200

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, t["id"]) == []
    finally:
        conn.close()


def test_home_subscribe_multiple_platforms_independent(client, with_home_channels):
    """Subscribing on telegram does not affect discord and vice versa."""
    from hermes_cli import kanban_db as kb
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/discord")

    conn = kb.connect()
    try:
        subs = {s["platform"]: s for s in kb.list_notify_subs(conn, t["id"])}
    finally:
        conn.close()
    assert set(subs) == {"telegram", "discord"}

    # Unsubscribe telegram only.
    client.delete(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/telegram")
    conn = kb.connect()
    try:
        subs = {s["platform"]: s for s in kb.list_notify_subs(conn, t["id"])}
    finally:
        conn.close()
    assert set(subs) == {"discord"}


def test_home_subscribe_rejects_stale_snapshot_without_subscription(
    client,
    with_home_channels,
):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Stale subscription target"},
    ).json()["task"]["id"]
    expected = _expected_operator_snapshot(task_id)
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title = 'Current subscription target' WHERE id = ?",
                (task_id,),
            )

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram",
        json=expected,
    )

    assert response.status_code == 409, response.text
    assert response.json()["current"]["title"] == "Current subscription target"
    with kb.connect() as conn:
        assert kb.list_notify_subs(conn, task_id) == []


def test_home_channels_empty_when_no_homes_configured(client, monkeypatch):
    """Zero platforms with a home -> empty list (UI hides the section)."""
    # No BOT_TOKEN env vars set → load_gateway_config().platforms is empty.
    for var in [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL",
        "DISCORD_BOT_TOKEN", "DISCORD_HOME_CHANNEL",
        "SLACK_BOT_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    assert r.json()["home_channels"] == []


# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------

def test_board_surfaces_warnings_field_for_hallucinated_completions(client):
    """Tasks with a pending completion_blocked_hallucination event surface
    a ``warnings`` object on the /board payload so the UI can badge
    them without fetching per-task events. The warnings summary is
    keyed by diagnostic kind (``hallucinated_cards``) rather than the
    raw event kind — see hermes_cli.kanban_diagnostics for the rule
    that produces it.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")

        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="claimed phantom",
                created_cards=[real, "t_deadbeefcafe"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    tasks = [t for col in data["columns"] for t in col["tasks"]]
    parent_dict = next(t for t in tasks if t["title"] == "parent")
    assert parent_dict.get("warnings") is not None
    w = parent_dict["warnings"]
    assert w["count"] >= 1
    assert "hallucinated_cards" in w["kinds"]
    assert w["highest_severity"] == "error"
    # Full diagnostic list also on the payload for drawer rendering.
    assert parent_dict.get("diagnostics") is not None
    assert parent_dict["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert "t_deadbeefcafe" in parent_dict["diagnostics"][0]["data"]["phantom_ids"]


def test_board_warnings_cleared_after_clean_completion(client):
    """A completed or edited event after a hallucination event clears
    the warning badge — we don't mark tasks permanently."""
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")

        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="first attempt phantom",
                created_cards=[real, "t_phantom11"],
            )

        # Second attempt drops the bad id — succeeds.
        ok = kb.complete_task(
            conn, parent,
            summary="retry without phantom",
            created_cards=[real],
        )
        assert ok is True
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board", params={"include_archived": True})
    assert r.status_code == 200
    data = r.json()
    tasks = [t for col in data["columns"] for t in col["tasks"]]
    parent_dict = next(t for t in tasks if t["title"] == "parent")
    # The clean completion wiped the warning.
    assert parent_dict.get("warnings") is None


def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()


def test_reclaim_endpoint_409_for_non_running_task(client):
    """Reclaiming a task that's already ready returns 409."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="ready", assignee="x")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={},
    )
    assert r.status_code == 409
    assert r.json()["current"]["status"] == "ready"


def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()


def test_reassign_endpoint_409_on_running_without_reclaim(client):
    """Reassigning a running task without reclaim_first returns 409."""
    import secrets
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="orig")
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=? WHERE id=?",
            (secrets.token_hex(4), t),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "new", "reclaim_first": False},
    )
    assert r.status_code == 409


def test_reassign_endpoint_with_reclaim_first_succeeds_on_running(client):
    """With reclaim_first=true, a running task is reclaimed+reassigned in
    one call."""
    import secrets
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="orig")
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 1234, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, int(time.time()) + 3600, 1234, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "new", "reclaim_first": True, "reason": "switch"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "new"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["assignee"] == "new"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------

def test_diagnostics_endpoint_empty_for_clean_board(client):
    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["diagnostics"] == []


def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent, summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]


def test_diagnostics_endpoint_severity_filter(client):
    """Severity filter is at-or-above: warning includes warning+error+critical,
    error includes error+critical, critical is exact (no higher level)."""
    conn = kb.connect()
    try:
        # A warning-severity diagnostic (prose phantom) on one task.
        # Phantom id must be valid hex — the prose scanner regex
        # requires ``t_[a-f0-9]{8,}``.
        p1 = kb.create_task(conn, title="prose", assignee="a")
        kb.complete_task(conn, p1, summary="mentioned t_deadbeef1234")
        # An error-severity diagnostic (spawn failures) on another.
        # Keep this below critical severity (failure_threshold * 2).
        p2 = kb.create_task(conn, title="spawn", assignee="b")
        conn.execute(
            "UPDATE tasks SET consecutive_failures=2, last_failure_error='x' WHERE id=?",
            (p2,),
        )
        conn.commit()
    finally:
        conn.close()

    # warning filter is at-or-above → both the warning AND the error pass.
    r = client.get("/api/plugins/kanban/diagnostics?severity=warning")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    task_ids = {row["task_id"] for row in data["diagnostics"]}
    assert task_ids == {p1, p2}

    # error filter is at-or-above → only the error passes (warning is below).
    r = client.get("/api/plugins/kanban/diagnostics?severity=error")
    data = r.json()
    assert data["count"] == 1
    assert data["diagnostics"][0]["task_id"] == p2


def test_board_exposes_diagnostics_list_and_summary(client):
    """/board should attach both the full diagnostics list AND the
    compact warnings summary (with highest_severity) on each task
    that has any diagnostic.
    """
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="crashy", assignee="worker")
        # Simulate 2 consecutive crashes -> repeated_crashes error diag
        for i in range(2):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, outcome, started_at, "
                "ended_at, error) VALUES (?, 'crashed', 'crashed', ?, ?, ?)",
                (t, int(time.time()) - 100, int(time.time()) - 50, "OOM"),
            )
        conn.commit()
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    data = r.json()
    tasks = [x for col in data["columns"] for x in col["tasks"]]
    task_dict = next(x for x in tasks if x["title"] == "crashy")
    assert task_dict["warnings"] is not None
    assert task_dict["warnings"]["highest_severity"] == "error"
    assert task_dict["diagnostics"][0]["kind"] == "repeated_crashes"


# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------


def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(return_value=resp)
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda *a, **kw: (fake_client, model),
    )
    return fake_client


def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps(
            {"title": "Polished", "body": "**Goal**\nDo the thing."}
        ),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")


def test_specify_non_triage_returns_ok_false_not_http_error(client, monkeypatch):
    """The endpoint intentionally returns ``{ok: false, reason: ...}`` for
    "task not in triage" rather than a 4xx — the dashboard renders the
    reason inline so the user can fix it without a page reload."""
    # Create a normal (ready) task — not in triage.
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    _patch_specifier_response(monkeypatch, content="unused")

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "not in triage" in body["reason"]


def test_specify_no_aux_client_surfaces_reason(client, monkeypatch):
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "rough", "triage": True},
    ).json()["task"]

    # Simulate "no auxiliary client configured".
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda *a, **kw: (None, ""),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "auxiliary client" in body["reason"]

    # Task must stay in triage — nothing was touched.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] == "triage"


def test_board_endpoint_accepts_explicit_board_default_param(client):
    """GET /board?board=default must not fall through to env/current-file resolution.

    The dashboard always sends ``?board=<slug>`` (including ``board=default``)
    so that the server-side ``current`` file can never override the dashboard's
    selected board.  This test asserts the endpoint accepts the parameter and
    returns the default board without falling back to environment variable or
    current-file resolution.
    Regression: #21819.
    """
    # Create a task on the default board.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "on-default-board"},
    ).json()["task"]
    assert t["status"] == "ready"

    # Request with explicit board=default — must succeed and include the task.
    r = client.get("/api/plugins/kanban/board?board=default")
    assert r.status_code == 200
    data = r.json()
    ready = next((c for c in data["columns"] if c["name"] == "ready"), None)
    assert ready is not None, "no 'ready' column in default board response"
    task_ids = [task["id"] for task in ready["tasks"]]
    assert t["id"] in task_ids, (
        f"task {t['id']} not found in ready column of default board "
        f"(got tasks: {task_ids}). The board=default param was likely ignored."
    )


def test_dashboard_requests_default_board_explicitly():
    """Dashboard REST calls must include board=default instead of relying on server current board."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    assert "SDK.fetchJSON(withBoard(`${API}/config`, board))" in dist
    assert "SDK.fetchJSON(withBoard(`${API}/boards`, board))" in dist
    assert "}, [loadBoardList, switchBoard, board]);" in dist


def test_dashboard_search_includes_body_and_result():
    """Client-side search must match body, result, latest_summary, and summary
    so full card contents are findable."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    assert "t.body || \"\"" in dist
    assert "t.result || \"\"" in dist
    assert "t.latest_summary || \"\"" in dist


def test_dashboard_bulk_actions_include_reclaim_first():
    """Bulk action bar must expose reclaim_first checkbox and expanded status buttons."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    assert "reclaim_first: reclaimFirst" in dist
    assert "hermes-kanban-bulk-reclaim-first" in dist
    assert '"→ todo"' in dist
    assert '"Block"' in dist
    assert '"Unblock"' in dist


def test_dashboard_shift_click_range_selection_exists():
    """Shift-click must trigger range selection via toggleRange."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    assert "function toggleRange" in dist or "const toggleRange =" in dist
    assert "props.toggleRange(t.id)" in dist or "props.toggleRange" in dist
    assert "e.shiftKey" in dist


def test_dashboard_multi_move_bulk_exists():
    """Dragging a selected card with other selections must use /tasks/bulk."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    assert "onMoveSelected" in dist
    assert "props.onMoveSelected" in dist
    assert "`${API}/tasks/bulk`" in dist


def test_dashboard_failed_card_highlight_class_exists():
    """Partial bulk failures must highlight failing cards."""
    repo_root = Path(__file__).resolve().parents[2]
    js = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()
    css = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "style.css").read_text()

    assert "hermes-kanban-card--failed" in js
    assert "hermes-kanban-card--failed" in css
    assert "failedIds" in js

def test_dashboard_create_accepts_workflow_fields_at_creation(client):
    kb.ensure_product_board_defaults("prod", name="Product")

    r = client.post(
        "/api/plugins/kanban/tasks?board=prod",
        json={
            "title": "User story: dashboard create",
            "workflow_template_id": "product",
            "current_step_key": "backlog",
        },
    )

    assert r.status_code == 200
    task = r.json()["task"]
    assert task["workflow_template_id"] == "product"
    assert task["current_step_key"] == "backlog"


def test_dashboard_lifecycle_patch_uses_selected_product_board_context(client):
    kb.ensure_product_board_defaults("prod", name="Product")
    with kb.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: finish via dashboard",
            workflow_template_id="product",
            current_step_key="backlog",
            initial_status="running",
        )

    r = client.patch(
        f"/api/plugins/kanban/tasks/{tid}?board=prod",
        json={"status": "done", "summary": "PO backlog complete"},
    )

    assert r.status_code == 200
    task = r.json()["task"]
    assert task["workflow_template_id"] == "product"
    assert task["current_step_key"] == "architecture"
    assert task["status"] == "ready"


def test_dashboard_rejects_invalid_product_workflow_patch_without_mutation(client):
    kb.ensure_product_board_defaults("prod-invalid-patch", name="Product")
    with kb.connect(board="prod-invalid-patch") as conn:
        task_id = kb.create_task(
            conn,
            title="User story: preserve valid state",
            workflow_template_id="product",
            current_step_key="backlog",
            board="prod-invalid-patch",
        )
        before = kb.task_snapshot_from_row(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}?board=prod-invalid-patch",
        json={
            "workflow_template_id": "product",
            "current_step_key": "typo-development",
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["current"] == before
    with kb.connect(board="prod-invalid-patch") as conn:
        after = kb.task_snapshot_from_row(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
    assert after == before


def test_dashboard_custom_column_cannot_create_arbitrary_product_step(client):
    board = "prod-invalid-custom-column"
    kb.ensure_product_board_defaults(board, name="Product")
    metadata = kb.read_board_metadata(board)
    metadata["columns"].insert(-1, {"name": "qa_hold", "status": "review"})
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="User story: valid backlog",
            workflow_template_id="product",
            current_step_key="backlog",
            board=board,
        )
        before = kb.task_snapshot_from_row(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}?board={board}",
        json={"status": "qa_hold"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["current"] == before
    with kb.connect(board=board) as conn:
        after = kb.task_snapshot_from_row(
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
    assert after == before


def _task_status(task_id: str) -> str:
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None
        return task.status
    finally:
        conn.close()


def _task_assignee(task_id: str):
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None
        return task.assignee
    finally:
        conn.close()


def _operator_snapshot(task_id: str) -> dict:
    conn = kb.connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None
        return kb.task_snapshot_from_row(row)
    finally:
        conn.close()


def _expected_operator_snapshot(task_id: str) -> dict:
    return {
        f"expected_{field}": value
        for field, value in _operator_snapshot(task_id).items()
    }


@pytest.mark.parametrize(
    ("action", "initial_status", "stale_field", "current_value"),
    [
        ("edit", "ready", "status", "review"),
        ("move", "ready", "title", "Current title"),
        ("assign", "ready", "title", "Current title"),
        ("comment", "ready", "title", "Current title"),
        ("block", "ready", "current_step_key", "architecture"),
        ("reassign", "ready", "assignee", "tester"),
        ("approve", "blocked", "current_step_key", "architecture"),
    ],
)
def test_conditional_operator_writes_reject_stale_snapshot_without_mutation(
    client,
    action,
    initial_status,
    stale_field,
    current_value,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Snapshot title",
            assignee="architect",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="backlog",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (initial_status, task_id),
            )

    expected = _expected_operator_snapshot(task_id)
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                f"UPDATE tasks SET {stale_field} = ? WHERE id = ?",
                (current_value, task_id),
            )
        comments_before = len(kb.list_comments(conn, task_id))
        events_before = len(kb.list_events(conn, task_id))
    current_before = _operator_snapshot(task_id)

    if action == "edit":
        response = client.patch(
            f"/api/plugins/kanban/tasks/{task_id}",
            json={"title": "Operator edit", **expected},
        )
    elif action == "move":
        response = client.patch(
            f"/api/plugins/kanban/tasks/{task_id}",
            json={"status": "review", **expected},
        )
    elif action == "assign":
        response = client.patch(
            f"/api/plugins/kanban/tasks/{task_id}",
            json={"assignee": "developer", **expected},
        )
    elif action == "comment":
        response = client.post(
            f"/api/plugins/kanban/tasks/{task_id}/comments",
            json={"body": "Operator note", **expected},
        )
    elif action == "block":
        response = client.patch(
            f"/api/plugins/kanban/tasks/{task_id}",
            json={
                "status": "blocked",
                "block_reason": "Operator block",
                **expected,
            },
        )
    elif action == "reassign":
        response = client.post(
            f"/api/plugins/kanban/tasks/{task_id}/reassign",
            json={"profile": "developer", **expected},
        )
    else:
        response = client.post(
            f"/api/plugins/kanban/tasks/{task_id}/approve-unblock",
            json={"confirmed": True, **expected},
        )

    assert response.status_code == 409, response.text
    body = response.json()
    assert "refresh" in body["detail"]
    assert body["current"] == current_before
    assert _operator_snapshot(task_id) == current_before
    with kb.connect() as conn:
        assert len(kb.list_comments(conn, task_id)) == comments_before
        assert len(kb.list_events(conn, task_id)) == events_before


def test_conditional_comment_applies_when_snapshot_matches(client):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Matching comment target", "assignee": "developer"},
    ).json()["task"]["id"]

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/comments",
        json={"body": "Fresh operator note", **_expected_operator_snapshot(task_id)},
    )

    assert response.status_code == 200, response.text
    with kb.connect() as conn:
        comments = kb.list_comments(conn, task_id)
    assert [comment.body for comment in comments] == ["Fresh operator note"]


def test_existing_task_mutations_require_complete_snapshot(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    raw_client = TestClient(app)
    task_id = raw_client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Snapshot required"},
    ).json()["task"]["id"]

    missing = raw_client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"title": "Bypass attempt"},
    )
    partial = raw_client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"title": "Bypass attempt", "expected_status": "ready"},
    )

    assert missing.status_code == 422
    assert partial.status_code == 422
    assert _operator_snapshot(task_id)["title"] == "Snapshot required"


@pytest.mark.parametrize(
    "action",
    [
        "delete",
        "bulk",
        "reclaim",
        "terminate",
        "specify",
        "decompose",
        "link",
        "unlink",
        "upload_attachment",
        "delete_attachment",
    ],
)
def test_remaining_operator_writes_reject_stale_snapshot_without_mutation(
    client,
    tmp_path,
    action,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Operator target",
            assignee="developer",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="backlog",
        )
        parent_id = kb.create_task(
            conn,
            title="Dependency parent",
            initial_status="blocked",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        if action == "unlink":
            kb.link_tasks(conn, parent_id, task_id)
        attachment_id = None
        if action == "delete_attachment":
            stored = tmp_path / "operator-note.txt"
            stored.write_text("preserve", encoding="utf-8")
            attachment_id = kb.add_attachment(
                conn,
                task_id,
                filename=stored.name,
                stored_path=str(stored),
                size=stored.stat().st_size,
            )
        run_id = None
        if action == "terminate":
            with kb.write_txn(conn):
                run = conn.execute(
                    """
                    INSERT INTO task_runs
                        (task_id, profile, status, started_at, ended_at)
                    VALUES (?, 'developer', 'running', 1234, NULL)
                    """,
                    (task_id,),
                )
                run_id = int(run.lastrowid)
                conn.execute(
                    "UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?",
                    (run_id, task_id),
                )

    expected = _expected_operator_snapshot(task_id)
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title = 'Current operator target' WHERE id = ?",
                (task_id,),
            )
        comments_before = len(kb.list_comments(conn, task_id))
        events_before = len(kb.list_events(conn, task_id))
        priority_before = kb.get_task(conn, task_id).priority
    current_before = _operator_snapshot(task_id)

    if action == "delete":
        response = client.request(
            "DELETE",
            f"/api/plugins/kanban/tasks/{task_id}",
            json=expected,
        )
    elif action == "bulk":
        response = client.post(
            "/api/plugins/kanban/tasks/bulk",
            json={
                "ids": [task_id],
                "priority": 9,
                "expected_snapshots": {task_id: expected},
            },
        )
    elif action == "reclaim":
        response = client.post(
            f"/api/plugins/kanban/tasks/{task_id}/reclaim",
            json=expected,
        )
    elif action == "terminate":
        response = client.post(
            f"/api/plugins/kanban/runs/{run_id}/terminate",
            json=expected,
        )
    elif action in {"specify", "decompose"}:
        response = client.post(
            f"/api/plugins/kanban/tasks/{task_id}/{action}",
            json=expected,
        )
    elif action == "link":
        response = client.post(
            "/api/plugins/kanban/links",
            json={
                "parent_id": parent_id,
                "child_id": task_id,
                "expected_task_id": task_id,
                **expected,
            },
        )
    elif action == "unlink":
        response = client.request(
            "DELETE",
            f"/api/plugins/kanban/links?parent_id={parent_id}&child_id={task_id}",
            json={"expected_task_id": task_id, **expected},
        )
    elif action == "upload_attachment":
        response = client.post(
            f"/api/plugins/kanban/tasks/{task_id}/attachments",
            data={"expected_snapshot": json.dumps(expected)},
            files={"file": ("new.txt", b"new attachment", "text/plain")},
        )
    else:
        response = client.request(
            "DELETE",
            f"/api/plugins/kanban/attachments/{attachment_id}",
            json=expected,
        )

    assert response.status_code == 409, response.text
    assert response.json()["current"] == current_before
    assert _operator_snapshot(task_id) == current_before
    with kb.connect() as conn:
        assert len(kb.list_comments(conn, task_id)) == comments_before
        assert len(kb.list_events(conn, task_id)) == events_before
        assert kb.get_task(conn, task_id).priority == priority_before
        link = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, task_id),
        ).fetchone()
        assert bool(link) is (action == "unlink")
        if attachment_id is not None:
            assert kb.get_attachment(conn, attachment_id) is not None
        if run_id is not None:
            assert kb.get_run(conn, run_id).ended_at is None


def test_conditional_bulk_requires_snapshot_for_every_task(client):
    first = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Bulk first"},
    ).json()["task"]["id"]
    second = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Bulk second"},
    ).json()["task"]["id"]

    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [first, second],
            "priority": 9,
            "expected_snapshots": {first: _expected_operator_snapshot(first)},
        },
    )

    assert response.status_code == 400, response.text
    with kb.connect() as conn:
        assert kb.get_task(conn, first).priority == 0
        assert kb.get_task(conn, second).priority == 0


def test_conditional_manual_block_accepts_todo_and_review_cards(client):
    """Cockpit can manually block non-running product-workflow cards via API.

    Regression coverage for the Agentic OS Cockpit API migration: the old
    direct-DB Cockpit control allowed todo/review/ready cards to be blocked.
    The worker-oriented block_task helper only accepts running/ready, so the
    dashboard API needs an explicit compare-and-swap manual block path.
    """
    todo_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "todo block target"},
    ).json()["task"]["id"]
    review_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "review block target"},
    ).json()["task"]["id"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (todo_id,))
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review_id,))
    finally:
        conn.close()

    todo_resp = client.patch(
        f"/api/plugins/kanban/tasks/{todo_id}",
        json={
            "status": "blocked",
            "block_reason": "waiting for product input",
            "expected_status": "todo",
            "expected_current_run_id": None,
        },
    )
    review_resp = client.patch(
        f"/api/plugins/kanban/tasks/{review_id}",
        json={
            "status": "blocked",
            "block_reason": "waiting for compliance review",
            "expected_status": "review",
            "expected_current_run_id": None,
        },
    )

    assert todo_resp.status_code == 200, todo_resp.text
    assert review_resp.status_code == 200, review_resp.text
    assert _task_status(todo_id) == "blocked"
    assert _task_status(review_id) == "blocked"


def test_conditional_manual_block_rejects_stale_status_snapshot(client):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "stale block target"},
    ).json()["task"]["id"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
    finally:
        conn.close()

    resp = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={
            "status": "blocked",
            "block_reason": "stale snapshot should fail",
            "expected_status": "ready",
            "expected_current_run_id": None,
        },
    )

    assert resp.status_code == 409, resp.text
    assert _task_status(task_id) == "review"


def test_conditional_manual_block_rejects_active_current_run_even_when_snapshot_matches(client):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "active run block target"},
    ).json()["task"]["id"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            run_cur = conn.execute(
                """
                INSERT INTO task_runs (task_id, profile, step_key, status, started_at, ended_at)
                VALUES (?, ?, ?, 'running', ?, NULL)
                """,
                (task_id, "developer", "development", 1234),
            )
            run_id = run_cur.lastrowid
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=? WHERE id=?",
                (run_id, task_id),
            )
    finally:
        conn.close()

    resp = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={
            "status": "blocked",
            "block_reason": "active run should fail",
            "expected_status": "ready",
            "expected_current_run_id": run_id,
        },
    )

    assert resp.status_code == 409, resp.text
    assert _task_status(task_id) == "ready"


def test_conditional_manual_block_clears_stale_failure_state(client):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "failure state block target"},
    ).json()["task"]["id"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', consecutive_failures=4, last_failure_error='old failure' WHERE id=?",
                (task_id,),
            )
    finally:
        conn.close()

    resp = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={
            "status": "blocked",
            "block_reason": "manual operator block",
            "expected_status": "ready",
            "expected_current_run_id": None,
        },
    )

    assert resp.status_code == 200, resp.text
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, consecutive_failures, last_failure_error FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "blocked"
    assert row["consecutive_failures"] == 0
    assert row["last_failure_error"] is None


def test_conditional_manual_block_fires_hook_and_stays_blocked(
    client,
    monkeypatch,
):
    fired = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: fired.append((event, task_id, fields)),
    )
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "sticky operator block"},
    ).json()["task"]["id"]

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "blocked", "block_reason": "waiting for operator"},
    )

    assert response.status_code == 200, response.text
    assert len(fired) == 1
    event, fired_task_id, fields = fired[0]
    assert event == "kanban_task_blocked"
    assert fired_task_id == task_id
    assert fields["reason"] == "waiting for operator"
    with kb.connect() as conn:
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"


def test_conditional_manual_block_preserves_product_preflight_routing(client):
    kb.ensure_product_board_defaults("prod", name="Product")
    task_id = client.post(
        "/api/plugins/kanban/tasks?board=prod",
        json={
            "title": "Product block target",
            "assignee": "developer",
            "workflow_template_id": "product",
            "current_step_key": "backlog",
        },
    ).json()["task"]["id"]
    with kb.connect(board="prod") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        expected = {
            "expected_status": task.status,
            "expected_title": task.title,
            "expected_assignee": task.assignee,
            "expected_current_step_key": task.current_step_key,
            "expected_current_run_id": task.current_run_id,
        }

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}?board=prod",
        json={"status": "blocked", "block_reason": "operator hold", **expected},
    )

    assert response.status_code == 200, response.text
    with kb.connect(board="prod") as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "ready"
    assert task.running is False
    assert task.blocked is False
    assert task.assignee == "default"


def test_conditional_reassign_rejects_stale_assignee_snapshot(client):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "stale reassign target", "assignee": "architect"},
    ).json()["task"]["id"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='tester' WHERE id=?", (task_id,))
    finally:
        conn.close()

    resp = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/reassign",
        json={
            "profile": "developer",
            "reclaim_first": False,
            "reason": "Cockpit redirect",
            "expected_status": "ready",
            "expected_current_run_id": None,
            "expected_assignee": "architect",
        },
    )

    assert resp.status_code == 409, resp.text
    assert _task_assignee(task_id) == "tester"


def test_conditional_reassign_with_reclaim_rejects_stale_snapshot(client):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="running", assignee="architect")
        assert kb.claim_task(conn, task_id) is not None
    expected = _expected_operator_snapshot(task_id)
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title='changed elsewhere' WHERE id=?",
                (task_id,),
            )
    before = _operator_snapshot(task_id)

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/reassign",
        json={
            "profile": "developer",
            "reclaim_first": True,
            "reason": "Cockpit redirect",
            **expected,
        },
    )

    assert response.status_code == 409, response.text
    assert _operator_snapshot(task_id) == before


def test_conditional_reassign_rejects_active_current_run_even_when_snapshot_matches(client):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "active run reassign target", "assignee": "architect"},
    ).json()["task"]["id"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            run_cur = conn.execute(
                """
                INSERT INTO task_runs (task_id, profile, step_key, status, started_at, ended_at)
                VALUES (?, ?, ?, 'running', ?, NULL)
                """,
                (task_id, "developer", "development", 2345),
            )
            run_id = run_cur.lastrowid
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=? WHERE id=?",
                (run_id, task_id),
            )
    finally:
        conn.close()

    resp = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/reassign",
        json={
            "profile": "developer",
            "reclaim_first": False,
            "reason": "Cockpit redirect",
            "expected_status": "ready",
            "expected_current_run_id": run_id,
            "expected_assignee": "architect",
        },
    )

    assert resp.status_code == 409, resp.text
    assert _task_assignee(task_id) == "architect"


def test_conditional_reassign_applies_when_snapshot_matches(client):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "matching reassign target", "assignee": "architect"},
    ).json()["task"]["id"]

    resp = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/reassign",
        json={
            "profile": "developer",
            "reclaim_first": False,
            "reason": "Cockpit redirect",
            "expected_status": "ready",
            "expected_current_run_id": None,
            "expected_assignee": "architect",
        },
    )

    assert resp.status_code == 200, resp.text
    assert _task_assignee(task_id) == "developer"


def test_conditional_reassign_holds_write_lock_through_canonical_mutation(
    client,
    monkeypatch,
):
    task_id = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Race target", "assignee": "architect"},
    ).json()["task"]["id"]
    expected = _expected_operator_snapshot(task_id)
    original_assign = kb.assign_task
    race = {"blocked": False}

    def assign_with_competing_writer(conn, target_id, profile):
        competing = kb.connect()
        try:
            competing.execute("PRAGMA busy_timeout = 0")
            with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
                with kb.write_txn(competing):
                    competing.execute(
                        "UPDATE tasks SET title = 'Lost race' WHERE id = ?",
                        (target_id,),
                    )
            race["blocked"] = True
        finally:
            competing.close()
        return original_assign(conn, target_id, profile)

    monkeypatch.setattr(kb, "assign_task", assign_with_competing_writer)

    response = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/reassign",
        json={"profile": "developer", **expected},
    )

    assert response.status_code == 200, response.text
    assert race["blocked"] is True
    assert _operator_snapshot(task_id)["title"] == "Race target"
    assert _task_assignee(task_id) == "developer"


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------


def test_task_detail_exposes_result_and_latest_summary_separately(client):
    """The drawer receives both source fields without a duplicate alias."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Task with explicit result"},
    )
    task_id = r.json()["task"]["id"]
    client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "done", "result": "The final answer is 42.", "summary": "short handoff"},
    )
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200
    data = r.json()["task"]
    assert data["result"] == "The final answer is 42."
    assert data["latest_summary"] == "short handoff"
    assert "final_result" not in data


def test_task_detail_exposes_latest_summary_when_result_is_empty(client):
    """Summary-only completions remain available to the drawer fallback."""
    conn = kb.connect()
    task_id = kb.create_task(conn, title="Task with only run summary")
    kb.claim_task(conn, task_id)
    kb.complete_task(conn, task_id, summary="Report written to /output/report.md")
    conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200
    data = r.json()["task"]
    assert data["status"] == "done"
    assert not data["result"]
    assert data["latest_summary"] == "Report written to /output/report.md"


def test_task_detail_latest_summary_none_when_nothing_recorded(client):
    """When no run summary exists, the existing field remains None."""
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "Task with no result at all"},
    )
    task_id = r.json()["task"]["id"]
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200
    assert r.json()["task"]["latest_summary"] is None


def test_board_tasks_include_latest_summary(client):
    """Board cards already expose the summary used by the drawer fallback."""
    conn = kb.connect()
    task_id = kb.create_task(conn, title="Board card with summary only")
    kb.claim_task(conn, task_id)
    kb.complete_task(conn, task_id, summary="Done: see attachment")
    conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    done_col = next(c for c in r.json()["columns"] if c["name"] == "done")
    card = next((t for t in done_col["tasks"] if t["id"] == task_id), None)
    assert card is not None
    assert "Done: see attachment" in card["latest_summary"]


def test_dashboard_done_final_result_section_rendered_from_summary():
    """Frontend must render Final Result section from run summary when task.result is empty."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()
    assert "t.result || t.latest_summary" in dist
    assert "Final Result (run summary)" in dist
    assert "No final result was recorded" in dist
    assert "orchestrator" in dist or "parent task" in dist


def test_task_detail_includes_child_result_summaries(client):
    """Parent drawers should receive the child results they need to render."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="Research topic")
        child = kb.create_task(conn, title="Collect sources")
        kb.link_tasks(conn, parent, child)
        kb.complete_task(conn, parent, summary="Delegated research to child tasks.")
        kb.recompute_ready(conn)
        kb.complete_task(conn, child, summary="Collected five primary sources.")

    response = client.get(f"/api/plugins/kanban/tasks/{parent}")

    assert response.status_code == 200
    assert response.json()["child_results"] == [
        {
            "id": child,
            "title": "Collect sources",
            "status": "done",
            "latest_summary": "Collected five primary sources.",
            "result": None,
        }
    ]


def test_dashboard_final_result_uses_existing_fields_without_alias():
    """The drawer should not duplicate result/summary into another API field."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()
    api = (repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py").read_text()

    assert "var finalResult = t.result || t.latest_summary || null;" in dist
    assert "t.final_result" not in dist
    assert 'd["final_result"]' not in api


def test_dashboard_parent_notice_and_child_results_use_detail_links():
    """Parent detection must use links.children, which exists in task detail."""
    repo_root = Path(__file__).resolve().parents[2]
    dist = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()
    detail = dist[dist.index("function TaskDetail"):]

    assert "links.children.length > 0" in detail
    assert "t.link_counts" not in detail
    assert "Child Results" in detail
    assert "props.data.child_results" in detail
