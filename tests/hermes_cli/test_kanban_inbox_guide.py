"""Public instructions for submitting governed work to Hermes."""

from __future__ import annotations

import json

from hermes_cli.kanban_inbox_guide import build_inbox_guide


def test_build_inbox_guide_is_copy_ready_and_contains_no_credential():
    guide_url = "https://hermes.example/.well-known/hermes-inbox?board=strict"

    body = build_inbox_guide(
        board="strict",
        origin="https://hermes.example",
        guide_url=guide_url,
    )

    assert body["guide_version"] == 1
    assert body["board"] == "strict"
    assert body["submission"]["method"] == "POST"
    assert body["submission"]["url"] == (
        "https://hermes.example/api/plugins/kanban/intake?board=strict"
    )
    assert body["receipt"]["url_template"] == (
        "https://hermes.example/api/plugins/kanban/intake/{intake_id}?board=strict"
    )
    assert body["submission"]["example"]["request"]["client_request_id"]
    assert guide_url in body["copy_ready_prompt"]
    assert "Do not create, edit, assign, route, qualify, or override" in (
        body["copy_ready_prompt"]
    )
    serialized = json.dumps(body)
    assert "secret" not in serialized.lower()
    assert "canonical_json" not in serialized
