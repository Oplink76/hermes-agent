"""Release / Measure may reach done only with structured release evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def release_home(tmp_path, monkeypatch):
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


def _repo_with_story_branch(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "release@example.com")
    _git(repo, "config", "user.name", "Release Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    branch = "story/release-evidence"
    _git(repo, "switch", "-c", branch)
    (repo / "story.txt").write_text("released\n", encoding="utf-8")
    _git(repo, "add", "story.txt")
    _git(repo, "commit", "-m", "story")
    source_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    return repo, branch, source_sha


def _release_board(board: str, repo: Path, *, policy: str = "manual", **settings) -> None:
    kb.ensure_product_board_defaults(board, default_workdir=str(repo))
    path = kb.board_metadata_path(board)
    meta = json.loads(path.read_text(encoding="utf-8"))
    workflow = meta.setdefault("product_workflow", {})
    workflow["deployment_policy"] = policy
    workflow.update(settings)
    path.write_text(json.dumps(meta), encoding="utf-8")
    # ensure_product_board_defaults protects .worktrees/; commit that board
    # bootstrap edit so the target checkout is clean for real integration.
    if _git(repo, "status", "--porcelain"):
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore integration worktrees")


def _release_task(
    conn,
    board: str,
    repo: Path,
    branch: str,
    *,
    title: str = "Story: release evidence",
    parents: list[str] | None = None,
) -> str:
    return kb.create_task(
        conn,
        title=title,
        board=board,
        parents=parents or (),
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name=branch,
        workflow_template_id="product",
        current_step_key="release_measure",
    )


def _seed_structured_evidence(
    conn,
    task_id: str,
    branch: str,
    source_sha: str,
    *,
    include_test: bool = True,
    include_review: bool = True,
    reviewer: str = "codex",
    reviewed_commit: str | None = None,
) -> dict[str, int]:
    ids: dict[str, int] = {}
    with kb.write_txn(conn):
        ids["writer"] = kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="development",
            metadata={
                "ai_provenance": {
                    "writer": {
                        "agent": "claude-code",
                        "branch": branch,
                        "commit": source_sha,
                    }
                }
            },
        )
        if include_test:
            ids["test"] = kb._synthesize_ended_run(
                conn,
                task_id,
                outcome="advanced",
                step_key="test",
                metadata={
                    "workflow_outcome": {"verdict": "passed"},
                    "ai_provenance": {
                        "tester": {"agent": "hermes", "result": "passed"}
                    },
                },
            )
        if include_review:
            ids["review"] = kb._synthesize_ended_run(
                conn,
                task_id,
                outcome="advanced",
                step_key="review",
                metadata={
                    "workflow_outcome": {"verdict": "approved"},
                    "ai_provenance": {
                        "writer": {"agent": "claude-code"},
                        "reviewer": {
                            "agent": reviewer,
                            "verdict": "approved",
                            "reviewed_branch": branch,
                            "reviewed_commit": reviewed_commit or source_sha,
                        },
                    },
                },
            )
    return ids


def test_product_board_defaults_to_manual_deployment_policy(release_home):
    board = "release-default-policy"
    kb.ensure_product_board_defaults(board)
    workflow = kb.read_board_metadata(board)["product_workflow"]
    assert workflow["deployment_policy"] == "manual"


@pytest.mark.parametrize(
    ("seed_overrides", "missing"),
    [
        ({"include_test": False}, "tester_pass"),
        ({"include_review": False}, "reviewer_approval"),
        ({"reviewer": "claude-code"}, "independent_reviewer"),
        ({"reviewed_commit": "0" * 40}, "reviewed_candidate"),
    ],
)
def test_release_rejects_missing_or_untrustworthy_run_evidence(
    release_home, tmp_path, monkeypatch, seed_overrides, missing,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = f"release-missing-{missing.replace('_', '-')}"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        task_id = _release_task(conn, board, repo, branch)
        _seed_structured_evidence(
            conn, task_id, branch, source_sha, **seed_overrides,
        )
        integrate = Mock(return_value="merged")
        monkeypatch.setattr(kb, "_merge_standalone_story_to_main", integrate)

        with pytest.raises(kb.ReleaseEvidenceError) as exc_info:
            kb.release_product_task(
                conn, task_id, board, lambda _path: True, None,
                measurement_note="measured",
            )

        assert missing in exc_info.value.missing
        integrate.assert_not_called()
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.current_step_key == "release_measure"


def test_standalone_release_integrates_before_done_and_attaches_terminal_evidence(
    release_home, tmp_path,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-standalone-order"
    _release_board(board, repo, policy="manual")
    with kb.connect(board=board) as conn:
        task_id = _release_task(conn, board, repo, branch)
        run_ids = _seed_structured_evidence(conn, task_id, branch, source_sha)

        result = kb.release_product_task(
            conn,
            task_id,
            board,
            lambda candidate: (candidate / "story.txt").read_text() == "released\n",
            None,
            measurement_note="No automatic deployment; release measured manually.",
        )

        assert result.released is True
        assert result.status == "released"
        assert (repo / "story.txt").read_text(encoding="utf-8") == "released\n"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"
        assert task.current_step_key == "done"

        events = kb.list_events(conn, task_id)
        integration = next(e for e in events if e.kind == "story_merged_to_main")
        policy = next(e for e in events if e.kind == "deployment_policy_evaluated")
        completed = next(e for e in events if e.kind == "completed")
        assert integration.id < policy.id < completed.id
        evidence = completed.payload["release_evidence"]
        assert evidence["test_run_id"] == run_ids["test"]
        assert evidence["review_run_id"] == run_ids["review"]
        assert evidence["integration_event_id"] == integration.id
        assert evidence["integration_sha"] == integration.payload["candidate_sha"]
        assert evidence["deployment_policy_event_id"] == policy.id
        assert evidence["deployment_record_event_id"] is None
        assert evidence["measurement_note"].startswith("No automatic deployment")
        assert policy.payload == {
            "policy": "manual",
            "deployment_required": False,
            "deployment_occurred": False,
        }


@pytest.mark.parametrize("policy_name", ["manual", "not_required"])
def test_non_deploy_policy_is_recorded_without_fake_deployment(
    release_home, tmp_path, policy_name,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = f"release-policy-{policy_name.replace('_', '-')}"
    _release_board(board, repo, policy=policy_name)
    with kb.connect(board=board) as conn:
        task_id = _release_task(conn, board, repo, branch)
        _seed_structured_evidence(conn, task_id, branch, source_sha)
        result = kb.release_product_task(
            conn, task_id, board, lambda _path: True, None,
            measurement_note="explicit policy evaluation",
        )

        assert result.released is True
        events = kb.list_events(conn, task_id)
        evaluated = next(e for e in events if e.kind == "deployment_policy_evaluated")
        assert evaluated.payload["policy"] == policy_name
        assert evaluated.payload["deployment_occurred"] is False
        assert not any(e.kind == "deployment_recorded" for e in events)


def test_required_deployment_without_adapter_stays_in_release_measure(
    release_home, tmp_path,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-adapter-missing"
    _release_board(board, repo, policy="required")
    with kb.connect(board=board) as conn:
        task_id = _release_task(conn, board, repo, branch)
        _seed_structured_evidence(conn, task_id, branch, source_sha)

        result = kb.release_product_task(
            conn, task_id, board, lambda _path: True, None,
            measurement_note="deployment required",
        )

        assert result.released is False
        assert result.status == "release_adapter_missing"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.current_step_key == "release_measure"
        events = kb.list_events(conn, task_id)
        assert any(e.kind == "release_adapter_missing" for e in events)
        assert not any(e.kind == "completed" for e in events)


class _ReleaseAdapter:
    def __init__(self, record: dict):
        self.record = record
        self.calls: list[tuple[str, str]] = []

    def release(self, task_id: str, revision: str) -> dict:
        self.calls.append((task_id, revision))
        return dict(self.record)


class _SuccessfulReleaseAdapter:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def release(self, task_id: str, revision: str) -> dict:
        self.calls.append((task_id, revision))
        return {
            "environment": "preprod",
            "revision": revision,
            "smoke_result": True,
            "rollback_target": "preprod-previous",
            "runtime_evidence": {"health": "green"},
        }


def test_required_deployment_records_runtime_evidence_before_done(
    release_home, tmp_path,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-adapter-success"
    _release_board(board, repo, policy="required")
    with kb.connect(board=board) as conn:
        task_id = _release_task(conn, board, repo, branch)
        _seed_structured_evidence(conn, task_id, branch, source_sha)
        adapter = _SuccessfulReleaseAdapter()

        result = kb.release_product_task(
            conn, task_id, board, lambda _path: True, adapter,
            measurement_note="runtime measured",
        )

        assert result.released is True
        assert adapter.calls == [(task_id, result.integration_sha)]
        events = kb.list_events(conn, task_id)
        deployment = next(e for e in events if e.kind == "deployment_recorded")
        completed = next(e for e in events if e.kind == "completed")
        assert (
            completed.payload["release_evidence"]["deployment_record_event_id"]
            == deployment.id
        )
        assert deployment.payload["rollback_target"] == "preprod-previous"
        assert deployment.payload["runtime_evidence"] == {"health": "green"}


def test_required_pull_request_is_referenced_by_terminal_evidence(
    release_home, tmp_path,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-pr-required"
    _release_board(board, repo, pull_request_required=True)
    with kb.connect(board=board) as conn:
        task_id = _release_task(conn, board, repo, branch)
        _seed_structured_evidence(conn, task_id, branch, source_sha)

        result = kb.release_product_task(
            conn, task_id, board, lambda _path: True, None,
            measurement_note="PR release measured",
            completion_metadata={"pull_request": "https://example.test/pr/42"},
        )

        assert result.released is True
        completed = next(
            e for e in kb.list_events(conn, task_id) if e.kind == "completed"
        )
        assert completed.payload["release_evidence"]["pull_request"] == (
            "https://example.test/pr/42"
        )


def test_required_deployment_rejects_missing_smoke_or_rollback_evidence(
    release_home, tmp_path,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-adapter-incomplete"
    _release_board(board, repo, policy="required")
    with kb.connect(board=board) as conn:
        task_id = _release_task(conn, board, repo, branch)
        _seed_structured_evidence(conn, task_id, branch, source_sha)
        adapter = _ReleaseAdapter(
            {
                "environment": "preprod",
                "revision": source_sha,
                "smoke_result": True,
                "runtime_evidence": {"health": "green"},
            }
        )

        with pytest.raises(kb.ReleaseEvidenceError) as exc_info:
            kb.release_product_task(
                conn, task_id, board, lambda _path: True, adapter,
                measurement_note="deployment attempted",
            )

        assert "rollback_evidence" in exc_info.value.missing
        task = kb.get_task(conn, task_id)
        assert task is not None and task.current_step_key == "release_measure"


def test_epic_child_integrates_before_child_done(release_home, tmp_path, monkeypatch):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-epic-child"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board)
        story = _release_task(conn, board, repo, branch, parents=[epic])
        _seed_structured_evidence(conn, story, branch, source_sha)

        def integrate_before_done(inner_conn, story_id, **_kwargs):
            assert kb.get_task(inner_conn, story_id).status != "done"
            with kb.write_txn(inner_conn):
                kb._append_event(
                    inner_conn,
                    story_id,
                    "story_integrated_to_epic",
                    {
                        "source_branch": branch,
                        "target_branch": kb.epic_branch_for(epic),
                        "candidate_sha": source_sha,
                    },
                )
            return "integrated"

        monkeypatch.setattr(kb, "integrate_story_to_epic", integrate_before_done)
        result = kb.release_product_task(
            conn, story, board, lambda _path: True, None,
            measurement_note="child integrated",
        )

        assert result.released is True
        assert kb.get_task(conn, story).status == "done"


def test_epic_release_requires_every_child_done_and_integrated(
    release_home, tmp_path, monkeypatch,
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-epic"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        epic = _release_task(conn, board, repo, branch, title="Epic: release")
        child = kb.create_task(conn, title="Story: child", board=board, parents=[epic])
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', current_step_key='done' WHERE id=?",
                (child,),
            )
        _seed_structured_evidence(conn, epic, branch, source_sha)
        merge = Mock(return_value="merged")
        monkeypatch.setattr(kb, "merge_epic_to_main", merge)

        with pytest.raises(kb.ReleaseEvidenceError) as exc_info:
            kb.release_product_task(
                conn, epic, board, lambda _path: True, None,
                measurement_note="epic release",
            )
        assert "integrated_children" in exc_info.value.missing
        merge.assert_not_called()

        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child,
                "story_integrated_to_epic",
                {
                    "target_branch": kb.epic_branch_for(epic),
                    "candidate_sha": source_sha,
                },
            )

        def merge_before_done(inner_conn, epic_id, **_kwargs):
            assert kb.get_task(inner_conn, epic_id).status != "done"
            with kb.write_txn(inner_conn):
                kb._append_event(
                    inner_conn,
                    epic_id,
                    "epic_merged",
                    {
                        "target_branch": "main",
                        "candidate_sha": source_sha,
                    },
                )
            return "merged"

        monkeypatch.setattr(kb, "merge_epic_to_main", merge_before_done)
        result = kb.release_product_task(
            conn, epic, board, lambda _path: True, None,
            measurement_note="epic release",
        )
        assert result.released is True
        assert kb.get_task(conn, epic).status == "done"
