"""Release-blocking regressions recovered from the 2026-07-10 Kanban review."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "review@example.com")
    _git(repo, "config", "user.name", "Review")
    (repo / "scripts").mkdir()
    test_script = repo / "scripts" / "run_tests.sh"
    test_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    test_script.chmod(0o755)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")


def _product_board(board: str, repo: Path) -> None:
    kb.create_board(board, name="Review board", default_workdir=str(repo))
    meta = kb.read_board_metadata(board)
    meta.pop("db_path", None)
    meta["preset"] = "product"
    meta["columns"] = [
        {"name": "backlog", "status": "ready"},
        {"name": "architecture", "status": "ready"},
        {"name": "development", "status": "ready"},
        {"name": "test", "status": "ready"},
        {"name": "review", "status": "review"},
        {"name": "release_measure", "status": "ready"},
        {"name": "done", "status": "done"},
    ]
    meta["product_workflow"] = {
        "handoff_v2": True,
        "merge_after_green": False,
        "assignees": {
            "productowner": "productowner",
            "architect": "architect",
            "developer": "developer",
            "tester": "tester",
            "reviewer": "reviewer",
        },
    }
    kb.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    return home


def _story_branch(repo: Path) -> None:
    _git(repo, "switch", "-c", "wt/story")
    (repo / "story.txt").write_text("story\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "story")
    _git(repo, "switch", "main")


def test_default_merge_verifier_integrates_while_main_is_checked_out(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _story_branch(repo)
    board = "review-default-verifier"
    _product_board(board, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    with kb.connect(board=board) as conn:
        story_id = kb.create_task(
            conn,
            title="Story: verify real path",
            assignee="reviewer",
            board=board,
            branch_name="wt/story",
            workspace_kind="worktree",
            workspace_path=str(repo),
            workflow_template_id="product",
            current_step_key="done",
        )
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=1 WHERE id=?",
            (story_id,),
        )
        conn.commit()
        outcome = kb._merge_standalone_story_to_main(conn, story_id, board=board)

    assert outcome == "merged"
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert (repo / "story.txt").read_text(encoding="utf-8") == "story\n"
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_dirty_target_is_preserved_and_not_integrated(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _story_branch(repo)
    (repo / "base.txt").write_text("uncommitted user work\n", encoding="utf-8")
    board = "review-dirty-main"
    _product_board(board, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    with kb.connect(board=board) as conn:
        story_id = kb.create_task(
            conn,
            title="Story: preserve user work",
            assignee="reviewer",
            board=board,
            branch_name="wt/story",
            workspace_kind="worktree",
            workspace_path=str(repo),
            workflow_template_id="product",
            current_step_key="done",
        )
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=1 WHERE id=?",
            (story_id,),
        )
        conn.commit()
        outcome = kb._merge_standalone_story_to_main(
            conn, story_id, board=board, verify_fn=lambda _branch: True
        )

    assert outcome == "verify_failed"
    assert (repo / "base.txt").read_text(encoding="utf-8") == "uncommitted user work\n"
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "wt/story", "main"],
        check=False,
    )
    assert ancestor.returncode == 1


def test_old_completion_cannot_close_next_claim(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    board = "review-handoff-race"
    _product_board(board, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: race",
            assignee="architect",
            board=board,
            workflow_template_id="product",
            current_step_key="architecture",
        )
        first = kb.claim_task(conn, task_id, board=board, claimer="old")
        assert first is not None and first.current_run_id is not None
        first_run_id = first.current_run_id
        original_handoff = kb.handoff
        raced: dict[str, int] = {}

        def handoff_then_claim(*args, **kwargs):
            advanced = original_handoff(*args, **kwargs)
            with kb.connect(board=board) as other:
                second = kb.claim_task(other, task_id, board=board, claimer="new")
                assert second is not None and second.current_run_id is not None
                raced["run_id"] = second.current_run_id
            return advanced

        monkeypatch.setattr(kb, "handoff", handoff_then_claim)
        assert kb.complete_task(
            conn,
            task_id,
            summary="Architecture complete",
            expected_run_id=first_run_id,
            board=board,
        )
        after = kb.get_task(conn, task_id)
        second_run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id=?",
            (raced["run_id"],),
        ).fetchone()

    assert after is not None and after.current_run_id == raced["run_id"]
    assert second_run["ended_at"] is None
    assert second_run["outcome"] is None


def test_role_only_card_remains_scratch_work(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    board = "review-role-misclassification"
    _product_board(board, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Rotate staging API token",
            assignee="developer",
            board=board,
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.workflow_template_id is None
    assert task.current_step_key is None
    assert task.workspace_kind == "scratch"


def test_invalid_product_step_fails_closed_with_audit_event(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    board = "review-invalid-step"
    _product_board(board, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    with kb.connect(board=board) as conn:
        with pytest.raises(ValueError, match="invalid product workflow step"):
            kb.create_task(
                conn,
                title="Story with malformed step",
                assignee="specialist",
                board=board,
                workflow_template_id="product",
                current_step_key="typo-development",
            )
        task_id = kb.create_task(
            conn,
            title="Story: corrupt legacy row",
            assignee="developer",
            board=board,
            workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute(
            "UPDATE tasks SET current_step_key='typo-development' WHERE id=?",
            (task_id,),
        )
        conn.commit()
        with pytest.raises(kb.ProductWorkflowStateError):
            kb.complete_task(conn, task_id, summary="done", board=board)
        task = kb.get_task(conn, task_id)
        kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]

    assert task is not None and task.status == "ready"
    assert task.current_step_key == "typo-development"
    assert "completion_blocked_invalid_workflow" in kinds
