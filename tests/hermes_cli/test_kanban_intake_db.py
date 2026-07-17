from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake as intake


@pytest.fixture
def conn(tmp_path):
    connection = kb.connect(tmp_path / "kanban.db")
    try:
        yield connection
    finally:
        connection.close()


def _signed_contract(request_id: str = "qi_example"):
    return intake.sign_work_contract(
        {
            "version": 1,
            "policy_version": "product-handoff-v2+qualification-v1",
            "qualification_path": "hermes",
            "request_id": request_id,
            "work": {
                "item_kind": "card",
                "work_type": "story",
                "title": "Qualified card",
                "outcome": "safe execution",
                "scope": [],
                "out_of_scope": [],
            },
            "routing": {
                "entry_phase": "development",
                "assignee": "developer",
                "epic_id": None,
                "dependencies": [],
            },
            "handover": {
                "deliverables": [],
                "required_evidence": [],
                "done_when": [],
                "next_phase": "test",
                "next_role": "tester",
            },
            "rules": {"allowed": [], "forbidden": []},
            "classification": ["framework:story"],
            "issuer": {"profile": "hermes", "run_id": 42, "issued_at": 1_784_270_000},
        },
        secret=b"test-only-secret",
    )


def test_intake_submission_is_durable_and_inert(conn):
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    intake_id = kb.create_qualification_intake(
        conn,
        raw_request='{"original": "keep exactly"}',
        source="codex",
        session_id="session-123",
        attachments=[{"name": "brief.pdf", "path": "/tmp/brief.pdf"}],
        created_at=100,
    )
    record = kb.get_qualification_intake(conn, intake_id)

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    assert record == {
        "id": intake_id,
        "raw_request": '{"original": "keep exactly"}',
        "source": "codex",
        "session_id": "session-123",
        "attachments": [{"name": "brief.pdf", "path": "/tmp/brief.pdf"}],
        "status": "pending",
        "created_at": 100,
        "updated_at": 100,
    }


@pytest.mark.parametrize("decision", ["qualified", "rejected", "overridden"])
def test_terminal_intake_records_remain_queryable_with_append_only_audit(conn, decision):
    intake_id = kb.create_qualification_intake(
        conn, raw_request="do work", source="chat", created_at=100
    )

    kb.record_qualification_decision(
        conn,
        intake_id=intake_id,
        decision=decision,
        actor_profile="hermes",
        reason="policy applied",
        created_at=110,
    )

    assert kb.get_qualification_intake(conn, intake_id)["status"] == decision
    decisions = kb.list_qualification_decisions(conn, intake_id)
    assert [(row["decision"], row["actor_profile"], row["created_at"]) for row in decisions] == [
        (decision, "hermes", 110)
    ]
    assert kb.list_qualification_intakes(conn, status=decision)[0]["id"] == intake_id

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE qualification_intake_decisions SET reason = 'rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM qualification_intake_decisions")


def test_work_contract_storage_is_immutable_and_queryable(conn):
    intake_id = kb.create_qualification_intake(conn, raw_request="do work", source="chat")
    signed = _signed_contract(intake_id)

    contract_id = kb.store_work_contract(
        conn, signed, secret=b"test-only-secret", created_at=120
    )
    stored = kb.get_work_contract(conn, contract_id)

    assert stored["canonical_json"] == signed["canonical_json"]
    assert stored["digest"] == signed["digest"]
    assert stored["signature"] == signed["signature"]
    assert stored["issuer_profile"] == "hermes"
    assert stored["issuer_run_id"] == 42

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE work_contracts SET signature = 'rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM work_contracts")


def test_epic_membership_has_one_parent_per_child(conn):
    epic_a = kb.create_task(conn, title="Epic A")
    epic_b = kb.create_task(conn, title="Epic B")
    child = kb.create_task(conn, title="Story")
    conn.execute("UPDATE tasks SET work_item_kind = 'epic' WHERE id IN (?, ?)", (epic_a, epic_b))

    kb.add_epic_membership(conn, epic_id=epic_a, task_id=child)
    assert kb.list_epic_members(conn, epic_a) == [child]

    with pytest.raises(sqlite3.IntegrityError):
        kb.add_epic_membership(conn, epic_id=epic_b, task_id=child)
