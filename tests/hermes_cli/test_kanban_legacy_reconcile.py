from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_legacy_reconcile as reconcile


BOARDS = [
    "agentic-os-cockpit",
    "the-trading-company",
    "llm-memory-wiki-bridge",
    "handoff-lab",
    "ready-console",
    "useful-tool",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _insert_internal_lineage(
    conn: Any,
    *,
    task_id: str,
    intake_id: str,
    contract_id: str,
    source: str,
) -> None:
    task_field = "legacy_task_id" if source == "hermes-migration" else "target_task_id"
    raw_request = json.dumps(
        {"kind": "test_fixture", task_field: task_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    with kb.authorized_governance_write(), kb.write_txn(conn):
        conn.execute(
            """
            INSERT INTO qualification_intake (
                id, raw_request, source, session_id, attachments_json,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, '[]', 'qualified', 10, 10)
            """,
            (intake_id, raw_request, source),
        )
        conn.execute(
            """
            INSERT INTO work_contracts (
                id, request_id, canonical_json, digest, signature,
                issuer_profile, issuer_run_id, policy_version, created_at
            ) VALUES (?, ?, '{}', ?, 'test-signature', ?, NULL, 'test-policy', 10)
            """,
            (contract_id, intake_id, f"digest-{contract_id}", source),
        )
        conn.execute(
            """
            INSERT INTO qualification_intake_decisions (
                intake_id, decision, actor_profile, reason, contract_id, created_at
            ) VALUES (?, 'qualified', ?, 'test fixture', ?, 10)
            """,
            (intake_id, source, contract_id),
        )
        conn.execute(
            "UPDATE tasks SET work_contract_id = ? WHERE id = ?",
            (contract_id, task_id),
        )


@pytest.fixture
def exact_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)

    for board in BOARDS:
        with kb.connect_closing(board=board):
            pass

    card_specs = [
        (
            "agentic-os-cockpit",
            "Verify shipped card",
            "done",
            "done",
            "verify",
            "legitimate_or_none",
        ),
        (
            "agentic-os-cockpit",
            "Reconcile legacy completion",
            "ready",
            "release_measure",
            "legacy_reconciled",
            "migration_artifact_not_qualification",
        ),
        (
            "agentic-os-cockpit",
            "Keep unfinished card open",
            "blocked",
            "development",
            "keep_open",
            "migration_artifact_not_qualification",
        ),
        (
            "the-trading-company",
            "Review uncertain card",
            "review",
            "review",
            "review",
            "legitimate_or_none",
        ),
        (
            "the-trading-company",
            "Archive orphan card",
            "archived",
            "backlog",
            "archive",
            "legitimate_or_none",
        ),
    ]
    cards: list[dict[str, Any]] = []
    correction_number = 0
    for (
        board,
        title,
        status,
        step,
        card_disposition,
        qualification_disposition,
    ) in card_specs:
        with kb.connect_closing(board=board) as conn:
            task_id = kb.create_task(
                conn,
                title=title,
                initial_status="running",
                current_step_key=step,
            )
            running = int(status == "running")
            blocked = int(status == "blocked")
            with kb.write_txn(conn):
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, current_step_key = ?, current_run_id = NULL,
                        running = ?, blocked = ?
                    WHERE id = ?
                    """,
                    (status, step, running, blocked, task_id),
                )

            lineage = {"intake_ids": [], "contract_ids": []}
            work_contract_id = None
            if qualification_disposition == "migration_artifact_not_qualification":
                correction_number += 1
                intake_id = f"qi_internal_{correction_number}"
                contract_id = f"wc_internal_{correction_number}"
                source = (
                    "hermes-migration" if correction_number == 1 else "hermes-reconcile"
                )
                _insert_internal_lineage(
                    conn,
                    task_id=task_id,
                    intake_id=intake_id,
                    contract_id=contract_id,
                    source=source,
                )
                lineage = {
                    "intake_ids": [intake_id],
                    "contract_ids": [contract_id],
                }
                work_contract_id = contract_id

        cards.append({
            "board": board,
            "task_id": task_id,
            "expected": {
                "status": status,
                "current_step_key": step,
                "current_run_id": None,
                "running": running,
                "blocked": blocked,
                "work_contract_id": work_contract_id,
            },
            "card_disposition": card_disposition,
            "qualification_disposition": qualification_disposition,
            "qualification_lineage": lineage,
            "evidence": [f"fixture evidence for {task_id}"],
        })

    manifest = {
        "version": 1,
        "scope": "legacy-board-inbox-reconciliation-2026-07-20",
        "boards": BOARDS,
        "cards": cards,
    }
    manifest_path = tmp_path / "legacy-reconciliation-manifest.json"
    _write_manifest(manifest_path, manifest)
    return manifest_path, manifest


def test_audit_is_read_only_and_reports_exact_counts(
    exact_manifest,
    monkeypatch: pytest.MonkeyPatch,
):
    manifest_path, _manifest = exact_manifest
    before = {board: _sha(kb.kanban_db_path(board)) for board in BOARDS}
    monkeypatch.setattr(
        kb,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must use a direct query-only connection")
        ),
    )
    monkeypatch.setattr(
        kb,
        "init_db",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not initialize or migrate boards")
        ),
    )

    report = reconcile.audit_manifest(manifest_path)

    assert report["mode"] == "dry-run"
    assert report["manifest_sha256"] == _sha(manifest_path)
    assert report["counts"] == {
        "cards": 5,
        "verify": 1,
        "legacy_reconciled": 1,
        "keep_open": 1,
        "review": 1,
        "archive": 1,
        "qualification_corrections": 2,
    }
    assert {board: _sha(kb.kanban_db_path(board)) for board in BOARDS} == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("status", "guard mismatch"),
        ("missing_card", "manifest inventory mismatch"),
        ("duplicate_card", "duplicate card"),
        ("unknown_disposition", "unknown card disposition"),
    ],
)
def test_audit_fails_closed_on_invalid_or_changed_inventory(
    exact_manifest,
    mutation: str,
    message: str,
):
    manifest_path, original = exact_manifest
    manifest = copy.deepcopy(original)
    if mutation == "status":
        card = manifest["cards"][0]
        with kb.connect_closing(board=card["board"]) as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'review' WHERE id = ?",
                    (card["task_id"],),
                )
    elif mutation == "missing_card":
        manifest["cards"].pop()
        _write_manifest(manifest_path, manifest)
    elif mutation == "duplicate_card":
        manifest["cards"].append(copy.deepcopy(manifest["cards"][0]))
        _write_manifest(manifest_path, manifest)
    else:
        manifest["cards"][0]["card_disposition"] = "invented"
        _write_manifest(manifest_path, manifest)

    with pytest.raises(reconcile.ReconciliationBlocked, match=message):
        reconcile.audit_manifest(manifest_path)


def test_audit_fails_closed_when_internal_lineage_is_omitted(exact_manifest):
    manifest_path, original = exact_manifest
    manifest = copy.deepcopy(original)
    correction = next(
        card
        for card in manifest["cards"]
        if card["qualification_disposition"] == "migration_artifact_not_qualification"
    )
    correction["qualification_lineage"] = {
        "intake_ids": [],
        "contract_ids": [],
    }
    _write_manifest(manifest_path, manifest)

    with pytest.raises(reconcile.ReconciliationBlocked, match="lineage mismatch"):
        reconcile.audit_manifest(manifest_path)


def test_audit_requires_the_exact_approved_board_list(exact_manifest):
    manifest_path, original = exact_manifest
    manifest = copy.deepcopy(original)
    manifest["boards"] = manifest["boards"][:-1]
    _write_manifest(manifest_path, manifest)

    with pytest.raises(reconcile.ReconciliationBlocked, match="approved boards"):
        reconcile.audit_manifest(manifest_path)
