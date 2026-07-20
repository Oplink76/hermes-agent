"""Public instructions for submitting governed work to Hermes."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db, web_server
from hermes_cli.kanban_inbox_guide import build_inbox_guide


def test_guide_names_one_minimal_work_inbox_route():
    guide_url = "https://hermes.example/.well-known/hermes-inbox?board=strict"

    body = build_inbox_guide(
        board="strict",
        origin="https://hermes.example",
    )

    assert body["guide_version"] == 2
    assert body["name"] == "Hermes Work Inbox"
    assert body["board"] == "strict"
    assert body["qualification_required"] is True
    assert body["purpose"] == (
        "Submit Ole-approved local work for framework qualification or hand "
        "over an exact assigned delivery."
    )
    assert body["authority"]["allowed"][0] == (
        "submit Ole-approved work for framework processing"
    )
    prompt = body["copy_ready_prompt"]
    assert "local AI outside the Hermes-managed framework" in prompt
    assert "Ole has approved it to enter Hermes" in prompt
    assert "Admission does not authorize execution" in prompt
    assert "qualification and a signed Work Contract remain required" in prompt
    assert body["submission"]["method"] == "POST"
    assert body["submission"]["url"] == (
        "https://hermes.example/api/plugins/kanban/work-inbox?board=strict"
    )
    assert set(body["submission"]["kinds"]) == {"new_work", "assigned_delivery"}
    assert body["submission"]["scope"] == "work_inbox:submit"
    assert body["submission"]["authentication"] == {
        "required": True,
        "type": "bearer",
        "authorization_header": "Authorization: Bearer <machine credential>",
        "credential_included": False,
    }
    assert body["submission"]["examples"]["assigned_delivery"]["run_id"] == 123
    assert guide_url in body["copy_ready_prompt"]
    assert body["retry"]["automatic_retry"] is False
    assert "receipt" not in body
    serialized = json.dumps(body)
    assert "token" not in serialized.lower()
    assert "canonical_json" not in serialized
    assert "<assigned task id>" in serialized
    assert "<assigned Work Contract id>" in serialized
    assert str(get_hermes_home()) not in serialized


@pytest.fixture
def strict_board() -> str:
    kanban_db.ensure_product_board_defaults("strict")
    metadata_path = kanban_db.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    return "strict"


@pytest.fixture
def client():
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    yield TestClient(
        web_server.app,
        base_url="http://127.0.0.1:9119",
    )
    for key, value in previous.items():
        setattr(web_server.app.state, key, value)


def test_public_endpoint_returns_guide_without_dashboard_auth(client, strict_board):
    response = client.get(
        "/.well-known/hermes-inbox", params={"board": strict_board}
    )

    assert response.status_code == 200
    assert response.json()["board"] == strict_board
    assert web_server._SESSION_TOKEN not in response.text
    assert str(get_hermes_home()) not in response.text


def test_public_endpoint_bypasses_oauth_gate(strict_board):
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "hermes.example"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    try:
        gated_client = TestClient(
            web_server.app,
            base_url="https://hermes.example",
        )
        response = gated_client.get(
            "/.well-known/hermes-inbox", params={"board": strict_board}
        )
    finally:
        for key, value in previous.items():
            setattr(web_server.app.state, key, value)

    assert response.status_code == 200
    assert response.json()["board"] == strict_board


def test_public_endpoint_rejects_missing_board(client):
    assert client.get("/.well-known/hermes-inbox").status_code == 400


def test_public_endpoint_rejects_malformed_board(client):
    assert client.get(
        "/.well-known/hermes-inbox", params={"board": "../bad"}
    ).status_code == 400


def test_public_endpoint_rejects_unknown_board(client):
    response = client.get(
        "/.well-known/hermes-inbox", params={"board": "missing"}
    )
    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]


def test_public_endpoint_rejects_non_strict_board(client):
    kanban_db.create_board("plain")
    response = client.get(
        "/.well-known/hermes-inbox", params={"board": "plain"}
    )
    assert response.status_code == 409
