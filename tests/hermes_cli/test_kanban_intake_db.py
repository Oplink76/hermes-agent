from __future__ import annotations

import json
import sqlite3
import subprocess

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
            "entry_assessment": {
                "reason": "Earlier phases are already satisfied",
                "skipped_phases": [
                    {
                        "phase": "backlog",
                        "reason": "backlog evidence exists",
                        "evidence": ["backlog-artifact"],
                    },
                    {
                        "phase": "architecture",
                        "reason": "architecture evidence exists",
                        "evidence": ["architecture-artifact"],
                    },
                ],
                "evidence": ["backlog-artifact", "architecture-artifact"],
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


def _strict_product_board(tmp_path, monkeypatch, board: str) -> None:
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults(board)
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def _strict_v2_product_board(tmp_path, monkeypatch, board: str) -> None:
    _strict_product_board(tmp_path, monkeypatch, board)
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("product_workflow", {})["handoff_v2"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def _materialized_card(connection, board: str) -> str:
    request_id = kb.create_qualification_intake(
        connection,
        raw_request=json.dumps(
            {
                "kind": "task_create",
                "request": {
                    "title": "Qualified card",
                    "evidence": ["backlog-artifact", "architecture-artifact"],
                },
            }
        ),
        source="hermes",
        attachments=[
            {"name": "backlog-artifact"},
            {"name": "architecture-artifact"},
        ],
    )
    task_id = intake.materialize_contract(
        connection,
        board=board,
        signed_contract=_signed_contract(request_id),
        secret=b"test-only-secret",
    )
    return task_id


def _materialized_scheduled_card(connection, board: str) -> str:
    task_id = _materialized_card(connection, board)
    assert kb.schedule_task(connection, task_id, reason="no wake action")
    return task_id


def _materialized_epic(connection, board: str) -> str:
    request_id = kb.create_qualification_intake(
        connection,
        raw_request=json.dumps(
            {"kind": "task_create", "request": {"title": "Qualified Epic"}}
        ),
        source="hermes",
    )
    contract = _signed_contract(request_id)["contract"]
    contract["work"]["item_kind"] = "epic"
    contract["work"]["title"] = "Qualified Epic"
    contract["routing"] = {
        "entry_phase": None,
        "assignee": None,
        "epic_id": None,
        "dependencies": [],
    }
    contract["handover"]["next_phase"] = None
    contract["handover"]["next_role"] = None
    signed = intake.sign_work_contract(contract, secret=b"test-only-secret")
    return intake.materialize_contract(
        connection,
        board=board,
        signed_contract=signed,
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
            connection,
            raw_request="qualified request",
            source="hermes",
            attachments=[
                {"name": "backlog-artifact"},
                {"name": "architecture-artifact"},
            ],
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
        with pytest.raises(PermissionError, match="strict-board"):
            kb.delete_task(connection, task_id)
        with pytest.raises(sqlite3.IntegrityError, match="strict-board"):
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        with pytest.raises(PermissionError, match="strict-board"):
            kb.archive_task(connection, task_id)
        with pytest.raises(sqlite3.IntegrityError, match="strict-board"):
            connection.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?", (task_id,)
            )
        with kb.authorized_governance_write():
            assert kb.archive_task(connection, task_id)
        with pytest.raises(PermissionError, match="strict-board"):
            kb.delete_archived_task(connection, task_id)
    finally:
        connection.close()


def test_handoff_v2_materializes_executable_card_with_canonical_worktree(
    tmp_path, monkeypatch
):
    board = "strict-v2-worktree"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    connection = kb.connect(board=board)
    try:
        task_id = _materialized_card(connection, board)
        task = kb.get_task(connection, task_id)

        assert task.workspace_kind == "worktree"
        assert task.workspace_path is None
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
            connection,
            raw_request="bad dependency",
            source="hermes",
            attachments=[
                {"name": "backlog-artifact"},
                {"name": "architecture-artifact"},
            ],
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


def test_materialization_revalidates_late_entry_evidence_before_writing(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults("strict")
    with kb.connect(board="strict") as legacy_connection:
        unrelated = kb.create_task(
            legacy_connection, title="Unrelated evidence holder"
        )
        legacy_connection.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'tester', 'backlog-artifact architecture-artifact', 1)",
            (unrelated,),
        )
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    connection = kb.connect(board="strict")
    try:
        request_id = kb.create_qualification_intake(
            connection, raw_request="late entry", source="hermes"
        )
        contract = _signed_contract(request_id)["contract"]
        contract["entry_assessment"] = {
            "reason": "Earlier phases are claimed complete",
            "skipped_phases": [
                {
                    "phase": "backlog",
                    "reason": "claimed evidence",
                    "evidence": ["backlog-artifact"],
                },
                {
                    "phase": "architecture",
                    "reason": "claimed evidence",
                    "evidence": ["architecture-artifact"],
                },
            ],
            "evidence": ["backlog-artifact", "architecture-artifact"],
        }
        signed = intake.sign_work_contract(contract, secret=b"test-only-secret")

        with pytest.raises(intake.WorkContractError, match="not grounded"):
            intake.materialize_contract(
                connection,
                board="strict",
                signed_contract=signed,
                secret=b"test-only-secret",
            )

        assert kb.get_qualification_intake(connection, request_id)["status"] == "pending"
        assert connection.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        connection.close()


def test_materialization_revalidates_product_owner_evidence_for_epics(
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
            connection, raw_request="PO Epic", source="productowner"
        )
        contract = _signed_contract(request_id)["contract"]
        contract["qualification_path"] = "po"
        contract["work"]["item_kind"] = "epic"
        contract["routing"] = {
            "entry_phase": None,
            "assignee": None,
            "epic_id": None,
            "dependencies": [],
        }
        contract["handover"]["next_phase"] = None
        contract["handover"]["next_role"] = None
        contract["po_evidence"] = {"run_id": 999, "artifact": "brief.md"}
        signed = intake.sign_work_contract(contract, secret=b"test-only-secret")

        with pytest.raises(intake.WorkContractError, match="Product Owner run"):
            intake.materialize_contract(
                connection,
                board="strict",
                signed_contract=signed,
                secret=b"test-only-secret",
            )

        assert connection.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        connection.close()


def test_requalification_intake_requires_hermes_service_authority(
    tmp_path, monkeypatch
):
    board = "strict-requalification-authority"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        raw_request = json.dumps(
            {"kind": "task_requalification", "target_task_id": task_id}
        )

        with pytest.raises(sqlite3.IntegrityError, match="Hermes service authority"):
            kb.create_qualification_intake(
                connection,
                raw_request=raw_request,
                source="codex",
            )


def test_requalification_authority_rejects_duplicate_kind_keys(
    tmp_path, monkeypatch
):
    board = "strict-requalification-duplicate-kind"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        raw_request = (
            '{"kind":"task_create","kind":"task_requalification",'
            f'"target_task_id":"{task_id}"}}'
        )

        with pytest.raises(sqlite3.IntegrityError, match="Hermes service authority"):
            kb.create_qualification_intake(
                connection,
                raw_request=raw_request,
                source="codex",
            )


def test_submit_requalification_is_inert_durable_and_idempotent(
    tmp_path, monkeypatch
):
    board = "strict-requalification-intake"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)

        first = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="qualified scheduled work has no wake action",
        )
        second = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="qualified scheduled work has no wake action",
        )

        assert first["created"] is True
        assert second["created"] is False
        assert first["intake_id"] == second["intake_id"]
        assert kb.get_task(connection, task_id).status == "scheduled"
        pending = kb.list_qualification_intakes(connection, status="pending")
        assert [record["id"] for record in pending] == [first["intake_id"]]
        payload = intake.intake_payload(
            kb.get_qualification_intake(connection, first["intake_id"])
        )
        assert payload["kind"] == "task_requalification"
        assert payload["target_task_id"] == task_id
        assert payload["reason"] == "qualified scheduled work has no wake action"


def test_submit_requalification_ignores_legacy_non_json_intake(
    tmp_path, monkeypatch
):
    board = "strict-requalification-legacy-intake"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        kb.create_qualification_intake(
            connection,
            raw_request="legacy opaque request",
            source="legacy",
        )
        task_id = _materialized_scheduled_card(connection, board)

        receipt = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="resume through the governed flow",
        )

        assert receipt["task_id"] == task_id
        assert receipt["intake_status"] == "pending"


def test_requalification_captures_complete_history_and_repository_state(
    tmp_path, monkeypatch
):
    board = "strict-requalification-evidence"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        for number in range(51):
            kb.add_comment(connection, task_id, "test", f"history-{number}")
        with kb.authorized_governance_write():
            connection.execute(
                "UPDATE tasks SET workspace_kind = 'dir', workspace_path = ?, "
                "branch_name = 'feature/evidence' WHERE id = ?",
                (str(tmp_path), task_id),
            )

        receipt = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="qualify from complete current evidence",
        )
        payload = intake.intake_payload(
            kb.get_qualification_intake(connection, receipt["intake_id"])
        )

        assert len(payload["evidence"]["comments"]) == 51
        assert payload["evidence"]["repository"] == {
            "branch_name": "feature/evidence",
            "project_id": None,
            "workspace_kind": "dir",
            "workspace_path": str(tmp_path),
            "available": False,
        }


def test_requalification_captures_current_git_head_and_status(tmp_path, monkeypatch):
    board = "strict-requalification-git-evidence"
    _strict_product_board(tmp_path, monkeypatch, board)
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    (repository / "README.md").write_text("evidence\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Hermes Test",
            "-c",
            "user.email=hermes@example.invalid",
            "commit",
            "-m",
            "test evidence",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        with kb.authorized_governance_write():
            connection.execute(
                "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ?, "
                "branch_name = 'main' WHERE id = ?",
                (str(repository), task_id),
            )
        receipt = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="capture repository evidence",
        )
        payload = intake.intake_payload(
            kb.get_qualification_intake(connection, receipt["intake_id"])
        )

        repository_evidence = payload["evidence"]["repository"]
        assert repository_evidence["available"] is True
        assert repository_evidence["head"] == head
        assert repository_evidence["current_branch"] == "main"
        assert repository_evidence["status_porcelain"] == []


def test_repository_evidence_is_unavailable_when_git_is_not_installed(
    tmp_path, monkeypatch
):
    board = "strict-requalification-no-git"
    _strict_product_board(tmp_path, monkeypatch, board)
    monkeypatch.setattr(kb.shutil, "which", lambda _name: None)

    def unexpected_subprocess(*_args, **_kwargs):
        pytest.fail("repository evidence must not shell out when git is unavailable")

    monkeypatch.setattr(kb.subprocess, "run", unexpected_subprocess)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        with kb.authorized_governance_write():
            connection.execute(
                "UPDATE tasks SET workspace_kind = 'dir', workspace_path = ? "
                "WHERE id = ?",
                (str(tmp_path), task_id),
            )
        receipt = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="capture portable repository evidence",
        )
        payload = intake.intake_payload(
            kb.get_qualification_intake(connection, receipt["intake_id"])
        )

        assert payload["evidence"]["repository"]["available"] is False


def test_successor_contract_requalifies_same_card_and_preserves_audit(
    tmp_path, monkeypatch
):
    board = "strict-requalification-apply"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        before_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        old_contract_id = kb.get_task(connection, task_id).work_contract_id
        receipt = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="resume through the governed flow",
        )
        contract = _signed_contract(receipt["intake_id"])["contract"]
        contract["work"]["title"] = "Requalified card"
        contract["work"]["outcome"] = "The same card resumes safely"
        successor = intake.sign_work_contract(
            contract, secret=b"test-only-secret"
        )

        materialized_id = intake.materialize_contract(
            connection,
            board=board,
            signed_contract=successor,
            secret=b"test-only-secret",
        )
        repeated_id = intake.materialize_contract(
            connection,
            board=board,
            signed_contract=successor,
            secret=b"test-only-secret",
        )

        card = kb.get_task(connection, task_id)
        assert materialized_id == repeated_id == task_id
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before_count
        assert card.title == "Requalified card"
        assert card.body == "The same card resumes safely"
        assert card.current_step_key == "development"
        assert card.assignee == "developer"
        assert card.status == "ready"
        assert card.work_contract_id != old_contract_id
        assert kb.get_work_contract(connection, old_contract_id) is not None
        event = [
            item
            for item in kb.list_events(connection, task_id)
            if item.kind == "requalified"
        ][-1]
        assert event.payload == {
            "intake_id": receipt["intake_id"],
            "old_work_contract_id": old_contract_id,
            "new_work_contract_id": card.work_contract_id,
            "entry_phase": "development",
        }


def test_requalification_replaces_dependencies_and_epic_membership(
    tmp_path, monkeypatch
):
    board = "strict-requalification-relationships"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        target_id = _materialized_scheduled_card(connection, board)
        unfinished_parent_id = _materialized_card(connection, board)
        epic_id = _materialized_epic(connection, board)
        receipt = intake.submit_requalification(
            connection,
            task_id=target_id,
            reason="replace sequencing with dependencies",
        )
        contract = _signed_contract(receipt["intake_id"])["contract"]
        contract["routing"]["dependencies"] = [unfinished_parent_id]
        contract["routing"]["epic_id"] = epic_id
        successor = intake.sign_work_contract(
            contract, secret=b"test-only-secret"
        )

        assert (
            intake.materialize_contract(
                connection,
                board=board,
                signed_contract=successor,
                secret=b"test-only-secret",
            )
            == target_id
        )

        assert kb.parent_ids(connection, target_id) == [unfinished_parent_id]
        assert kb.epic_id_for_task(connection, target_id) == epic_id
        assert kb.get_task(connection, target_id).status == "todo"


def test_requalification_treats_archived_dependency_as_satisfied(
    tmp_path, monkeypatch
):
    board = "strict-requalification-archived-parent"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        target_id = _materialized_scheduled_card(connection, board)
        parent_id = _materialized_card(connection, board)
        with kb.authorized_governance_write():
            assert kb.archive_task(connection, parent_id) is True
        receipt = intake.submit_requalification(
            connection,
            task_id=target_id,
            reason="derive state from normal dependency rules",
        )
        contract = _signed_contract(receipt["intake_id"])["contract"]
        contract["routing"]["dependencies"] = [parent_id]

        intake.materialize_contract(
            connection,
            board=board,
            signed_contract=intake.sign_work_contract(
                contract, secret=b"test-only-secret"
            ),
            secret=b"test-only-secret",
        )

        assert kb.get_task(connection, target_id).status == "ready"


def test_requalification_uses_autonomous_release_dependency_policy(
    tmp_path, monkeypatch
):
    board = "strict-requalification-release-parent"
    _strict_product_board(tmp_path, monkeypatch, board)
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("product_workflow", {})[
        "release_measure_unblocks_dependents"
    ] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with kb.connect(board=board) as connection:
        target_id = _materialized_scheduled_card(connection, board)
        parent_id = _materialized_card(connection, board)
        with kb.authorized_governance_write():
            connection.execute(
                "UPDATE tasks SET status = 'ready', current_step_key = "
                "'release_measure', assignee = NULL WHERE id = ?",
                (parent_id,),
            )
        receipt = intake.submit_requalification(
            connection,
            task_id=target_id,
            reason="follow the board's dependency policy",
        )
        contract = _signed_contract(receipt["intake_id"])["contract"]
        contract["routing"]["dependencies"] = [parent_id]

        intake.materialize_contract(
            connection,
            board=board,
            signed_contract=intake.sign_work_contract(
                contract, secret=b"test-only-secret"
            ),
            secret=b"test-only-secret",
        )

        assert kb.get_task(connection, target_id).status == "ready"


def test_requalification_rejects_break_glass_and_rolls_back(
    tmp_path, monkeypatch
):
    board = "strict-requalification-no-override"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        receipt = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="ordinary requalification",
        )
        old_contract_id = kb.get_task(connection, task_id).work_contract_id
        before_contract_count = connection.execute(
            "SELECT COUNT(*) FROM work_contracts"
        ).fetchone()[0]
        contract = _signed_contract(receipt["intake_id"])["contract"]
        contract["qualification_path"] = "override"
        contract["override_authority"] = {
            "reason": "not ordinary requalification",
            "source_session": "session-1",
            "instruction_ref": "message-1",
        }
        signed = intake.sign_work_contract(
            contract, secret=b"test-only-secret"
        )

        with pytest.raises(intake.WorkContractError, match="break-glass override"):
            intake.materialize_contract(
                connection,
                board=board,
                signed_contract=signed,
                secret=b"test-only-secret",
            )

        assert kb.get_task(connection, task_id).work_contract_id == old_contract_id
        assert kb.get_qualification_intake(
            connection, receipt["intake_id"]
        )["status"] == "pending"
        assert (
            connection.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0]
            == before_contract_count
        )


def test_reconcile_requests_one_scheduled_requalification_per_pass(
    tmp_path, monkeypatch
):
    board = "strict-v2-requalification-bounded"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        first_id = _materialized_scheduled_card(connection, board)
        second_id = _materialized_scheduled_card(connection, board)
        connection.execute(
            "UPDATE tasks SET priority = 10 WHERE id = ?", (first_id,)
        )

        result = kb.reconcile(connection, board=board, spawn_ready=False)

        assert result.requalification_requested == [first_id]
        pending = kb.list_qualification_intakes(connection, status="pending")
        assert len(pending) == 1
        assert intake.intake_payload(pending[0])["target_task_id"] == first_id
        assert kb.get_task(connection, second_id).status == "scheduled"


def test_reconcile_does_not_duplicate_pending_requalification(
    tmp_path, monkeypatch
):
    board = "strict-v2-requalification-idempotent"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)

        first = kb.reconcile(connection, board=board, spawn_ready=False)
        second = kb.reconcile(connection, board=board, spawn_ready=False)

        assert first.requalification_requested == [task_id]
        assert second.requalification_requested == []
        pending = kb.list_qualification_intakes(connection, status="pending")
        assert len(pending) == 1
        assert intake.intake_payload(pending[0])["target_task_id"] == task_id


def test_reconcile_does_not_retry_rejected_requalification(
    tmp_path, monkeypatch
):
    board = "strict-v2-requalification-rejected"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        first = kb.reconcile(connection, board=board, spawn_ready=False)
        intake_id = kb.list_qualification_intakes(connection, status="pending")[0]["id"]
        kb.record_qualification_decision(
            connection,
            intake_id=intake_id,
            decision="rejected",
            actor_profile="hermes",
            reason="requires operator attention",
        )

        second = kb.reconcile(connection, board=board, spawn_ready=False)
        kb.add_comment(connection, task_id, "operator", "New evidence is available")
        third = kb.reconcile(connection, board=board, spawn_ready=False)

        assert first.requalification_requested == [task_id]
        assert second.requalification_requested == []
        assert third.requalification_requested == [task_id]
        records = [
            record
            for record in kb.list_qualification_intakes(connection)
            if intake.requalification_target_id(record) == task_id
        ]
        assert len(records) == 2
        assert sorted(record["status"] for record in records) == ["pending", "rejected"]


def test_reconcile_retries_rejected_requalification_after_qualifier_revision_change(
    tmp_path, monkeypatch
):
    board = "strict-v2-requalification-new-qualifier"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        first = kb.reconcile(connection, board=board, spawn_ready=False)
        first_intake = kb.list_qualification_intakes(
            connection, status="pending"
        )[0]
        kb.record_qualification_decision(
            connection,
            intake_id=first_intake["id"],
            decision="rejected",
            actor_profile="hermes",
            reason="old qualifier contract",
        )
        monkeypatch.setattr(
            intake,
            "REQUALIFICATION_QUALIFIER_REVISION",
            intake.REQUALIFICATION_QUALIFIER_REVISION + 1,
        )

        second = kb.reconcile(connection, board=board, spawn_ready=False)

        assert first.requalification_requested == [task_id]
        assert second.requalification_requested == [task_id]
        pending = kb.list_qualification_intakes(connection, status="pending")
        assert len(pending) == 1
        assert (
            intake.intake_payload(pending[0])["qualifier_revision"]
            == intake.REQUALIFICATION_QUALIFIER_REVISION
        )


def test_reconcile_leaves_scheduled_card_with_unresolved_blocker_untouched(
    tmp_path, monkeypatch
):
    board = "strict-v2-requalification-blocked"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        with kb.authorized_governance_write():
            connection.execute(
                "UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?",
                (task_id,),
            )
            kb._append_event(
                connection,
                task_id,
                "blocked",
                {"reason": "waiting for explicit operator input"},
            )

        result = kb.reconcile(connection, board=board, spawn_ready=False)

        assert result.requalification_requested == []
        assert kb.list_qualification_intakes(connection, status="pending") == []


def test_submit_requalification_rejects_an_unresolved_blocker(
    tmp_path, monkeypatch
):
    board = "strict-requalification-direct-blocked"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        with kb.authorized_governance_write():
            kb._append_event(
                connection,
                task_id,
                "blocked",
                {"reason": "waiting for explicit operator input"},
            )

        with pytest.raises(intake.WorkContractError, match="unresolved blocker"):
            intake.submit_requalification(
                connection,
                task_id=task_id,
                reason="must not bypass the blocker",
            )


@pytest.mark.parametrize("status", ["ready", "todo", "blocked", "done"])
def test_reconcile_leaves_non_scheduled_qualified_work_untouched(
    tmp_path, monkeypatch, status
):
    board = f"strict-v2-requalification-{status}"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        connection.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
        )

        result = kb.reconcile(connection, board=board, spawn_ready=False)

        assert result.requalification_requested == []
        assert kb.list_qualification_intakes(connection, status="pending") == []


def test_reconcile_leaves_release_measure_to_release_evidence_policy(
    tmp_path, monkeypatch
):
    board = "strict-v2-requalification-release"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        with kb.authorized_governance_write():
            connection.execute(
                "UPDATE tasks SET current_step_key = 'release_measure', assignee = NULL "
                "WHERE id = ?",
                (task_id,),
            )

        result = kb.reconcile(connection, board=board, spawn_ready=False)

        assert result.requalification_requested == []
        assert kb.list_qualification_intakes(connection, status="pending") == []
