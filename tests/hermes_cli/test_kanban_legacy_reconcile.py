from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
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


def _write_approval(path: Path, manifest_path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "authority": "break-glass",
                "scope": "legacy-board-inbox-reconciliation-2026-07-20",
                "approved_by": "Ole Ørum-Petersen",
                "approved_at": "2026-07-20T00:00:00+02:00",
                "manifest_sha256": _sha(manifest_path),
                "evidence": "Default-board maintenance card t_test",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _table_rows(board: str, table: str) -> list[dict[str, Any]]:
    with kb.connect_closing(board=board) as conn:
        return [
            dict(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]


def _task_row(board: str, task_id: str) -> dict[str, Any]:
    with kb.connect_closing(board=board) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return dict(row)


def _event_payloads(board: str, task_id: str, kind: str) -> list[dict[str, Any]]:
    with kb.connect_closing(board=board) as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _logical_dump(board: str) -> str:
    with sqlite3.connect(str(kb.kanban_db_path(board))) as conn:
        return "\n".join(conn.iterdump())


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
            "ready",
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


def test_apply_requires_approval_bound_to_manifest_hash(exact_manifest, tmp_path: Path):
    manifest_path, _manifest = exact_manifest
    approval_path = tmp_path / "approval.json"
    _write_approval(approval_path, manifest_path)
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["manifest_sha256"] = "0" * 64
    approval_path.write_text(json.dumps(approval), encoding="utf-8")

    with pytest.raises(reconcile.ReconciliationBlocked, match="approval hash"):
        reconcile.apply_manifest(manifest_path, approval_path)


def test_apply_requires_ole_as_break_glass_approver(exact_manifest, tmp_path: Path):
    manifest_path, _manifest = exact_manifest
    approval_path = tmp_path / "approval.json"
    _write_approval(approval_path, manifest_path)
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["approved_by"] = "Someone Else"
    approval_path.write_text(json.dumps(approval), encoding="utf-8")

    with pytest.raises(reconcile.ReconciliationBlocked, match="Ole Ørum-Petersen"):
        reconcile.apply_manifest(manifest_path, approval_path)


def test_apply_writes_only_approved_state_and_events(exact_manifest, tmp_path: Path):
    manifest_path, manifest = exact_manifest
    approval_path = tmp_path / "approval.json"
    recovery_root = tmp_path / "recovery"
    _write_approval(approval_path, manifest_path)
    no_op_cards = [
        card
        for card in manifest["cards"]
        if card["card_disposition"] in {"verify", "keep_open", "review"}
    ]
    before_no_op = {
        (card["board"], card["task_id"]): _task_row(card["board"], card["task_id"])
        for card in no_op_cards
    }
    preserved_tables = (
        "qualification_intake",
        "qualification_intake_decisions",
        "work_contracts",
        "task_comments",
        "task_runs",
        "task_attachments",
    )
    before_evidence = {
        (board, table): _table_rows(board, table)
        for board in BOARDS
        for table in preserved_tables
    }

    result = reconcile.apply_manifest(
        manifest_path,
        approval_path,
        recovery_root=recovery_root,
    )

    assert result["status"] == "applied"
    assert result["counts"] == {
        "legacy_reconciled": 1,
        "archived": 1,
        "qualification_history_corrected": 2,
    }
    assert result["manifest_sha256"] == _sha(manifest_path)
    receipt_path = Path(result["receipt_path"])
    assert receipt_path.is_file()
    if os.name != "nt":
        assert receipt_path.stat().st_mode & 0o222 == 0
        assert receipt_path.parent.stat().st_mode & 0o222 == 0
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["before_status_counts"]
    assert receipt["after_status_counts"]
    assert receipt["event_counts"] == {
        "legacy_reconciled": 1,
        "qualification_history_corrected": 2,
    }
    assert {
        (card["board"], card["task_id"]): _task_row(card["board"], card["task_id"])
        for card in no_op_cards
    } == before_no_op
    assert {
        (board, table): _table_rows(board, table)
        for board in BOARDS
        for table in preserved_tables
    } == before_evidence

    legacy = next(
        card
        for card in manifest["cards"]
        if card["card_disposition"] == "legacy_reconciled"
    )
    legacy_row = _task_row(legacy["board"], legacy["task_id"])
    assert legacy_row["status"] == "done"
    assert legacy_row["current_step_key"] == "done"
    assert len(
        _event_payloads(
            legacy["board"], legacy["task_id"], "legacy_reconciled"
        )
    ) == 1

    archive = next(
        card for card in manifest["cards"]
        if card["card_disposition"] == "archive"
    )
    assert _task_row(archive["board"], archive["task_id"])["status"] == "archived"
    correction_cards = [
        card
        for card in manifest["cards"]
        if card["qualification_disposition"] == "migration_artifact_not_qualification"
    ]
    assert all(
        len(
            _event_payloads(
                card["board"],
                card["task_id"],
                "qualification_history_corrected",
            )
        ) == 1
        for card in correction_cards
    )


def test_apply_is_idempotent_for_same_manifest(exact_manifest, tmp_path: Path):
    manifest_path, manifest = exact_manifest
    approval_path = tmp_path / "approval.json"
    recovery_root = tmp_path / "recovery"
    _write_approval(approval_path, manifest_path)

    first = reconcile.apply_manifest(
        manifest_path, approval_path, recovery_root=recovery_root
    )
    second = reconcile.apply_manifest(
        manifest_path, approval_path, recovery_root=recovery_root
    )

    assert first["already_applied"] is False
    assert second["already_applied"] is True
    assert second["receipt_path"] == first["receipt_path"]
    for card in manifest["cards"]:
        if card["card_disposition"] == "legacy_reconciled":
            assert len(
                _event_payloads(
                    card["board"], card["task_id"], "legacy_reconciled"
                )
            ) == 1
        if card["qualification_disposition"] == "migration_artifact_not_qualification":
            assert len(
                _event_payloads(
                    card["board"],
                    card["task_id"],
                    "qualification_history_corrected",
                )
            ) == 1


def test_active_mutation_target_blocks_before_snapshots_or_writes(
    exact_manifest,
    tmp_path: Path,
):
    manifest_path, original = exact_manifest
    manifest = copy.deepcopy(original)
    target = next(
        card
        for card in manifest["cards"]
        if card["card_disposition"] == "legacy_reconciled"
    )
    with kb.connect_closing(board=target["board"]) as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET running = 1 WHERE id = ?",
                (target["task_id"],),
            )
    target["expected"]["running"] = 1
    _write_manifest(manifest_path, manifest)
    approval_path = tmp_path / "approval.json"
    recovery_root = tmp_path / "recovery"
    _write_approval(approval_path, manifest_path)

    with pytest.raises(reconcile.ReconciliationBlocked, match="active run"):
        reconcile.apply_manifest(
            manifest_path,
            approval_path,
            recovery_root=recovery_root,
        )

    assert not recovery_root.exists()
    assert _event_payloads(
        target["board"], target["task_id"], "legacy_reconciled"
    ) == []


@pytest.mark.parametrize("active_state", ["qualification_only", "unended_run"])
def test_every_written_card_with_an_active_state_blocks_before_snapshots(
    exact_manifest,
    tmp_path: Path,
    active_state: str,
):
    manifest_path, original = exact_manifest
    manifest = copy.deepcopy(original)
    target = next(
        card
        for card in manifest["cards"]
        if card["card_disposition"] == "keep_open"
        and card["qualification_disposition"]
        == "migration_artifact_not_qualification"
    )
    with kb.connect_closing(board=target["board"]) as conn:
        with kb.write_txn(conn):
            if active_state == "qualification_only":
                conn.execute(
                    "UPDATE tasks SET running = 1 WHERE id = ?",
                    (target["task_id"],),
                )
                target["expected"]["running"] = 1
            else:
                cursor = conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, started_at) "
                    "VALUES (?, 'default', 'done', 1)",
                    (target["task_id"],),
                )
                run_id = int(cursor.lastrowid)
                conn.execute(
                    "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                    (run_id, target["task_id"]),
                )
                target["expected"]["current_run_id"] = run_id
    _write_manifest(manifest_path, manifest)
    approval_path = tmp_path / "approval.json"
    recovery_root = tmp_path / "recovery"
    _write_approval(approval_path, manifest_path)

    with pytest.raises(reconcile.ReconciliationBlocked, match="active run"):
        reconcile.apply_manifest(
            manifest_path, approval_path, recovery_root=recovery_root
        )

    assert not recovery_root.exists()


def test_partial_event_without_success_receipt_fails_closed(
    exact_manifest, tmp_path: Path
):
    manifest_path, manifest = exact_manifest
    target = next(
        card
        for card in manifest["cards"]
        if card["card_disposition"] == "legacy_reconciled"
    )
    digest = _sha(manifest_path)
    with kb.connect_closing(board=target["board"]) as conn:
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                target["task_id"],
                "legacy_reconciled",
                {"manifest_sha256": digest, "actor": "interrupted-apply"},
            )
    approval_path = tmp_path / "approval.json"
    recovery_root = tmp_path / "recovery"
    _write_approval(approval_path, manifest_path)

    with pytest.raises(reconcile.ReconciliationBlocked, match="partial"):
        reconcile.apply_manifest(
            manifest_path, approval_path, recovery_root=recovery_root
        )

    assert not recovery_root.exists()


def test_repeat_rejects_tampered_canonical_event(exact_manifest, tmp_path: Path):
    manifest_path, manifest = exact_manifest
    approval_path = tmp_path / "approval.json"
    recovery_root = tmp_path / "recovery"
    _write_approval(approval_path, manifest_path)
    reconcile.apply_manifest(
        manifest_path, approval_path, recovery_root=recovery_root
    )
    target = next(
        card
        for card in manifest["cards"]
        if card["card_disposition"] == "legacy_reconciled"
    )
    with kb.connect_closing(board=target["board"]) as conn:
        with kb.write_txn(conn):
            row = conn.execute(
                "SELECT id, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'legacy_reconciled'",
                (target["task_id"],),
            ).fetchone()
            payload = json.loads(row["payload"])
            payload["actor"] = "tampered"
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(payload), row["id"]),
            )

    with pytest.raises(reconcile.ReconciliationBlocked, match="event mismatch"):
        reconcile.apply_manifest(
            manifest_path, approval_path, recovery_root=recovery_root
        )


def test_later_board_failure_restores_earlier_board(
    exact_manifest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manifest_path, _manifest = exact_manifest
    approval_path = tmp_path / "approval.json"
    recovery_root = tmp_path / "recovery"
    _write_approval(approval_path, manifest_path)
    before = {board: _logical_dump(board) for board in BOARDS}
    original_apply_board = reconcile._apply_board

    def fail_on_second_board(board, entries, digest, now):
        if board == "the-trading-company":
            raise RuntimeError("injected later-board failure")
        return original_apply_board(board, entries, digest, now)

    monkeypatch.setattr(reconcile, "_apply_board", fail_on_second_board)

    with pytest.raises(reconcile.ReconciliationBlocked, match="restored"):
        reconcile.apply_manifest(
            manifest_path,
            approval_path,
            recovery_root=recovery_root,
        )

    assert {board: _logical_dump(board) for board in BOARDS} == before
    failure_receipts = list(recovery_root.glob("*/failure-receipt.json"))
    assert len(failure_receipts) == 1
    failure = json.loads(failure_receipts[0].read_text(encoding="utf-8"))
    assert failure["status"] == "restored"
    assert failure["integrity"] == {board: "ok" for board in BOARDS}
