from __future__ import annotations

import json
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

    contract_id = None
    if decision != "rejected":
        contract_id = kb.store_work_contract(
            conn,
            _signed_contract(intake_id),
            secret=b"test-only-secret",
            created_at=105,
        )
    kb.record_qualification_decision(
        conn,
        intake_id=intake_id,
        decision=decision,
        actor_profile="hermes",
        reason="policy applied",
        contract_id=contract_id,
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


def test_qualified_decision_requires_the_matching_contract(conn):
    intake_id = kb.create_qualification_intake(conn, raw_request="one", source="chat")
    other_id = kb.create_qualification_intake(conn, raw_request="two", source="chat")
    other_contract = kb.store_work_contract(
        conn,
        _signed_contract(other_id),
        secret=b"test-only-secret",
    )

    with pytest.raises(ValueError, match="matching Work Contract"):
        kb.record_qualification_decision(
            conn,
            intake_id=intake_id,
            decision="qualified",
            actor_profile="hermes",
        )
    with pytest.raises(ValueError, match="does not belong"):
        kb.record_qualification_decision(
            conn,
            intake_id=intake_id,
            decision="overridden",
            actor_profile="hermes",
            contract_id=other_contract,
        )


def test_work_contract_must_reference_an_existing_intake(conn):
    with pytest.raises(ValueError, match="unknown qualification intake"):
        kb.store_work_contract(
            conn,
            _signed_contract("qi_missing"),
            secret=b"test-only-secret",
        )


def test_raw_intake_and_attachments_are_immutable(conn):
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request="original",
        source="chat",
        attachments=[{"name": "brief.pdf"}],
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE qualification_intake SET raw_request = 'rewritten' WHERE id = ?",
            (intake_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM qualification_intake WHERE id = ?", (intake_id,))


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


def test_strict_board_rejects_direct_task_insert_and_materializes_atomically(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    path_connection = kb.connect(db_path=kb.kanban_db_path(board="strict"))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="qualification"):
            kb.create_task(path_connection, title="explicit path bypass")
    finally:
        path_connection.close()

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="strict")))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    env_connection = kb.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="qualification"):
            kb.create_task(env_connection, title="environment path bypass")
    finally:
        env_connection.close()

    connection = kb.connect(board="strict")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="qualification"):
            connection.execute(
                "INSERT INTO tasks (id, title, status, created_at) "
                "VALUES ('t_direct', 'bypass', 'ready', 1)"
            )

        request_id = kb.create_qualification_intake(
            connection, raw_request="qualified request", source="hermes"
        )
        signed = _signed_contract(request_id)
        task_id = intake.materialize_contract(
            connection,
            board="strict",
            signed_contract=signed,
            secret=b"test-only-secret",
        )
        task = kb.get_task(connection, task_id)

        assert task is not None
        assert task.work_contract_id is not None
        assert task.work_item_kind == "card"
        assert task.workflow_template_id == "product"
        assert task.current_step_key == "development"
        assert task.assignee == "developer"
        assert kb.get_qualification_intake(connection, request_id)["status"] == "qualified"
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="Work Contract-owned"):
            connection.execute(
                "UPDATE tasks SET assignee = 'reviewer', current_step_key = 'review' "
                "WHERE id = ?",
                (task_id,),
            )
        assert kb.set_phase(connection, task_id, "test", board="strict")
        assert kb.get_task(connection, task_id).current_step_key == "test"
        with pytest.raises(sqlite3.IntegrityError, match="Work Contract-owned"):
            connection.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (task_id, task_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="Work Contract-owned"):
            connection.execute(
                "INSERT INTO epic_memberships (epic_id, task_id, created_at) "
                "VALUES (?, ?, 1)",
                (task_id, task_id),
            )
        assert (
            intake.materialize_contract(
                connection,
                board="strict",
                signed_contract=signed,
                secret=b"test-only-secret",
            )
            == task_id
        )
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        connection.close()


def test_epic_contract_materializes_as_non_executable_container(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    connection = kb.connect(board="strict")
    try:
        request_id = kb.create_qualification_intake(
            connection, raw_request="epic outcome", source="hermes"
        )
        contract = _signed_contract(request_id)["contract"]
        contract["work"]["item_kind"] = "epic"
        contract["work"]["title"] = "Epic: qualification outcome"
        contract["routing"] = {
            "entry_phase": None,
            "assignee": None,
            "epic_id": None,
            "dependencies": [],
        }
        signed = intake.sign_work_contract(contract, secret=b"test-only-secret")

        epic_id = intake.materialize_contract(
            connection,
            board="strict",
            signed_contract=signed,
            secret=b"test-only-secret",
        )
        epic = kb.get_task(connection, epic_id)

        assert epic.work_item_kind == "epic"
        assert epic.status == "todo"
        assert epic.assignee is None
        assert epic.workflow_template_id is None
        assert epic.current_step_key is None
        assert kb.claim_task(connection, epic_id, board="strict") is None
        assert kb.list_runs(connection, epic_id) == []
    finally:
        connection.close()


def test_materialization_rolls_back_contract_and_decision_on_invalid_relationship(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    connection = kb.connect(board="strict")
    try:
        request_id = kb.create_qualification_intake(
            connection, raw_request="bad dependency", source="hermes"
        )
        contract = _signed_contract(request_id)["contract"]
        contract["routing"]["dependencies"] = ["t_missing"]
        signed = intake.sign_work_contract(contract, secret=b"test-only-secret")

        with pytest.raises(ValueError, match="unknown parent"):
            intake.materialize_contract(
                connection,
                board="strict",
                signed_contract=signed,
                secret=b"test-only-secret",
            )

        assert kb.get_qualification_intake(connection, request_id)["status"] == "pending"
        assert kb.list_qualification_decisions(connection, request_id) == []
        assert connection.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        connection.close()
