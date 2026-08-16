from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path):
    connection = kb.connect(tmp_path / "kanban.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def epic_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_moved_head(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "epic@example.com")
    _git(repo, "config", "user.name", "Epic Tests")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)
    (repo / "moved.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "moved.txt")
    _git(repo, "commit", "-m", "move main")
    assert _git(repo, "rev-parse", "HEAD") != base_sha
    return repo, base_sha


def _repository_policy(base_ref: str) -> dict[str, object]:
    command = {
        "argv": ["python", "-m", "unittest"],
        "workdir": ".",
        "timeout_seconds": 60,
    }
    return {
        "base_ref": base_ref,
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": {"commands": [command]},
            "epic_release": {"commands": [command]},
        },
        "ci_observation": {
            "provider": "github_actions",
            "required_workflows": ["CI"],
        },
        "boundary_evidence": {
            "test_globs": ["tests/**"],
            "fixture_globs": ["tests/fixtures/**"],
            "generated_paths": ["README.md"],
        },
    }


def _configured_epic_story(
    board: str, repo: Path, *, base_ref: str
) -> tuple[str, str]:
    kb.ensure_product_board_defaults(
        board,
        default_workdir=str(repo),
        repository=_repository_policy(base_ref),
    )
    with kb.connect(board=board) as connection:
        epic_id = kb.create_task(
            connection,
            title="Release outcome",
            board=board,
            work_item_kind="epic",
        )
        story_id = kb.create_task(
            connection,
            title="Story",
            board=board,
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            workflow_template_id="product",
            current_step_key="development",
        )
        kb.add_epic_membership(connection, epic_id=epic_id, task_id=story_id)
    return epic_id, story_id


def test_epic_progress_comes_only_from_explicit_membership(conn):
    epic = kb.create_task(conn, title="Portfolio outcome", work_item_kind="epic")
    member = kb.create_task(conn, title="Member")
    dependency = kb.create_task(conn, title="Acceptance dependency")
    kb.add_epic_membership(conn, epic_id=epic, task_id=member)
    kb.link_tasks(conn, dependency, member)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (member,))

    assert kb.epic_id_for_task(conn, member) == epic
    assert kb.epic_id_for_task(conn, dependency) is None
    assert kb.epic_progress(conn, epic) == {
        "done": 1,
        "total": 1,
        "release_state": "pending",
    }


def test_title_and_dependency_edges_never_create_epic_behavior(conn):
    titled_parent = kb.create_task(conn, title="Epic: only a title")
    child = kb.create_task(conn, title="Standalone card")
    kb.link_tasks(conn, titled_parent, child)

    assert kb._is_epic_task(conn, titled_parent) is False
    assert kb.epic_id_for_task(conn, child) is None
    assert kb.release_scope_for_task(conn, child) == "standalone"


def test_one_card_has_at_most_one_epic_but_dependencies_remain_unbounded(conn):
    first = kb.create_task(conn, title="First outcome", work_item_kind="epic")
    second = kb.create_task(conn, title="Second outcome", work_item_kind="epic")
    card = kb.create_task(conn, title="Card")
    dependency_a = kb.create_task(conn, title="Dependency A")
    dependency_b = kb.create_task(conn, title="Dependency B")
    kb.add_epic_membership(conn, epic_id=first, task_id=card)
    kb.link_tasks(conn, dependency_a, card)
    kb.link_tasks(conn, dependency_b, card)

    with pytest.raises(Exception):
        kb.add_epic_membership(conn, epic_id=second, task_id=card)
    assert kb.parent_ids(conn, card) == sorted([dependency_a, dependency_b])
    assert kb.epic_id_for_task(conn, card) == first


def test_epic_members_may_enter_at_different_valid_phases(conn):
    epic = kb.create_task(conn, title="Cross-phase outcome", work_item_kind="epic")
    architecture = kb.create_task(
        conn,
        title="Architecture member",
        assignee="architect",
        workflow_template_id="product",
        current_step_key="architecture",
    )
    review = kb.create_task(
        conn,
        title="Review member",
        assignee="reviewer",
        workflow_template_id="product",
        current_step_key="review",
    )
    kb.add_epic_membership(conn, epic_id=epic, task_id=architecture)
    kb.add_epic_membership(conn, epic_id=epic, task_id=review)

    assert kb.list_epic_members(conn, epic) == sorted([architecture, review])
    assert kb.get_task(conn, architecture).current_step_key == "architecture"
    assert kb.get_task(conn, review).current_step_key == "review"


def test_epic_cannot_be_completed_by_an_ordinary_task_completion(conn):
    epic = kb.create_task(conn, title="Release container", work_item_kind="epic")
    member = kb.create_task(conn, title="Done member")
    kb.add_epic_membership(conn, epic_id=epic, task_id=member)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (member,))

    assert kb.complete_task(conn, epic, summary="members done") is False
    assert kb.get_task(conn, epic).status != "done"
    assert any(
        event.kind == "completion_blocked_epic_release"
        for event in kb.list_events(conn, epic)
    )


def test_configured_epic_base_uses_ref_not_ambient_head(epic_home, tmp_path):
    repo, base_sha = _repo_with_moved_head(tmp_path)
    epic_id, story_id = _configured_epic_story(
        "configured-epic-base",
        repo,
        base_ref="refs/remotes/origin/main",
    )

    with kb.connect(board="configured-epic-base") as connection:
        story = kb.get_task(connection, story_id)
        assert story is not None
        kb._resolve_worktree_workspace(
            story,
            board="configured-epic-base",
            conn=connection,
        )
        pin = kb._epic_base_pinned_sha(connection, epic_id)

    assert pin == base_sha
    assert _git(repo, "rev-parse", kb.epic_branch_for(epic_id)) == base_sha


@pytest.mark.parametrize("base_ref", ["refs/remotes/origin/missing", "shared"])
def test_missing_or_ambiguous_configured_base_fails_before_epic_mutation(
    epic_home, tmp_path, base_ref: str
):
    repo, _base_sha = _repo_with_moved_head(tmp_path)
    if base_ref == "shared":
        _git(repo, "branch", "shared")
        _git(repo, "tag", "shared")
    epic_id, story_id = _configured_epic_story(
        f"bad-epic-base-{base_ref.split('/')[-1]}",
        repo,
        base_ref=base_ref,
    )
    board = f"bad-epic-base-{base_ref.split('/')[-1]}"

    with kb.connect(board=board) as connection:
        story = kb.get_task(connection, story_id)
        assert story is not None
        with pytest.raises(kb.RepositoryConfigurationError) as exc_info:
            kb._resolve_worktree_workspace(story, board=board, conn=connection)
        assert exc_info.value.code == "missing_ref"
        assert kb._epic_base_pinned_sha(connection, epic_id) is None
        assert connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
            (epic_id, kb.EPIC_BASE_PINNED_EVENT),
        ).fetchone()[0] == 0

    assert not kb._git_branch_exists(repo, kb.epic_branch_for(epic_id))


def test_public_epic_readiness_uses_current_durable_fact_not_story_events(
    epic_home, tmp_path
):
    repo, base_sha = _repo_with_moved_head(tmp_path)
    board = "fact-derived-epic-ready"
    epic_id, story_id = _configured_epic_story(
        board, repo, base_ref="refs/heads/main"
    )
    branch = "story/ready"
    _git(repo, "switch", "-c", branch)
    (repo / "story.txt").write_text("ready\n", encoding="utf-8")
    _git(repo, "add", "story.txt")
    _git(repo, "commit", "-m", "story")
    source_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    epic_branch = kb.epic_branch_for(epic_id)
    _git(repo, "update-ref", f"refs/heads/{epic_branch}", source_sha)
    now = int(time.time())
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "tester": {"agent": "tester", "result": "passed"},
        },
        "test_branch": branch,
        "test_head_sha": source_sha,
    }
    review_metadata = {
        "workflow_outcome": {"verdict": "approved"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "reviewer": {"agent": "reviewer"},
        },
        "review_branch": branch,
        "review_base_sha": base_sha,
        "review_head_sha": source_sha,
    }

    with kb.connect(board=board) as connection:
        connection.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(test_metadata), now - 4, now - 3),
        )
        review_run_id = connection.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'review', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(review_metadata), now - 2, now - 1),
        ).lastrowid
        connection.execute(
            "UPDATE tasks SET status='done', current_step_key='done', running=0, "
            "blocked=0, current_run_id=NULL, branch_name=? WHERE id=?",
            (branch, story_id),
        )
        connection.execute(
            "INSERT INTO story_integration_intents ("
            "epic_id, story_id, source_sha, source_branch, review_run_id, review_base_sha, "
            "status, candidate_sha, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 'integrated', ?, ?, ?)",
            (
                epic_id,
                story_id,
                source_sha,
                branch,
                review_run_id,
                base_sha,
                source_sha,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO epic_story_integrations "
            "(epic_id, story_id, source_sha, candidate_sha, integrated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (epic_id, story_id, source_sha, source_sha, now),
        )
        event_id = connection.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'repository_verification', '{}', ?)",
            (story_id, now),
        ).lastrowid

        assert kb.epic_ready(
            connection, epic_id, board=board, verify_fn=lambda _branch: True
        ) is True
        connection.execute("DELETE FROM task_events WHERE id=?", (event_id,))
        assert kb.epic_ready(
            connection, epic_id, board=board, verify_fn=lambda _branch: True
        ) is True
        empty_metadata = dict(review_metadata)
        empty_metadata["review_base_sha"] = source_sha
        connection.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(empty_metadata), review_run_id),
        )
        assert kb.epic_ready(
            connection, epic_id, board=board, verify_fn=lambda _branch: True
        ) is False
        connection.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(review_metadata), review_run_id),
        )
        connection.execute(
            "DELETE FROM epic_story_integrations WHERE epic_id=? AND story_id=?",
            (epic_id, story_id),
        )
        assert kb.epic_ready(
            connection, epic_id, board=board, verify_fn=lambda _branch: True
        ) is False
