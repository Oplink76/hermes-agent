from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kc
from hermes_cli import kanban_qualification_migrate as migrate


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def product_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = "product"
    kb.ensure_product_board_defaults(board, default_workdir=str(tmp_path / "repo"))
    return home, board


def _create_legacy_fixture(board: str) -> dict[str, str]:
    with kb.connect(board=board) as conn:
        epic = kb.create_task(
            conn, title="Epic: Release Alpha", body="Explicit legacy Epic.",
            initial_status="running",
        )
        member = kb.create_task(
            conn, title="Story: Ship Alpha", body="User-visible release slice.",
            assignee="default", parents=(epic,), initial_status="running",
            workflow_template_id="product", current_step_key="development",
        )
        ordinary_parent = kb.create_task(
            conn, title="Prepare API", assignee="architect", initial_status="running",
            workflow_template_id="product", current_step_key="architecture",
        )
        ordinary_child = kb.create_task(
            conn, title="Refactor order router", body="Maintenance work.",
            assignee="developer", parents=(ordinary_parent,), initial_status="running",
            workspace_kind="worktree", branch_name="product/refactor-router",
            workflow_template_id="product", current_step_key="development",
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', running = 0, current_run_id = NULL "
            "WHERE id IN (?, ?, ?, ?)",
            (epic, member, ordinary_parent, ordinary_child),
        )
        conn.execute(
            "UPDATE tasks SET project_id = 'p_alpha', created_at = 111, "
            "started_at = 222 WHERE id = ?",
            (ordinary_child,),
        )
        kb.add_comment(conn, ordinary_child, "tester", "Keep this trace.")
        cursor = conn.execute(
            "INSERT INTO task_runs (task_id, profile, step_key, status, started_at, ended_at, summary) "
            "VALUES (?, 'developer', 'development', 'done', 333, 444, 'Verified run.')",
            (ordinary_child,),
        )
        run_id = int(cursor.lastrowid)
        attachment_dir = kb.task_attachments_dir(ordinary_child, board=board)
        attachment_dir.mkdir(parents=True, exist_ok=True)
        attachment = attachment_dir / "evidence.txt"
        attachment.write_text("evidence", encoding="utf-8")
        kb.add_attachment(
            conn, ordinary_child, filename="evidence.txt", stored_path=str(attachment),
            content_type="text/plain", size=8, uploaded_by="tester",
        )
    return {
        "epic": epic,
        "member": member,
        "ordinary_parent": ordinary_parent,
        "ordinary_child": ordinary_child,
    }


def test_dry_run_is_read_only_and_reports_safe_routes(product_board):
    _home, board = product_board
    ids = _create_legacy_fixture(board)
    db_path = kb.kanban_db_path(board)
    metadata_path = kb.board_metadata_path(board)
    before = (_sha(db_path), _sha(metadata_path))

    report = migrate.audit_board(board)

    assert (_sha(db_path), _sha(metadata_path)) == before
    by_id = {item["id"]: item for item in report["items"]}
    assert by_id[ids["member"]]["proposed"]["assignee"] == "developer"
    assert by_id[ids["ordinary_child"]]["proposed"]["work_type"] == "maintenance"
    assert by_id[ids["ordinary_child"]]["relations"] == "dependency"
    assert by_id[ids["epic"]]["proposed"]["work_item_kind"] == "epic"
    assert report["strict_ready"] is True


def test_cli_exposes_json_dry_run_for_scoped_product_board(product_board):
    _home, board = product_board
    _create_legacy_fixture(board)

    payload = json.loads(
        kc.run_slash(f"--board {board} qualification-migrate --json")
    )

    assert payload["board"] == board
    assert payload["mode"] == "dry-run"
    assert payload["counts"]["items"] == 4


def test_apply_preserves_evidence_and_converts_only_explicit_epic_links(
    product_board, tmp_path
):
    _home, board = product_board
    ids = _create_legacy_fixture(board)
    with kb.connect(board=board) as conn:
        task_before = dict(conn.execute(
            "SELECT id, branch_name, project_id, created_at, started_at FROM tasks WHERE id = ?",
            (ids["ordinary_child"],),
        ).fetchone())
        comments_before = [dict(row) for row in conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id",
            (ids["ordinary_child"],),
        )]
        runs_before = [dict(row) for row in conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (ids["ordinary_child"],),
        )]
        attachments_before = [dict(row) for row in conn.execute(
            "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY id",
            (ids["ordinary_child"],),
        )]

    result = migrate.apply_board(board, recovery_root=tmp_path / "recovery")

    assert result["strict_enabled"] is True
    assert Path(result["receipt_path"]).is_file()
    with kb.connect(board=board) as conn:
        task_after = dict(conn.execute(
            "SELECT id, branch_name, project_id, created_at, started_at FROM tasks WHERE id = ?",
            (ids["ordinary_child"],),
        ).fetchone())
        assert task_after == task_before
        assert [dict(row) for row in conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id",
            (ids["ordinary_child"],),
        )] == comments_before
        assert [dict(row) for row in conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (ids["ordinary_child"],),
        )] == runs_before
        assert [dict(row) for row in conn.execute(
            "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY id",
            (ids["ordinary_child"],),
        )] == attachments_before
        assert conn.execute(
            "SELECT 1 FROM epic_memberships WHERE epic_id = ? AND task_id = ?",
            (ids["epic"], ids["member"]),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (ids["epic"], ids["member"]),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (ids["ordinary_parent"], ids["ordinary_child"]),
        ).fetchone()
        member_contract_id = conn.execute(
            "SELECT work_contract_id FROM tasks WHERE id = ?", (ids["member"],)
        ).fetchone()[0]
        member_contract = kb.get_work_contract(conn, member_contract_id)["contract"]
        assert member_contract["routing"]["epic_id"] == ids["epic"]
        dependency_contract_id = conn.execute(
            "SELECT work_contract_id FROM tasks WHERE id = ?",
            (ids["ordinary_child"],),
        ).fetchone()[0]
        dependency_contract = kb.get_work_contract(
            conn, dependency_contract_id
        )["contract"]
        assert dependency_contract["routing"]["dependencies"] == [
            ids["ordinary_parent"]
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status != 'archived' AND work_contract_id IS NULL"
        ).fetchone()[0] == 0

    metadata = json.loads(kb.board_metadata_path(board).read_text(encoding="utf-8"))
    assert metadata["qualification"] == {
        "required": True,
        "contract_version": 1,
        "policy_version": "product-handoff-v2+qualification-v1",
        "paths": ["po", "hermes"],
        "work_types": ["story", "bug", "maintenance", "ops", "spike"],
        "phase_assignees": {
            "backlog": "productowner", "architecture": "architect",
            "development": "developer", "test": "tester", "review": "reviewer",
            "release_measure": None,
        },
    }


def test_running_work_blocks_apply_without_mutating_board(product_board, tmp_path):
    _home, board = product_board
    with kb.connect(board=board) as conn:
        task = kb.create_task(
            conn, title="Running task", assignee="developer", initial_status="running",
            workflow_template_id="product", current_step_key="development",
        )
        cursor = conn.execute(
            "INSERT INTO task_runs (task_id, profile, step_key, status, started_at) "
            "VALUES (?, 'developer', 'development', 'running', 1)",
            (task,),
        )
        run_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE tasks SET current_run_id = ?, running = 1 WHERE id = ?",
            (run_id, task),
        )
    db_before = _sha(kb.kanban_db_path(board))

    with pytest.raises(migrate.MigrationBlocked, match="active running work"):
        migrate.apply_board(board, recovery_root=tmp_path / "recovery")

    assert _sha(kb.kanban_db_path(board)) == db_before
    assert kb.read_board_metadata(board)["qualification"]["required"] is False
    with kb.connect(board=board) as conn:
        row = conn.execute(
            "SELECT work_contract_id, current_run_id, running FROM tasks WHERE id = ?", (task,)
        ).fetchone()
        assert tuple(row) == (None, run_id, 1)


def test_ambiguous_epic_membership_is_reported_not_guessed(product_board, tmp_path):
    _home, board = product_board
    with kb.connect(board=board) as conn:
        epic_a = kb.create_task(
            conn, title="Epic: Alpha", initial_status="running"
        )
        epic_b = kb.create_task(
            conn, title="Epic: Beta", initial_status="running"
        )
        member = kb.create_task(
            conn,
            title="Shared story",
            parents=(epic_a, epic_b),
            initial_status="running",
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', running = 0 WHERE id IN (?, ?, ?)",
            (epic_a, epic_b, member),
        )

    report = migrate.audit_board(board)
    item = next(item for item in report["items"] if item["id"] == member)

    assert item["result"] == "ambiguous"
    assert item["proposed"]["epic_id"] is None
    assert report["strict_ready"] is False
    with pytest.raises(migrate.MigrationBlocked, match="ambiguous legacy Epic"):
        migrate.apply_board(board, recovery_root=tmp_path / "recovery")


def test_apply_is_idempotent_and_repairs_interrupted_metadata_activation(
    product_board, tmp_path
):
    _home, board = product_board
    _create_legacy_fixture(board)
    first = migrate.apply_board(board, recovery_root=tmp_path / "recovery")
    metadata = json.loads(kb.board_metadata_path(board).read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = False
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")

    second = migrate.apply_board(board, recovery_root=tmp_path / "recovery")

    assert first["changed"] > 0
    assert second["changed"] == 0
    assert second["strict_enabled"] is True


def test_rollback_restores_snapshot_and_keeps_immutable_receipt(product_board, tmp_path):
    home, board = product_board
    ids = _create_legacy_fixture(board)
    metadata_before = kb.board_metadata_path(board).read_text(encoding="utf-8")

    result = migrate.apply_board(board, recovery_root=tmp_path / "recovery")
    receipt = Path(result["receipt_path"])
    rollback = migrate.rollback_receipt(receipt)

    assert rollback["restored"] is True
    assert receipt.exists()
    assert receipt.stat().st_mode & 0o222 == 0
    assert kb.board_metadata_path(board).read_text(encoding="utf-8") == metadata_before
    assert not (home / "kanban" / "work_contract_signing.key").exists()
    with kb.connect(board=board) as conn:
        task = conn.execute(
            "SELECT work_contract_id FROM tasks WHERE id = ?", (ids["ordinary_child"],)
        ).fetchone()
        assert task["work_contract_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT qualification_required FROM board_governance WHERE id = 1"
        ).fetchone()[0] == 0
