from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake as intake
from hermes_cli import projects_db as pdb


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


def _escalate_materialized_card(connection, board: str, task_id: str) -> None:
    ordinary_run = kb.claim_task(connection, task_id)
    assert ordinary_run is not None and ordinary_run.current_run_id is not None
    assert kb.block_task(
        connection,
        task_id,
        reason="Need a resolver decision",
        kind="needs_input",
        attempted_resolutions=["checked the governed route"],
        expected_run_id=ordinary_run.current_run_id,
        board=board,
        human_escalation_assignee="resolver",
    )
    resolver_run = kb.claim_task(connection, task_id)
    assert resolver_run is not None and resolver_run.current_run_id is not None
    task = kb.get_task(connection, task_id)
    preflight = [
        event
        for event in kb.list_events(connection, task_id)
        if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT
    ][-1]
    expected = {
        "run_id": resolver_run.current_run_id,
        "preflight_event_id": preflight.id,
        "status": task.status,
        "phase": task.current_step_key,
        "assignee": task.assignee,
        "project_id": task.project_id,
        "workflow_template_id": task.workflow_template_id,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "running": task.running,
        "blocked": task.blocked,
    }
    assert kb.resolve_product_preflight(
        connection,
        task_id,
        board=board,
        request={
            "decision": "escalate",
            "fault_domain": "task_state",
            "diagnosis": "The task requires human intervention",
            "reason": "Escalate through the canonical resolver path",
            "expected": expected,
        },
        resolver_profile="resolver",
        resolver_model="test-model",
    )
    escalated = kb.get_task(connection, task_id)
    assert escalated is not None
    assert escalated.status == "blocked"
    assert escalated.assignee == "default"


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


def test_unblock_restores_assignee_on_strict_materialized_card(
    tmp_path, monkeypatch
):
    board = "strict-unblock-restores-assignee"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_card(connection, board)
        _escalate_materialized_card(connection, board, task_id)

        assert kb.unblock_task(connection, task_id)
        task = kb.get_task(connection, task_id)

    assert task is not None
    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "developer"


def test_approve_unblock_restores_assignee_on_strict_materialized_card(
    tmp_path, monkeypatch
):
    board = "strict-approve-unblock-restores-assignee"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_card(connection, board)
        task = kb.get_task(connection, task_id)
        _escalate_materialized_card(connection, board, task_id)

        approved = kb.approve_unblock_task(
            connection,
            task_id,
            expected_status="blocked",
            expected_title=task.title,
            comment_author="operator",
        )

    assert approved is not None
    assert approved.status == "ready"
    assert approved.current_step_key == "development"
    assert approved.assignee == "developer"


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


def test_identical_intake_submission_is_idempotent(conn):
    kwargs = {
        "raw_request": '{"kind":"task_create","request":{"title":"One"}}',
        "source": "codex",
        "session_id": "session-123",
        "attachments": [{"name": "brief.pdf", "digest": "abc"}],
    }

    first = kb.create_qualification_intake(conn, **kwargs, created_at=100)
    second = kb.create_qualification_intake(conn, **kwargs, created_at=101)

    assert second == first
    assert conn.execute("SELECT COUNT(*) FROM qualification_intake").fetchone()[0] == 1


def test_intake_run_claim_heartbeat_events_and_explicit_retry(conn):
    intake_id = kb.create_qualification_intake(
        conn, raw_request="assess this", source="chat", created_at=100
    )
    runtime = {
        "profile": "productowner",
        "provider": "claude-cli",
        "model": "claude-opus-5",
        "effort": "high",
        "surface": "work_inbox_intake",
    }

    run = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity=runtime,
        lease_seconds=30,
        now=110,
    )

    assert run["status"] == "running"
    assert run["claim_expires"] == 140
    assert kb.get_qualification_intake(conn, intake_id)["status"] == "running"
    assert (
        kb.claim_qualification_intake(
            conn,
            intake_id,
            profile="productowner",
            runtime_identity=runtime,
            lease_seconds=30,
            now=111,
        )
        is None
    )
    assert kb.heartbeat_qualification_intake(
        conn,
        intake_id=intake_id,
        run_id=run["id"],
        claim_lock=run["claim_lock"],
        lease_seconds=30,
        now=120,
    )
    assert kb.get_qualification_intake_run(conn, run["id"])["claim_expires"] == 150

    event_id = kb.append_qualification_intake_event(
        conn,
        intake_id=intake_id,
        run_id=run["id"],
        kind="clarification_requested",
        payload={"question": "Which customer?"},
        created_at=125,
    )
    kb.finish_qualification_intake_run(
        conn,
        intake_id=intake_id,
        run_id=run["id"],
        claim_lock=run["claim_lock"],
        intake_status="needs_clarification",
        outcome="needs_clarification",
        now=126,
    )

    events = kb.list_qualification_intake_events(conn, intake_id)
    assert [event["kind"] for event in events] == [
        "submitted",
        "claimed",
        "clarification_requested",
    ]
    assert events[-1] == {
        "id": event_id,
        "intake_id": intake_id,
        "run_id": run["id"],
        "kind": "clarification_requested",
        "payload": {"question": "Which customer?"},
        "created_at": 125,
    }
    assert kb.get_qualification_intake(conn, intake_id)["status"] == "needs_clarification"
    assert kb.get_qualification_intake_run(conn, run["id"])["status"] == "completed"

    with pytest.raises(ValueError, match="attention_required"):
        kb.retry_qualification_intake(conn, intake_id, now=130)

    conn.execute(
        "UPDATE qualification_intake SET status = 'attention_required' WHERE id = ?",
        (intake_id,),
    )
    assert kb.retry_qualification_intake(conn, intake_id, now=131)
    assert kb.get_qualification_intake(conn, intake_id)["status"] == "pending"


def test_legacy_intake_schema_migrates_without_losing_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE qualification_intake (
            id TEXT PRIMARY KEY,
            raw_request TEXT NOT NULL,
            source TEXT NOT NULL,
            session_id TEXT,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'qualified', 'rejected', 'overridden')),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO qualification_intake
        VALUES ('qi_legacy', 'original', 'chat', NULL, '[]', 'pending', 10, 10);
        """
    )
    legacy.commit()
    legacy.close()

    kb.init_db(db_path)
    migrated = kb.connect(db_path)
    try:
        row = kb.get_qualification_intake(migrated, "qi_legacy")
        assert row["raw_request"] == "original"
        run = kb.claim_qualification_intake(
            migrated,
            "qi_legacy",
            profile="productowner",
            runtime_identity={"provider": "claude-cli", "model": "opus", "effort": "high"},
            now=20,
        )
        assert run is not None
        assert kb.get_qualification_intake(migrated, "qi_legacy")["status"] == "running"
    finally:
        migrated.close()


def test_stale_intake_runs_retry_once_then_require_attention(conn):
    intake_id = kb.create_qualification_intake(
        conn, raw_request="recover me", source="chat", created_at=10
    )
    runtime = {"provider": "claude-cli", "model": "opus", "effort": "high"}

    first = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity=runtime,
        lease_seconds=5,
        now=20,
    )
    assert kb.recover_stale_qualification_intakes(
        conn,
        failure_limit=2,
        now=26,
        pid_alive=lambda _pid: False,
    ) == {"retried": 1, "attention_required": 0}
    assert kb.get_qualification_intake(conn, intake_id)["status"] == "pending"
    assert kb.get_qualification_intake_run(conn, first["id"])["outcome"] == "reclaimed"

    second = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity=runtime,
        lease_seconds=5,
        now=30,
    )
    assert kb.recover_stale_qualification_intakes(
        conn,
        failure_limit=2,
        now=36,
        pid_alive=lambda _pid: False,
    ) == {"retried": 0, "attention_required": 1}
    assert kb.get_qualification_intake(conn, intake_id)["status"] == "attention_required"
    assert kb.get_qualification_intake_run(conn, second["id"])["outcome"] == "reclaimed"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_qualification_worker_pid_check_uses_cross_platform_probe(monkeypatch):
    observed = []
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda pid: observed.append(pid) or pid == 4242,
    )

    assert kb._qualification_worker_pid_alive(4242) is True
    assert kb._qualification_worker_pid_alive(4343) is False
    assert observed == [4242, 4343]


def test_live_intake_worker_renews_expired_claim_instead_of_reclaiming(conn):
    intake_id = kb.create_qualification_intake(
        conn, raw_request="take time to assess", source="chat", created_at=10
    )
    run = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity={
            "provider": "claude-cli",
            "model": "claude-opus-5",
            "effort": "high",
        },
        lease_seconds=5,
        now=20,
    )
    assert kb.set_qualification_intake_worker_pid(
        conn,
        intake_id=intake_id,
        run_id=run["id"],
        claim_lock=run["claim_lock"],
        worker_pid=4242,
    )

    assert kb.recover_stale_qualification_intakes(
        conn,
        now=26,
        pid_alive=lambda pid: pid == 4242,
    ) == {"retried": 0, "attention_required": 0}

    intake = kb.get_qualification_intake(conn, intake_id)
    renewed_run = kb.get_qualification_intake_run(conn, run["id"])
    assert intake["status"] == "running"
    assert conn.execute(
        "SELECT claim_expires FROM qualification_intake WHERE id = ?",
        (intake_id,),
    ).fetchone()["claim_expires"] == 326
    assert renewed_run["status"] == "running"
    assert renewed_run["claim_expires"] == 326
    assert renewed_run["last_heartbeat_at"] == 20
    assert kb.list_qualification_intake_events(conn, intake_id)[-1]["kind"] == (
        "claim_extended"
    )


def test_live_intake_worker_cannot_renew_past_max_runtime(conn):
    intake_id = kb.create_qualification_intake(
        conn, raw_request="wedged assessment", source="chat", created_at=10
    )
    run = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity={
            "provider": "claude-cli",
            "model": "claude-opus-5",
            "effort": "high",
        },
        lease_seconds=5,
        now=20,
    )
    assert kb.set_qualification_intake_worker_pid(
        conn,
        intake_id=intake_id,
        run_id=run["id"],
        claim_lock=run["claim_lock"],
        worker_pid=4242,
    )

    assert kb.recover_stale_qualification_intakes(
        conn,
        now=621,
        max_runtime_seconds=600,
        pid_alive=lambda pid: pid == 4242,
    ) == {"retried": 0, "attention_required": 1}

    assert kb.get_qualification_intake(conn, intake_id)["status"] == (
        "attention_required"
    )
    ended = kb.get_qualification_intake_run(conn, run["id"])
    assert ended["status"] == "completed"
    assert ended["outcome"] == "reclaimed"
    assert ended["last_heartbeat_at"] == 20


def test_interrupted_modern_table_migration_recovers_orphaned_legacy_rows(tmp_path):
    db_path = tmp_path / "interrupted.db"
    conn = kb.connect(db_path)
    intake_id = kb.create_qualification_intake(
        conn, raw_request="preserve me", source="chat"
    )
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        DROP TRIGGER IF EXISTS qualification_intake_no_delete;
        CREATE TABLE qualification_intake_legacy AS
            SELECT id, raw_request, source, session_id, attachments_json,
                   status, created_at, updated_at
            FROM qualification_intake;
        DELETE FROM qualification_intake;
        """
    )
    raw.close()

    kb.init_db(db_path)
    recovered = kb.connect(db_path)
    try:
        assert kb.get_qualification_intake(recovered, intake_id)["raw_request"] == "preserve me"
        assert recovered.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='qualification_intake_legacy'"
        ).fetchone() is None
    finally:
        recovered.close()


def test_terminal_intake_cannot_be_reopened_without_a_new_terminal_decision(conn):
    intake_id = kb.create_qualification_intake(
        conn, raw_request="reject me", source="chat"
    )
    kb.record_qualification_decision(
        conn,
        intake_id=intake_id,
        decision="rejected",
        actor_profile="productowner",
        reason="not product work",
    )

    with pytest.raises(sqlite3.IntegrityError, match="cannot be reopened"):
        conn.execute(
            "UPDATE qualification_intake SET status = 'pending' WHERE id = ?",
            (intake_id,),
        )


def test_divergent_intake_heartbeat_rolls_back_run_lease(conn):
    intake_id = kb.create_qualification_intake(
        conn, raw_request="heartbeat", source="chat", created_at=10
    )
    run = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity={"provider": "claude-cli", "model": "opus", "effort": "high"},
        lease_seconds=10,
        now=20,
    )
    original_expiry = run["claim_expires"]
    conn.execute(
        "UPDATE qualification_intake SET claim_lock = 'changed' WHERE id = ?",
        (intake_id,),
    )

    with pytest.raises(RuntimeError, match="changed during heartbeat"):
        kb.heartbeat_qualification_intake(
            conn,
            intake_id=intake_id,
            run_id=run["id"],
            claim_lock=run["claim_lock"],
            lease_seconds=50,
            now=25,
        )

    assert kb.get_qualification_intake_run(conn, run["id"])["claim_expires"] == original_expiry


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
        with pytest.raises(sqlite3.IntegrityError, match="Work Contract-owned"):
            kb.assign_task(connection, task_id, "reviewer")
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


def test_project_bound_standalone_card_materializes_with_canonical_project_worktree(
    tmp_path, monkeypatch
):
    board = "strict-project-bound-card"
    _strict_v2_product_board(tmp_path, monkeypatch, board)
    repo = tmp_path / "project-repo"
    repo.mkdir()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Qualification Project",
            primary_path=str(repo),
            board_slug=board,
        )
        project = pdb.get_project(project_conn, project_id)

    # Qualification runs under the product-owner profile, while the board's
    # first-class Project binding belongs to the default control-plane profile.
    profile_home = tmp_path / ".hermes" / "profiles" / "productowner"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    with kb.connect(board=board) as connection:
        task_id = _materialized_card(connection, board)
        task = kb.get_task(connection, task_id)

    assert task is not None
    assert project is not None
    assert task.project_id == project.id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(repo / ".worktrees" / task_id)
    assert task.branch_name == f"{project.slug}/{task_id}-qualified-card"


def test_project_bound_epic_story_materializes_with_canonical_project_worktree(
    tmp_path, monkeypatch
):
    board = "strict-project-bound-epic"
    _strict_v2_product_board(tmp_path, monkeypatch, board)
    repo = tmp_path / "project-repo"
    repo.mkdir()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Qualification Epic Project",
            primary_path=str(repo),
            board_slug=board,
        )
        project = pdb.get_project(project_conn, project_id)

    with kb.connect(board=board) as connection:
        request_id = kb.create_qualification_intake(
            connection,
            raw_request="qualified epic with a story",
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
        contract["stories"] = [
            {
                "title": "Qualified Story",
                "outcome": "safe story execution",
                "scope": [],
                "out_of_scope": [],
                "done_when": [],
                "depends_on": [],
            }
        ]
        signed = intake.sign_work_contract(contract, secret=b"test-only-secret")

        epic_id = intake.materialize_contract(
            connection,
            board=board,
            signed_contract=signed,
            secret=b"test-only-secret",
        )
        epic = kb.get_task(connection, epic_id)
        story_id = connection.execute(
            "SELECT task_id FROM epic_memberships WHERE epic_id = ?",
            (epic_id,),
        ).fetchone()["task_id"]
        story = kb.get_task(connection, story_id)

    assert epic is not None
    assert project is not None
    assert epic.project_id is None
    assert story is not None
    assert story.project_id == project.id
    assert story.workspace_kind == "worktree"
    assert story.workspace_path == str(repo / ".worktrees" / story_id)
    assert story.branch_name == f"{project.slug}/{story_id}-qualified-story"


def test_projectless_strict_materialization_remains_unlinked(tmp_path, monkeypatch):
    board = "strict-projectless-compat"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_card(connection, board)
        task = kb.get_task(connection, task_id)

    assert task is not None
    assert task.project_id is None
    assert task.workspace_kind == "worktree"
    assert task.workspace_path is None
    assert task.branch_name is None


def test_bound_project_without_primary_path_rejects_materialization(
    tmp_path, monkeypatch
):
    board = "strict-project-without-path"
    _strict_v2_product_board(tmp_path, monkeypatch, board)
    with pdb.connect_closing() as project_conn:
        pdb.create_project(
            project_conn,
            name="Unworkable Qualification Project",
            board_slug=board,
        )

    with kb.connect(board=board) as connection:
        request_id = kb.create_qualification_intake(
            connection,
            raw_request="project has no checkout",
            source="hermes",
            attachments=[
                {"name": "backlog-artifact"},
                {"name": "architecture-artifact"},
            ],
        )
        with pytest.raises(intake.WorkContractError, match="primary path"):
            intake.materialize_contract(
                connection,
                board=board,
                signed_contract=_signed_contract(request_id),
                secret=b"test-only-secret",
            )

        assert kb.get_qualification_intake(connection, request_id)["status"] == "pending"
        assert kb.list_qualification_decisions(connection, request_id) == []
        assert connection.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_ambiguous_project_binding_rejects_materialization_and_rolls_back(
    tmp_path, monkeypatch
):
    board = "strict-ambiguous-project-binding"
    _strict_v2_product_board(tmp_path, monkeypatch, board)
    repo = tmp_path / "project-repo"
    repo.mkdir()
    with pdb.connect_closing() as project_conn:
        pdb.create_project(
            project_conn,
            name="First Qualification Project",
            primary_path=str(repo),
            board_slug=board,
        )
        pdb.create_project(
            project_conn,
            name="Second Qualification Project",
            primary_path=str(repo),
            board_slug=board,
            # This fixture deliberately builds the ambiguous binding the test
            # asserts intake rejects. Upstream's duplicate-path guard would
            # otherwise refuse to construct it, so opt out explicitly here.
            allow_duplicate_path=True,
        )

    with kb.connect(board=board) as connection:
        request_id = kb.create_qualification_intake(
            connection,
            raw_request="ambiguous project binding",
            source="hermes",
            attachments=[
                {"name": "backlog-artifact"},
                {"name": "architecture-artifact"},
            ],
        )
        with pytest.raises(ValueError, match="ambiguous.*project"):
            intake.materialize_contract(
                connection,
                board=board,
                signed_contract=_signed_contract(request_id),
                secret=b"test-only-secret",
            )

        assert kb.get_qualification_intake(connection, request_id)["status"] == "pending"
        assert kb.list_qualification_decisions(connection, request_id) == []
        assert connection.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_materialized_worker_and_show_receive_the_signed_work_contract(
    tmp_path, monkeypatch
):
    """A worker must see the authority it is expected to obey."""
    board = "strict-work-contract-context"
    _strict_v2_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_card(connection, board)
        task = kb.get_task(connection, task_id)
        context = kb.build_worker_context(connection, task_id)

    assert "## Work Contract" in context
    assert f"ID: `{task.work_contract_id}`" in context
    assert '"scope":[]' in context
    assert '"out_of_scope":[]' in context
    assert '"done_when":[]' in context
    assert '"forbidden":[]' in context
    assert '"classification"' in context

    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    from tools import kanban_tools

    shown = json.loads(kanban_tools._handle_show({}))
    assert shown["work_contract"]["id"] == task.work_contract_id
    assert shown["work_contract"]["digest"]
    assert shown["work_contract"]["work"]["outcome"] == "safe execution"
    assert shown["work_contract"]["routing"]["entry_phase"] == "development"
    assert shown["work_contract"]["handover"]["next_phase"] == "test"
    assert shown["work_contract"]["rules"] == {
        "allowed": [],
        "forbidden": [],
    }
    assert "## Work Contract" in shown["worker_context"]


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


def test_qualified_epic_does_not_authorize_unrelated_child_contract(
    tmp_path, monkeypatch
):
    board = "strict"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        epic_id = _materialized_epic(connection, board)
        epic = kb.get_task(connection, epic_id)
        root_contract = kb.get_work_contract(connection, epic.work_contract_id)
        rogue = _signed_contract(root_contract["request_id"])
        rogue_contract_id = kb.store_work_contract(
            connection,
            rogue,
            secret=b"test-only-secret",
        )

        with kb.authorized_governance_write():
            with pytest.raises(sqlite3.IntegrityError, match="qualification"):
                kb.create_task(
                    connection,
                    title="Unrelated child",
                    body="Must remain inert",
                    assignee="developer",
                    board=board,
                    workflow_template_id="product",
                    current_step_key="development",
                    work_contract_id=rogue_contract_id,
                    work_item_kind="card",
                )


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
        (repository / "README.md").write_bytes(b"dirty\r\n")
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
        assert repository_evidence["status_porcelain"] == ["M README.md"]

        captured_digest = repository_evidence["worktree_digest"]
        (repository / "README.md").write_bytes(b"dirty\n")
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        current_evidence = kb.task_repository_evidence(row)
        assert current_evidence["status_porcelain"] == ["M README.md"]
        assert current_evidence["worktree_digest"] != captured_digest


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


@pytest.mark.parametrize("mutation", ["comment", "dependency", "repository"])
def test_requalification_refuses_stale_card_evidence(
    tmp_path, monkeypatch, mutation
):
    board = "strict-requalification-stale-evidence"
    _strict_product_board(tmp_path, monkeypatch, board)

    with kb.connect(board=board) as connection:
        task_id = _materialized_scheduled_card(connection, board)
        old_contract_id = kb.get_task(connection, task_id).work_contract_id
        receipt = intake.submit_requalification(
            connection,
            task_id=task_id,
            reason="qualify the current evidence snapshot",
        )
        contract = _signed_contract(receipt["intake_id"])["contract"]
        successor = intake.sign_work_contract(
            contract, secret=b"test-only-secret"
        )

        if mutation == "comment":
            kb.add_comment(
                connection,
                task_id,
                "operator",
                "New evidence arrived while Product Owner qualification was running",
            )
        elif mutation == "dependency":
            parent_id = _materialized_card(connection, board)
            with kb.authorized_governance_write():
                kb.link_tasks(connection, parent_id, task_id)
        else:
            with kb.authorized_governance_write(), kb.write_txn(connection):
                connection.execute(
                    "UPDATE tasks SET branch_name = 'changed-branch' WHERE id = ?",
                    (task_id,),
                )

        with pytest.raises(
            intake.WorkContractError, match="evidence changed during qualification"
        ):
            intake.materialize_contract(
                connection,
                board=board,
                signed_contract=successor,
                secret=b"test-only-secret",
            )

        card = kb.get_task(connection, task_id)
        assert card.status == "scheduled"
        assert card.work_contract_id == old_contract_id


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
