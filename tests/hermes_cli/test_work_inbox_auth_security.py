"""Real middleware tests for the Work Inbox bearer route."""
from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake as intake
from hermes_cli import plugins, web_server
from hermes_cli.dashboard_auth import clear_providers, token_auth


@pytest.fixture
def strong_secret() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def app_client(strong_secret, monkeypatch):
    clear_providers()
    token_auth.clear_token_routes()
    monkeypatch.setenv("HERMES_WORK_INBOX_SECRET", strong_secret)
    monkeypatch.setattr(plugins, "_plugin_manager", plugins.PluginManager())
    plugins.discover_plugins()
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    try:
        yield TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    finally:
        for key, value in previous.items():
            setattr(web_server.app.state, key, value)
        clear_providers()
        token_auth.clear_token_routes()


@pytest.fixture
def strict_board() -> str:
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return "strict"


def _contracted_running_card(board: str, tmp_path: Path) -> tuple[str, str, int, str]:
    repo = tmp_path / f"repo-{secrets.token_hex(4)}"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Work Inbox Test"], check=True)
    (repo / "delivered.txt").write_text("delivered\n", encoding="utf-8")
    with kb.connect(board=board) as conn:
        intake_id = kb.create_qualification_intake(
            conn,
            raw_request=json.dumps({"kind": "task_create", "request": {"title": "Assigned change"}}),
            source="test",
            attachments=[
                {"name": "backlog-artifact"},
                {"name": "architecture-artifact"},
            ],
        )
        signed = intake.sign_work_contract(
            {
                "version": 1,
                "policy_version": "product-handoff-v2+qualification-v1",
                "qualification_path": "hermes",
                "request_id": intake_id,
                "work": {
                    "item_kind": "card", "work_type": "story",
                    "title": "Assigned change", "outcome": "Safe delivery",
                    "scope": [], "out_of_scope": [],
                },
                "routing": {
                    "entry_phase": "development", "assignee": "developer",
                    "epic_id": None, "dependencies": [],
                },
                "entry_assessment": {
                    "reason": "Earlier phases are satisfied",
                    "skipped_phases": [
                        {"phase": "backlog", "reason": "evidence", "evidence": ["backlog-artifact"]},
                        {"phase": "architecture", "reason": "evidence", "evidence": ["architecture-artifact"]},
                    ],
                    "evidence": ["backlog-artifact", "architecture-artifact"],
                },
                "handover": {
                    "deliverables": [], "required_evidence": [], "done_when": [],
                    "next_phase": "test", "next_role": "tester",
                },
                "rules": {"allowed": [], "forbidden": []},
                "classification": ["framework:story"],
                "issuer": {"profile": "hermes", "run_id": 42, "issued_at": 1_784_270_000},
            },
            secret=b"test-only-secret",
        )
        task_id = intake.materialize_contract(
            conn, board=board, signed_contract=signed, secret=b"test-only-secret",
        )
        kb.set_workspace_path(conn, task_id, str(repo))
        task = kb.claim_task(conn, task_id, board=board, claimer="work-inbox-test")
        assert task is not None and task.current_run_id is not None
        return board, task_id, task.current_run_id, str(task.work_contract_id)


def _assigned_completion_body(
    task_id: str,
    run_id: int,
    contract_id: str,
) -> dict:
    return {
        "version": 2,
        "kind": "assigned_delivery",
        "task_id": task_id,
        "run_id": run_id,
        "work_contract_id": contract_id,
        "outcome": "completed",
        "summary": "wrong assignment",
    }


def test_exact_work_inbox_route_uses_real_bearer_middleware(
    app_client, strict_board, strong_secret,
):
    accepted = app_client.post(
        "/api/plugins/kanban/work-inbox?board=strict",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={"version": 2, "kind": "new_work", "request": {}},
    )

    assert accepted.status_code != 401
    assert app_client.post(
        "/api/plugins/kanban/work-inbox?board=strict",
        headers={"Authorization": "Bearer wrong"},
        json={"version": 2, "kind": "new_work", "request": {}},
    ).status_code == 401
    assert app_client.post(
        "/api/plugins/kanban/work-inbox/other?board=strict",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={},
    ).status_code == 401


def test_new_work_delegates_to_existing_intake(app_client, strict_board, strong_secret):
    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={strict_board}",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={
            "version": 2,
            "kind": "new_work",
            "request": {"functional_intent": {"title": "Small change"}},
            "session_id": "external-1",
            "attachments": [],
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "qualification_required"
    with kb.connect(board=strict_board) as conn:
        assert len(kb.list_qualification_intakes(conn, status="pending")) == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_new_work_patch_evidence_creates_only_pending_intake(
    app_client, strict_board, strong_secret,
):
    patch = "diff --git a/app.py b/app.py\n+@@ -1 +1 @@\n-old\n+new\n"
    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={strict_board}",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={
            "version": 2,
            "kind": "new_work",
            "request": {
                "functional_intent": {"title": "Review existing patch"},
                "evidence": [{"kind": "patch", "content": patch}],
            },
        },
    )

    assert response.status_code == 202
    with kb.connect(board=strict_board) as conn:
        intakes = kb.list_qualification_intakes(conn, status="pending")
        assert len(intakes) == 1
        assert json.loads(intakes[0]["raw_request"])["request"]["evidence"] == [
            {"kind": "patch", "content": patch}
        ]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_exact_assigned_completion_uses_normal_handover(
    app_client, strict_board, strong_secret, tmp_path,
):
    board, task_id, run_id, contract_id = _contracted_running_card(strict_board, tmp_path)

    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={board}",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={
            "version": 2,
            "kind": "assigned_delivery",
            "task_id": task_id,
            "run_id": run_id,
            "work_contract_id": contract_id,
            "outcome": "completed",
            "summary": "Delivered the assigned change",
            "metadata": {"ai_provenance": {"writer": {"agent": "external"}}},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "handover_applied"
    with kb.connect(board=board) as conn:
        assert kb.get_task(conn, task_id).current_step_key == "test"


def test_exact_assigned_blocking_uses_normal_handover(
    app_client, strict_board, strong_secret, tmp_path,
):
    board, task_id, run_id, contract_id = _contracted_running_card(strict_board, tmp_path)

    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={board}",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={
            "version": 2, "kind": "assigned_delivery", "task_id": task_id,
            "run_id": run_id, "work_contract_id": contract_id,
            "outcome": "blocked", "summary": "Need a human decision",
            "block_kind": "needs_input", "attempted_resolutions": ["checked brief"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "handover_applied"
    with kb.connect(board=board) as conn:
        assert kb.get_run(conn, run_id).ended_at is not None


@pytest.mark.parametrize(
    "sql, params",
    [
        ("UPDATE tasks SET work_item_kind = 'epic' WHERE id = ?", lambda task_id: (task_id,)),
        ("UPDATE tasks SET goal_mode = 1 WHERE id = ?", lambda task_id: (task_id,)),
        ("UPDATE tasks SET current_step_key = 'release_measure' WHERE id = ?", lambda task_id: (task_id,)),
        ("UPDATE tasks SET status = 'ready' WHERE id = ?", lambda task_id: (task_id,)),
    ],
)
def test_ineligible_assigned_delivery_does_not_mutate_task_or_events(
    app_client, strict_board, strong_secret, tmp_path, sql, params,
):
    board, task_id, run_id, contract_id = _contracted_running_card(strict_board, tmp_path)
    with kb.connect(board=board) as conn:
        with kb.authorized_governance_write():
            conn.execute(sql, params(task_id))
        before = kb.get_task(conn, task_id)
        before_events = len(kb.list_events(conn, task_id))

    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={board}",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={
            "version": 2, "kind": "assigned_delivery", "task_id": task_id,
            "run_id": run_id, "work_contract_id": contract_id,
            "outcome": "completed", "summary": "must not apply",
        },
    )

    assert response.status_code == 409
    with kb.connect(board=board) as conn:
        assert kb.get_task(conn, task_id) == before
        assert len(kb.list_events(conn, task_id)) == before_events


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(run_id=999999),
        lambda body: body.update(work_contract_id="wc_wrong"),
        lambda body: body.update(metadata={"private": "no"}),
        lambda body: body.update(unexpected=True),
    ],
)
def test_rejected_assigned_delivery_does_not_mutate_task_or_events(
    app_client, strict_board, strong_secret, mutate, tmp_path,
):
    board, task_id, run_id, contract_id = _contracted_running_card(strict_board, tmp_path)
    with kb.connect(board=board) as conn:
        before = kb.get_task(conn, task_id)
        before_events = len(kb.list_events(conn, task_id))
    body = _assigned_completion_body(task_id, run_id, contract_id)
    mutate(body)

    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={board}",
        headers={"Authorization": f"Bearer {strong_secret}"}, json=body,
    )

    assert response.status_code in {409, 422}
    with kb.connect(board=board) as conn:
        after = kb.get_task(conn, task_id)
        assert after == before
        assert len(kb.list_events(conn, task_id)) == before_events


def test_task_mismatched_assignment_does_not_mutate_task_or_events(
    app_client, strict_board, strong_secret, tmp_path,
):
    board, task_id, run_id, contract_id = _contracted_running_card(strict_board, tmp_path)
    _, other_task_id, _, other_contract_id = _contracted_running_card(strict_board, tmp_path)
    with kb.connect(board=board) as conn:
        before = kb.get_task(conn, other_task_id)
        before_events = len(kb.list_events(conn, other_task_id))

    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={board}",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json=_assigned_completion_body(other_task_id, run_id, other_contract_id),
    )

    assert response.status_code == 409
    with kb.connect(board=board) as conn:
        assert kb.get_task(conn, other_task_id) == before
        assert len(kb.list_events(conn, other_task_id)) == before_events
        assert kb.get_task(conn, task_id).current_run_id == run_id
        assert kb.get_task(conn, task_id).work_contract_id == contract_id


def test_ended_run_assignment_does_not_mutate_task_or_events(
    app_client, strict_board, strong_secret, tmp_path,
):
    board, task_id, run_id, contract_id = _contracted_running_card(strict_board, tmp_path)
    with kb.connect(board=board) as conn:
        assert kb.reclaim_task(conn, task_id, reason="test ended run")
        before = kb.get_task(conn, task_id)
        before_events = len(kb.list_events(conn, task_id))

    response = app_client.post(
        f"/api/plugins/kanban/work-inbox?board={board}",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json=_assigned_completion_body(task_id, run_id, contract_id),
    )

    assert response.status_code == 409
    with kb.connect(board=board) as conn:
        assert kb.get_task(conn, task_id) == before
        assert len(kb.list_events(conn, task_id)) == before_events
