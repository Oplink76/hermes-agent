"""End-to-end proof for the delegated Agent Memory worker protocol."""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_qualifier as qualifier
from hermes_cli import main as main_module
from hermes_cli.agent_memory_vault import recall


SURFACES = (
    "hermes-direct",
    "hermes-child",
    "codex-cli",
    "claude-code-cli",
    "cowork-mcp",
)


def _development_decision(title: str) -> dict:
    return {
        "qualification_path": "hermes",
        "work": {
            "item_kind": "card",
            "work_type": "maintenance",
            "title": title,
            "outcome": "Delegated workers preserve governed memory",
            "scope": ["Exercise the actual-worker memory protocol"],
            "out_of_scope": ["Change workflow authority"],
        },
        "routing": {
            "entry_phase": "development",
            "assignee": "developer",
            "epic_id": None,
            "dependencies": [],
        },
        "entry_assessment": {
            "reason": "The bounded implementation contract is approved",
            "skipped_phases": [
                {
                    "phase": "backlog",
                    "reason": "The outcome is already defined",
                    "evidence": ["evidence:backlog"],
                },
                {
                    "phase": "architecture",
                    "reason": "The protocol design is already approved",
                    "evidence": ["evidence:architecture"],
                },
            ],
            "evidence": ["evidence:backlog", "evidence:architecture"],
        },
        "handover": {
            "deliverables": ["Actual-worker recall and write receipts"],
            "required_evidence": ["Focused end-to-end test"],
            "done_when": ["The production receipt gate accepts the handover"],
            "next_phase": "test",
            "next_role": "tester",
        },
        "rules": {
            "allowed": ["Use the governed CLI protocol"],
            "forbidden": ["Edit the vault or outbox directly"],
        },
        "classification": ["framework:maintenance", "path:hermes"],
    }


@pytest.fixture
def protocol_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    vault = tmp_path / "Agent Memory"
    vault.mkdir()
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_VAULT", str(vault))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_OUTBOX", str(outbox))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    board = "worker-protocol"
    kb.ensure_product_board_defaults(board)
    metadata = kb.read_board_metadata(board)
    metadata["qualification"]["required"] = True
    metadata["qualification"]["work_types"] = list(
        qualifier.DEFAULT_WORK_TYPES
    )
    kb.board_metadata_path(board).write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    conn = kb.connect(board=board)
    try:
        title = "Exercise delegated Agent Memory"
        intake = qualifier.submit_request(
            conn,
            request={"title": title, "body": "Run one bounded worker task"},
            source="e2e",
            attachments=(
                {"name": "evidence:backlog"},
                {"name": "evidence:architecture"},
            ),
        )
        qualified = qualifier.qualify_intake(
            conn,
            board=board,
            intake_id=intake["intake_id"],
            model_call=lambda _prompt: _development_decision(title),
            secret=b"test-only-secret",
            issued_at=100,
        )
        assert qualified["status"] == "qualified"
        task_id = qualified["task_id"]
        claimed = kb.claim_task(conn, task_id, board=board)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id

        def complete(worker_metadata: dict) -> bool:
            return kb.complete_task(
                conn,
                task_id,
                summary="The bounded worker task is complete",
                metadata=worker_metadata,
                expected_run_id=run_id,
                board=board,
                product_workflow_enabled=False,
            )

        yield SimpleNamespace(
            conn=conn,
            slug=board,
            task_id=task_id,
            run_id=run_id,
            vault=vault,
            outbox=outbox,
            complete=complete,
        )
    finally:
        conn.close()


def run_fake_delegated_worker(
    surface: str,
    conn,
    task_id: str,
    run_id: int,
    board: str,
    capsys,
):
    assert surface in SURFACES
    context = kb.build_worker_context(conn, task_id)

    def protocol_payload(command: str) -> dict:
        marker = f"Run `hermes agent-memory {command} --input -` with this JSON on stdin:"
        assert marker in context
        fenced = context.split(marker, 1)[1].split("```json", 1)[1]
        return json.loads(fenced.split("```", 1)[0])

    recall_payload = protocol_payload("recall")
    write_payload = protocol_payload("write")
    assert recall_payload["task_id"] == task_id
    assert recall_payload["run_id"] == run_id
    assert write_payload["context"] == f"board={board}; task={task_id}; run={run_id}"

    agent_id = {
        "hermes-direct": "hermes",
        "hermes-child": "hermes-child",
        "codex-cli": "codex",
        "claude-code-cli": "claude-code",
        "cowork-mcp": "claude-code",
    }[surface]
    executor = {
        **recall_payload["executor"],
        "agent_id": agent_id,
        "execution_id": f"execution-{surface}-{run_id}",
        "model": "test-model",
        "surface": surface,
    }
    recall_payload["executor"] = executor
    write_payload.update(
        {
            "behavior": "none",
            "decisions": "none",
            "evidence": "tests: end-to-end protocol passed",
            "executor": executor,
            "gist_id": f"gist-{surface}-{run_id}",
            "maturity": "code_complete",
            "occurred_at": "2026-07-19T12:00:00",
            "open_loops": "none",
            "result": "The bounded worker task is complete.",
            "reused": "the existing governed CLI and handover gate",
            "summary": "Completed the bounded actual-worker memory protocol.",
        }
    )

    def invoke(action: str, payload: dict) -> dict:
        capsys.readouterr()
        with patch.object(
            sys,
            "argv",
            ["hermes", "agent-memory", action, "--input", "-"],
        ), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            main_module.main()
        captured = capsys.readouterr()
        assert captured.err == ""
        return json.loads(captured.out)

    recall_result = invoke("recall", recall_payload)
    write_result = invoke("write", write_payload)
    metadata = {
        "agent_memory": {
            "recall": recall_result["receipt"],
            "write": write_result["receipt"],
        },
    }

    return SimpleNamespace(
        recall_receipt=recall_result["receipt"],
        write_receipt=write_result["receipt"],
        executor=executor,
        function_id=write_payload["function_id"],
        metadata=metadata,
    )


@pytest.mark.parametrize("surface", SURFACES)
def test_each_surface_recalls_and_writes(surface, protocol_board, capsys):
    if surface == "cowork-mcp":
        protocol_board.vault.rmdir()
    outcome = run_fake_delegated_worker(
        surface=surface,
        conn=protocol_board.conn,
        task_id=protocol_board.task_id,
        run_id=protocol_board.run_id,
        board=protocol_board.slug,
        capsys=capsys,
    )

    assert outcome.recall_receipt["operation"] == "recall"
    assert outcome.write_receipt["status"] in {
        "stored",
        "already_stored",
        "queued",
    }
    assert outcome.executor["surface"] == surface
    assert protocol_board.complete(outcome.metadata) is True
    if surface == "cowork-mcp":
        assert outcome.recall_receipt["status"] == "unavailable"
        assert outcome.write_receipt["status"] == "queued"
        assert list(protocol_board.outbox.glob("gist-*.json"))


def test_writer_and_reviewer_have_distinct_execution_and_gist_ids(
    protocol_board, capsys
):
    writer = run_fake_delegated_worker(
        surface="codex-cli",
        conn=protocol_board.conn,
        task_id=protocol_board.task_id,
        run_id=protocol_board.run_id,
        board=protocol_board.slug,
        capsys=capsys,
    )
    assert protocol_board.complete(writer.metadata) is True

    with kb.authorized_governance_write(), kb.write_txn(protocol_board.conn):
        protocol_board.conn.execute(
            "UPDATE tasks SET status='review', assignee='reviewer', "
            "current_step_key='review', running=0, blocked=0, "
            "claim_lock=NULL, claim_expires=NULL WHERE id=?",
            (protocol_board.task_id,),
        )
    claimed = kb.claim_review_task(
        protocol_board.conn,
        protocol_board.task_id,
        claimer="independent-reviewer",
    )
    assert claimed is not None and claimed.current_run_id is not None

    reviewer = run_fake_delegated_worker(
        surface="claude-code-cli",
        conn=protocol_board.conn,
        task_id=protocol_board.task_id,
        run_id=claimed.current_run_id,
        board=protocol_board.slug,
        capsys=capsys,
    )

    assert writer.executor["responsibility"] == "writer"
    assert reviewer.executor["responsibility"] == "reviewer"
    assert writer.executor["execution_id"] != reviewer.executor["execution_id"]
    assert writer.recall_receipt["delegation_id"] != reviewer.recall_receipt[
        "delegation_id"
    ]
    assert writer.recall_receipt["run_id"] != reviewer.recall_receipt["run_id"]
    assert writer.write_receipt["gist_id"] != reviewer.write_receipt["gist_id"]
    assert writer.function_id == reviewer.function_id
    writer_entry = recall(
        protocol_board.vault, writer.write_receipt["gist_id"], limit=1
    )[0]
    reviewer_entry = recall(
        protocol_board.vault, reviewer.write_receipt["gist_id"], limit=1
    )[0]
    assert writer_entry.function_id == reviewer_entry.function_id == writer.function_id
    assert kb.complete_task(
        protocol_board.conn,
        protocol_board.task_id,
        summary="Independent review approved the bounded task",
        metadata=reviewer.metadata,
        expected_run_id=claimed.current_run_id,
        board=protocol_board.slug,
        product_workflow_enabled=False,
    ) is True
