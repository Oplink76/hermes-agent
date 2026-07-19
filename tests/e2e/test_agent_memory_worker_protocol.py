"""End-to-end proof for the delegated Agent Memory worker protocol."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import agent_memory_vault as memory_vault
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_qualifier as qualifier
from hermes_cli import main as main_module


SURFACES = (
    "hermes-direct",
    "hermes-child",
    "codex-cli",
    "claude-code-cli",
    "cowork-mcp",
)
AGENT_IDS = {
    "hermes-direct": "hermes",
    "hermes-child": "hermes-child",
    "codex-cli": "codex",
    "claude-code-cli": "claude-code",
    "cowork-mcp": "claude-code",
}


def _persisted_gist(vault, gist_id: str):
    entries = [
        entry
        for entry in memory_vault._recent_valid_entries(vault)
        if entry.gist_id == gist_id
    ]
    assert len(entries) == 1
    return entries[0]


def _assert_executor(
    executor: dict,
    *,
    surface: str,
    run_id: int,
    hermes_role: str,
    responsibility: str,
) -> None:
    assert executor["surface"] == surface
    assert executor["agent_id"] == AGENT_IDS[surface]
    assert executor["execution_id"] == f"execution-{surface}-{run_id}"
    assert executor["hermes_role"] == hermes_role
    assert executor["responsibility"] == responsibility


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


def _git(repo, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(repo) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.com")
    _git(repo, "config", "user.name", "Worker Protocol")
    (repo / "README.md").write_text("worker protocol\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "test fixture")


@pytest.fixture
def protocol_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    vault = tmp_path / "Agent Memory"
    vault.mkdir()
    outbox = tmp_path / "outbox"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
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
        kb.set_workspace_path(conn, task_id, repo)
        (repo / "worker-result.txt").write_text(
            "bounded worker result\n", encoding="utf-8"
        )
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

    agent_id = AGENT_IDS[surface]
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
    role = executor["hermes_role"]
    provenance_key = {
        "developer": "writer",
        "tester": "tester",
        "reviewer": "reviewer",
    }[role]
    metadata["ai_provenance"] = {
        provenance_key: {
            "agent": agent_id,
            "model": executor["model"],
        }
    }
    if role == "tester":
        metadata["workflow_outcome"] = {"verdict": "passed"}
    elif role == "reviewer":
        metadata["workflow_outcome"] = {"verdict": "approved"}

    return SimpleNamespace(
        recall_receipt=recall_result["receipt"],
        write_receipt=write_result["receipt"],
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
    receipt_executor = outcome.write_receipt["executor"]
    assert outcome.recall_receipt["executor"] == receipt_executor
    _assert_executor(
        receipt_executor,
        surface=surface,
        run_id=protocol_board.run_id,
        hermes_role="developer",
        responsibility="writer",
    )
    assert protocol_board.complete(outcome.metadata) is True
    if surface == "cowork-mcp":
        assert outcome.recall_receipt["status"] == "unavailable"
        assert outcome.write_receipt["status"] == "queued"
        path = protocol_board.outbox / f"gist-{outcome.write_receipt['gist_id']}.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["receipt"] == outcome.write_receipt
        assert envelope["gist"]["executor"] == receipt_executor
    else:
        assert outcome.write_receipt["status"] in {"stored", "already_stored"}
        entry = _persisted_gist(
            protocol_board.vault, outcome.write_receipt["gist_id"]
        )
        assert json.loads(entry.fields["Executor"]) == receipt_executor
        assert entry.function_id == outcome.function_id
    task = kb.get_task(protocol_board.conn, protocol_board.task_id)
    assert task is not None and task.current_step_key == "test"


def test_receipt_gate_rejects_missing_and_mismatched_before_accepting_cli_receipts(
    protocol_board, capsys
):
    outcome = run_fake_delegated_worker(
        surface="hermes-child",
        conn=protocol_board.conn,
        task_id=protocol_board.task_id,
        run_id=protocol_board.run_id,
        board=protocol_board.slug,
        capsys=capsys,
    )

    with pytest.raises(kb.AgentMemoryHandoverError) as missing:
        protocol_board.complete({})
    assert missing.value.missing == ("recall", "write")
    task = kb.get_task(protocol_board.conn, protocol_board.task_id)
    assert task is not None and task.status == "running"

    mismatched = json.loads(json.dumps(outcome.metadata))
    mismatched["agent_memory"]["write"]["run_id"] += 1
    with pytest.raises(kb.AgentMemoryHandoverError) as invalid:
        protocol_board.complete(mismatched)
    assert invalid.value.invalid == ("write",)
    task = kb.get_task(protocol_board.conn, protocol_board.task_id)
    assert task is not None and task.status == "running"

    assert protocol_board.complete(outcome.metadata) is True
    task = kb.get_task(protocol_board.conn, protocol_board.task_id)
    assert task is not None and task.current_step_key == "test"


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
    task = kb.get_task(protocol_board.conn, protocol_board.task_id)
    assert task is not None and task.current_step_key == "test"

    tester_claimed = kb.claim_task(
        protocol_board.conn,
        protocol_board.task_id,
        board=protocol_board.slug,
        claimer="protocol-tester",
    )
    assert tester_claimed is not None and tester_claimed.current_run_id is not None
    tester = run_fake_delegated_worker(
        surface="hermes-direct",
        conn=protocol_board.conn,
        task_id=protocol_board.task_id,
        run_id=tester_claimed.current_run_id,
        board=protocol_board.slug,
        capsys=capsys,
    )
    assert kb.complete_task(
        protocol_board.conn,
        protocol_board.task_id,
        summary="The bounded worker task passed verification",
        metadata=tester.metadata,
        expected_run_id=tester_claimed.current_run_id,
        board=protocol_board.slug,
    ) is True
    task = kb.get_task(protocol_board.conn, protocol_board.task_id)
    assert task is not None and task.current_step_key == "review"

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

    writer_executor = writer.write_receipt["executor"]
    reviewer_executor = reviewer.write_receipt["executor"]
    _assert_executor(
        writer_executor,
        surface="codex-cli",
        run_id=protocol_board.run_id,
        hermes_role="developer",
        responsibility="writer",
    )
    _assert_executor(
        reviewer_executor,
        surface="claude-code-cli",
        run_id=claimed.current_run_id,
        hermes_role="reviewer",
        responsibility="reviewer",
    )
    assert writer_executor["execution_id"] != reviewer_executor["execution_id"]
    assert writer.recall_receipt["delegation_id"] != reviewer.recall_receipt[
        "delegation_id"
    ]
    assert writer.recall_receipt["run_id"] != reviewer.recall_receipt["run_id"]
    assert writer.write_receipt["gist_id"] != reviewer.write_receipt["gist_id"]
    assert writer.function_id == reviewer.function_id
    writer_entry = _persisted_gist(
        protocol_board.vault, writer.write_receipt["gist_id"]
    )
    reviewer_entry = _persisted_gist(
        protocol_board.vault, reviewer.write_receipt["gist_id"]
    )
    assert writer_entry.function_id == reviewer_entry.function_id == writer.function_id
    assert json.loads(writer_entry.fields["Executor"]) == writer_executor
    assert json.loads(reviewer_entry.fields["Executor"]) == reviewer_executor
    assert kb.complete_task(
        protocol_board.conn,
        protocol_board.task_id,
        summary="Independent review approved the bounded task",
        metadata=reviewer.metadata,
        expected_run_id=claimed.current_run_id,
        board=protocol_board.slug,
    ) is True
    task = kb.get_task(protocol_board.conn, protocol_board.task_id)
    assert task is not None and task.current_step_key == "release_measure"
