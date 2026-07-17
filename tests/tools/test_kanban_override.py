from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_qualifier as qualifier
from hermes_cli.kanban_intake import WorkContractError
from tools.kanban_tools import KANBAN_CREATE_SCHEMA


def _policy():
    return {
        "preset": "product",
        "qualification": {
            "required": True,
            "contract_version": 1,
            "policy_version": "product-handoff-v2+qualification-v1",
            "paths": ["po", "hermes"],
            "work_types": ["maintenance"],
            "phase_assignees": {
                "backlog": "productowner",
                "architecture": "architect",
                "development": "developer",
                "test": "tester",
                "review": "reviewer",
                "release_measure": None,
            },
        },
    }


def _decision():
    return {
        "qualification_path": "hermes",
        "work": {
            "item_kind": "card",
            "work_type": "maintenance",
            "title": "Recover approved work",
            "outcome": "The approved work is governed and executable",
            "scope": ["Hermes"],
            "out_of_scope": [],
        },
        "routing": {
            "entry_phase": "backlog",
            "assignee": "productowner",
            "epic_id": None,
            "dependencies": [],
        },
        "entry_assessment": {
            "reason": "Start at the first governed phase",
            "skipped_phases": [],
            "evidence": [],
        },
        "handover": {
            "deliverables": ["implementation"],
            "required_evidence": ["tests"],
            "done_when": ["green"],
            "next_phase": "architecture",
            "next_role": "architect",
        },
        "rules": {"allowed": ["scoped work"], "forbidden": ["fabricate evidence"]},
        "classification": ["path:override"],
    }


def test_override_is_not_exposed_on_ordinary_kanban_create_schema():
    properties = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "override" not in properties
    assert "qualification_path" not in properties


def test_override_rejects_forged_or_incomplete_authority(tmp_path, monkeypatch):
    conn = kb.connect(tmp_path / "kanban.db")
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request='{"request":{"title":"Recover approved work"}}',
        source="codex",
    )
    monkeypatch.setattr(kb, "read_board_metadata", lambda board: _policy())

    with pytest.raises(WorkContractError, match="authenticated Ole-to-Hermes"):
        qualifier.override_intake(
            conn,
            board="strict",
            intake_id=intake_id,
            authority=object(),
            model_call=lambda prompt: _decision(),
            secret=b"x" * 32,
        )


def test_authenticated_direct_ole_instruction_can_override_without_fake_evidence(
    tmp_path, monkeypatch
):
    conn = kb.connect(tmp_path / "kanban.db")
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request='{"request":{"title":"Recover approved work"}}',
        source="codex",
    )
    monkeypatch.setattr(kb, "read_board_metadata", lambda board: _policy())
    authority = qualifier._new_gateway_override_authority(
        intake_id=intake_id,
        instruction_text=f"Override {intake_id} because Ole approved recovery",
        reason="Ole approved recovery",
        source_session="session-1",
        instruction_ref="message-7",
    )

    result = qualifier.override_intake(
        conn,
        board="strict",
        intake_id=intake_id,
        authority=authority,
        model_call=lambda prompt: _decision(),
        secret=b"x" * 32,
        issued_at=10,
    )

    assert result["status"] == "overridden"
    task = kb.get_task(conn, result["task_id"])
    assert task is not None
    decisions = kb.list_qualification_decisions(conn, intake_id)
    assert decisions[-1]["decision"] == "overridden"
    assert "session-1" in (decisions[-1]["reason"] or "")
    contract = kb.get_work_contract(conn, task.work_contract_id)
    authority_record = contract["contract"]["override_authority"]
    assert authority_record == {
        "reason": "Ole approved recovery",
        "source_session": "session-1",
        "instruction_ref": "message-7",
    }
    assert "test" not in authority_record
    assert "review" not in authority_record
    assert "release" not in authority_record
