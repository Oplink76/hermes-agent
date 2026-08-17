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
    epics = board["epics"]
    assert len(epics) == 1
    assert epics[0]["id"] == epic_id
    assert epics[0]["title"] == "Portfolio outcome"
    assert epics[0]["workItemKind"] == "epic"
    assert epics[0]["progress"] == {"done": 0, "total": 1, "release_state": "pending"}
    # Truthful named lifecycle state surfaced per epic (E07).
    assert epics[0]["release_state"] == "collecting_members"
    assert epics[0]["release_actionable"] is False
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
    named = epic_detail["epic_detail"]
    assert named["release_state"] == "collecting_members"
    assert named["release"]["state"] == "collecting_members"
    assert named["release"]["actionable"] is False


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


def test_operator_intake_detail_exposes_only_bounded_contract_failure_path(client):
    kb.ensure_product_board_defaults("strict")
    with kb.connect(board="strict") as conn:
        intake_id = kb.create_qualification_intake(
            conn,
            raw_request=json.dumps({"title": "Operator-visible intake"}),
            source="dashboard-api",
        )
        kb.append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            kind="work_contract_verification_failed",
            payload={"failure_path": "io_error", "raw": "secret-event-sentinel"},
        )
        kb.append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            kind="work_contract_verification_failed",
            payload={"failure_path": "arbitrary-unsafe-path"},
        )

    response = client.get(
        f"/api/plugins/kanban/intake/{intake_id}?board=strict"
    )

    assert response.status_code == 200, response.text
    assert response.json()["failure_path"] == "io_error"
    assert "secret-event-sentinel" not in response.text
    assert "arbitrary-unsafe-path" not in response.text


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
    assert epic["epic_detail"]["outcome"] == "Customers receive the governed outcome"
    assert epic["epic_detail"]["scope"] == ["Hermes"]
    assert epic["epic_detail"]["constraints"] == ["bypass release evidence"]
    assert epic["epic_detail"]["definition_of_done"] == ["outcome measured"]
    assert epic["epic_detail"]["members"] == [card_id]
    assert epic["epic_detail"]["progress"] == {
        "done": 0, "total": 1, "release_state": "pending",
    }
    # E07: truthful named lifecycle state (read-only), not the coarse flag.
    assert epic["epic_detail"]["release_state"] == "collecting_members"
    release = epic["epic_detail"]["release"]
    assert release["kind"] == "epic"
    assert release["state"] == "collecting_members"
    assert release["actionable"] is False
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

    for field, value in (
        ("priority", 99),
        ("result", "bulk-forged result"),
        ("summary", "bulk-forged summary"),
        ("metadata", {"forged": True}),
    ):
        with kb.connect(board="strict") as conn:
            before = kb.get_task(conn, first)
            runs_before = [
                tuple(row)
                for row in conn.execute(
                    """
                    SELECT id, status, outcome, summary, metadata
                      FROM task_runs
                     WHERE task_id = ?
                     ORDER BY id
                    """,
                    (first,),
                ).fetchall()
            ]
        bulk_contract = client.post(
            "/api/plugins/kanban/tasks/bulk?board=strict",
            json={"ids": [first], field: value},
        )
        assert bulk_contract.status_code == 409, field
        assert "Work Contract" in bulk_contract.text, field
        with kb.connect(board="strict") as conn:
            after = kb.get_task(conn, first)
            runs_after = [
                tuple(row)
                for row in conn.execute(
                    """
                    SELECT id, status, outcome, summary, metadata
                      FROM task_runs
                     WHERE task_id = ?
                     ORDER BY id
                    """,
                    (first,),
                ).fetchall()
            ]
        assert after.priority == before.priority, field
        assert after.result == before.result, field
        assert runs_after == runs_before, field

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


def test_patch_board_sets_project_directory(client, tmp_path):
    """Board-level default_workdir must be editable after creation."""
    kb.create_board("late-config")
    project_dir = tmp_path / "late-project"
    project_dir.mkdir()

    response = client.patch(
        "/api/plugins/kanban/boards/late-config",
        json={"default_workdir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    # The recommendation flips from scratch to a persistent kind so the
    # create-task dialog's workspace default follows the board setting.
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("late-config")["default_workdir"] == str(
        project_dir.resolve()
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


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------


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
    # specify_task routes through call_llm now (#35566) — mock it directly.
    fake_call = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    return fake_call


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

    # Simulate "no auxiliary client configured" — call_llm raises when
    # no provider resolves (#35566 routing).
    def _no_provider(**kwargs):
        raise RuntimeError("No LLM provider configured")
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _no_provider)

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    # call_llm's no-provider RuntimeError surfaces via the LLM-error branch.
    assert "LLM error" in body["reason"]

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
# Truthful release lifecycle state (E07)
# ---------------------------------------------------------------------------


def _epic_task(conn, epic_id="epic-e07", board=None):
    return kb.create_task(
        conn,
        title=f"Epic: {epic_id}",
        board=board,
        work_item_kind="epic",
    )


def _member_task(conn, epic_id, story_id="story-e07"):
    task_id = kb.create_task(conn, title=f"Story: {story_id}", board=None)
    conn.execute(
        "INSERT INTO epic_memberships (epic_id, task_id, created_at) "
        "VALUES (?, ?, 1)",
        (epic_id, task_id),
    )
    return task_id


def test_release_state_endpoint_epic_collecting_members(client):
    with kb.connect() as conn:
        epic_id = _epic_task(conn)

    resp = client.get(f"/api/plugins/kanban/tasks/{epic_id}/release-state")

    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["kind"] == "epic"
    assert state["state"] == "collecting_members"
    assert state["actionable"] is False


def test_release_state_endpoint_404_for_unknown_task(client):
    resp = client.get("/api/plugins/kanban/tasks/t_missing/release-state")
    assert resp.status_code == 404


def test_release_state_endpoint_epic_ci_failed(client):
    with kb.connect() as conn:
        epic_id = _epic_task(conn)
        conn.execute(
            "INSERT INTO epic_release_snapshots (epic_id, epic_tip_sha, target_branch, "
            "target_pre_sha, release_candidate_sha, candidate_ref, "
            "aggregate_verification_event_id, repository_contract_digest, "
            "status, pushed_sha, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ci_failed', ?, ?, ?)",
            (epic_id, "1" * 40, "main", "2" * 40, "3" * 40,
             "refs/hermes/releases/epic-1", 71, "7" * 64, "6" * 40, 100, 110),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'epic_release_ci_failed', ?, 1)",
            (epic_id, json.dumps(
                {"conclusions": {"CI": "failure", "Deploy Test": "cancelled"}})),
        )

    resp = client.get(f"/api/plugins/kanban/tasks/{epic_id}/release-state")

    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["kind"] == "epic"
    assert state["state"] == "ci_failed"
    assert state["actionable"] is False
    assert (state.get("evidence") or {}).get("ci_evidence") == {
        "CI": "failure",
        "Deploy Test": "cancelled",
    }


def test_release_state_endpoint_member_integrating(client):
    with kb.connect() as conn:
        epic_id = _epic_task(conn)
        member_id = _member_task(conn, epic_id)
        conn.execute(
            "INSERT INTO story_integration_intents "
            "(epic_id, story_id, source_sha, source_branch, review_run_id, "
            "review_base_sha, status, attempt_count, last_failure_code, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'b', 1, ?, 'pending', 2, NULL, 1, 1)",
            (epic_id, member_id, "9" * 40, "0" * 40),
        )

    resp = client.get(f"/api/plugins/kanban/tasks/{member_id}/release-state")

    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["kind"] == "member"
    assert state["state"] == "integrating"
    assert state["actionable"] is False
    intent = (state.get("evidence") or {}).get("intent") or {}
    assert intent.get("status") == "pending"
    assert intent.get("attempt_count") == 2

    # The member detail payload carries the same read-only state and no
    # Release/Measure surface.
    detail = client.get(f"/api/plugins/kanban/tasks/{member_id}").json()
    assert detail.get("member_release_state", {}).get("state") == "integrating"
    assert "release_measure" not in json.dumps(detail.get("member_release_state"))


def test_release_state_endpoint_actionable_requires_e06_target_check(
    client, monkeypatch
):
    """Actionable=True is granted only after the E06 target re-check passes."""
    with kb.connect() as conn:
        epic_id = _epic_task(conn)
        conn.execute(
            "INSERT INTO epic_release_snapshots (epic_id, epic_tip_sha, target_branch, "
            "target_pre_sha, release_candidate_sha, candidate_ref, "
            "aggregate_verification_event_id, repository_contract_digest, "
            "status, pushed_sha, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_push', NULL, ?, ?)",
            (epic_id, "1" * 40, "main", "2" * 40, "3" * 40,
             "refs/hermes/releases/epic-1", 71, "7" * 64, 100, 110),
        )

    class _Handoff:
        def __init__(self, local_target_head, remote_target_head, remote_name,
                     checked_at, action):
            self.local_target_head = local_target_head
            self.remote_target_head = remote_target_head
            self.remote_name = remote_name
            self.checked_at = checked_at
            self.action = action

    from hermes_cli import kanban_db

    def failing_handoff(conn, eid, **kw):
        raise kanban_db.EpicReleaseHandoffError("remote_unavailable", {"epic_id": eid})

    monkeypatch.setattr(kanban_db, "build_epic_release_handoff", failing_handoff)
    resp = client.get(f"/api/plugins/kanban/tasks/{epic_id}/release-state")
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["state"] == "awaiting_push"
    assert state["actionable"] is False

    def passing_handoff(conn, eid, **kw):
        return _Handoff(
            local_target_head="2" * 40,
            remote_target_head="2" * 40,
            remote_name="origin",
            checked_at=123,
            action="Merge and push the pinned candidate externally.",
        )

    monkeypatch.setattr(kanban_db, "build_epic_release_handoff", passing_handoff)
    resp = client.get(f"/api/plugins/kanban/tasks/{epic_id}/release-state")
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["state"] == "awaiting_final_release"
    assert state["actionable"] is True
    assert "Merge and push" in (state.get("action") or "")


def test_task_detail_surfaces_named_release_state_for_epic(client):
    with kb.connect() as conn:
        epic_id = _epic_task(conn)
        member_id = _member_task(conn, epic_id)

    detail = client.get(f"/api/plugins/kanban/tasks/{epic_id}").json()

    epic_detail = detail.get("epic_detail") or {}
    assert epic_detail["release_state"] == "collecting_members"
    assert epic_detail["release"]["kind"] == "epic"
    assert epic_detail["release"]["state"] == "collecting_members"
    assert epic_detail["release"]["actionable"] is False


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Upstream review-lifecycle coverage (adopted in the 2026-08-17 sync)
# ---------------------------------------------------------------------------

def test_bulk_review_assignment_preserves_implementer_provenance(client):
    tasks = [
        client.post(
            "/api/plugins/kanban/tasks",
            json={"title": title, "assignee": "builder"},
        ).json()["task"]
        for title in ("review a", "review b")
    ]
    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [task["id"] for task in tasks],
            "status": "review",
            "assignee": "reviewer",
            "summary": "ready",
        },
    )
    assert response.status_code == 200, response.text
    assert all(item["ok"] for item in response.json()["results"])
    with kb.connect() as conn:
        for task in tasks:
            current = kb.get_task(conn, task["id"])
            assert current is not None
            assert current.status == "review"
            assert current.assignee == "reviewer"
            event = [
                item for item in kb.list_events(conn, task["id"])
                if item.kind == "review_requested"
            ][-1]
            assert event.payload is not None
            assert event.payload["implementer"] == "builder"
            assert event.payload["reviewer"] == "reviewer"


def test_dashboard_cancel_keeps_task_in_old_status(client):
    """Behavioral: the cancel branch of the dispatch path (no PATCH/DELETE
    issued) must leave the task in its previous status. The cancel guard
    lives in the bundle; this test pins the backend contract that the guard
    relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Tasks land in ``ready`` by default. No PATCH issued — simulating the
    # cancel branch in the bundle.
    assert t["status"] == "ready"
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.json()["task"]["status"] == "ready"


def test_dashboard_confirm_dispatches_expected_delete(client):
    """Behavioral: the DELETE call the bundle issues on confirm
    (``fetchJSON(`${API}/tasks/${id}`, { method: 'DELETE' })``) must
    succeed and remove the task.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200, r.text
    # 404 on the now-deleted task confirms removal.
    r2 = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r2.status_code == 404


def test_dashboard_confirm_dispatches_expected_patch_body(client):
    """Behavioral: the PATCH body shape the bundle produces on confirm
    (status + result + summary) must be accepted by the backend without
    rejection. The backend stores ``result`` as the human-readable
    completion summary (the bundle comments confirm ``summary`` is sent
    duplicatively so the backend can store the value under its preferred
    key while the wire format remains explicit).
    This is the contract the bundle's performMoveTask relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Bundle's performMoveTask on confirm with a summary produces:
    #   { status, result: summary, summary: summary }
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done", "result": "shipped", "summary": "shipped"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["task"]
    assert body["status"] == "done"
    assert body.get("result") == "shipped"


def test_dashboard_reclaim_of_active_review_preserves_review_phase(client):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="active review", assignee="reviewer")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    assert response.json()["task"]["assignee"] == "reviewer"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "reclaimed"
        next_review = kb.claim_review_task(conn, task_id)
        assert next_review is not None


def test_patch_review_lifecycle_preserves_handoff_and_reopens(client):
    secret = "ghp_" + "D" * 40
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "review me", "assignee": "builder"},
    ).json()["task"]

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={
            "status": "review",
            "assignee": "reviewer",
            "summary": f"Implementation ready. {secret}",
            "metadata": {"tests_run": 4, "token": secret},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task["id"])
        assert run is not None
        assert run.outcome == "review_requested"
        assert run.metadata is not None
        assert run.metadata["tests_run"] == 4
        assert secret not in str(run.summary)
        assert secret not in json.dumps(run.metadata)
        review_event = [
            event for event in kb.list_events(conn, task["id"])
            if event.kind == "review_requested"
        ][-1]
        assert secret not in json.dumps(review_event.payload)
        assert review_event.payload is not None
        assert review_event.payload["implementer"] == "builder"
        assert review_event.payload["reviewer"] == "reviewer"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
    assert response.json()["task"]["assignee"] == "builder"
    with kb.connect() as conn:
        assert any(
            event.kind == "review_reopened"
            for event in kb.list_events(conn, task["id"])
        )


def test_reopening_parent_recursively_retracts_done_and_running_descendants(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="root", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="accepted child",
            assignee="builder",
            parents=[parent_id],
        )
        assert kb.complete_task(conn, child_id)
        grandchild_id = kb.create_task(
            conn,
            title="running grandchild",
            assignee="writer",
            parents=[child_id],
        )
        grandchild_run = kb.claim_task(conn, grandchild_id)
        assert grandchild_run is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "todo"
        assert grandchild is not None and grandchild.status == "todo"
        assert grandchild.current_run_id is None
        assert kb.claim_task(conn, grandchild_id) is None
        reclaimed = kb.latest_run(conn, grandchild_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text
    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "ready"
        assert grandchild is not None and grandchild.status == "todo"


def test_reopening_parent_retracts_review_and_blocks_approval(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="child in review",
            assignee="reviewer",
            parents=[parent_id],
        )
        grandchild_id = kb.create_task(
            conn,
            title="downstream",
            assignee="writer",
            parents=[child_id],
        )
        implementation = kb.claim_task(conn, child_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            child_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        active_review = kb.claim_review_task(conn, child_id)
        assert active_review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo"
        reclaimed = kb.latest_run(conn, child_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"
        assert kb.claim_review_task(conn, child_id) is None
        assert not kb.complete_task(conn, child_id, summary="must not approve")
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "todo"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "review"
        review = kb.claim_review_task(conn, child_id)
        assert review is not None
        assert kb.complete_task(
            conn,
            child_id,
            summary="approved after parent stabilized",
            expected_run_id=review.current_run_id,
        )
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "ready"

