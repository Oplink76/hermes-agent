"""End-to-end acceptance coverage for a governed product recovery story."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_module(name: str, path: Path, *, package: bool = False):
    if package and "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []
        sys.modules["hermes_plugins"] = namespace
    spec = importlib.util.spec_from_file_location(
        name,
        path,
        submodule_search_locations=[str(path.parent)] if package else None,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = name
        module.__path__ = [str(path.parent)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _expected_snapshot(conn, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return {
        f"expected_{field}": value
        for field, value in kb.task_snapshot_from_row(row).items()
    }


def _claim(conn, task_id: str, *, board: str, claimer: str):
    claimed = kb.claim_task(conn, task_id, board=board, claimer=claimer)
    assert claimed is not None and claimed.current_run_id is not None
    return claimed


class _FakeReleaseAdapter:
    def __init__(self, rollback_target: str):
        self.rollback_target = rollback_target
        self.calls: list[tuple[str, str]] = []

    def release(self, task_id: str, revision: str) -> dict:
        self.calls.append((task_id, revision))
        return {
            "environment": "test/preprod",
            "revision": revision,
            "smoke_result": {
                "status": "passed",
                "test": "passed",
                "preprod": "passed",
            },
            "rollback_target": self.rollback_target,
            "runtime_evidence": {
                "health": "green",
                "test": "green",
                "preprod": "green",
            },
        }


@pytest.fixture
def governed_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        "plugins:\n  kanban-governance:\n    enabled: true\n",
        encoding="utf-8",
    )
    return home


def test_governed_product_story_recovers_through_release_and_done(
    governed_profile: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "product"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Governed Recovery Fixture")
    (repo / "README.md").write_text("governed recovery fixture\n", encoding="utf-8")
    script = repo / "scripts" / "run_tests.sh"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\nset -eu\ntest \"$(cat story.txt)\" = \"fixed\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    board = "governed-recovery-fixture"
    kb.ensure_product_board_defaults(
        board,
        name="Governed Recovery Fixture",
        default_workdir=str(repo),
    )
    metadata_path = kb.board_metadata_path(board)
    board_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    board_metadata["product_workflow"]["deployment_policy"] = "required"
    metadata_path.write_text(
        json.dumps(board_metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "README.md", "scripts/run_tests.sh", ".gitignore")
    _git(repo, "commit", "-m", "fixture: initialize governed product")
    rollback_target = _git(repo, "rev-parse", "HEAD")

    branch = "story/governed-recovery"
    story_worktree = repo / ".worktrees" / "governed-recovery"
    _git(repo, "worktree", "add", "-b", branch, str(story_worktree), "main")

    with pdb.connect() as conn:
        project_id = pdb.create_project(
            conn,
            name="Governed Recovery Fixture",
            folders=[str(repo)],
            primary_path=str(repo),
            board_slug=board,
        )

    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: recover governed product flow",
            assignee="product-owner",
            board=board,
            project_id=project_id,
            workspace_kind="worktree",
            workspace_path=str(story_worktree),
            branch_name=branch,
            workflow_template_id="product",
            current_step_key="backlog",
        )
        stale_snapshot = _expected_snapshot(conn, task_id)

    dashboard = _load_module(
        "hermes_kanban_recovery_fixture_api",
        repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py",
    )
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/plugins/kanban")
    api = TestClient(app)
    fresh = api.patch(
        f"/api/plugins/kanban/tasks/{task_id}?board={board}",
        json={"title": "Story: recover governed product flow (API-updated)", **stale_snapshot},
    )
    assert fresh.status_code == 200, fresh.text
    stale = api.patch(
        f"/api/plugins/kanban/tasks/{task_id}?board={board}",
        json={"priority": 2, **stale_snapshot},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["current"]["title"].endswith("(API-updated)")

    governance = _load_module(
        "hermes_plugins.kanban_governance_recovery_fixture",
        repo_root / "plugins" / "kanban-governance" / "__init__.py",
        package=True,
    )
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "not-a-card")
    unauthorized = repo / "non-card-write.txt"
    decision = governance._on_pre_tool_call(
        "write_file", {"path": str(unauthorized), "content": "blocked"}
    )
    assert decision is not None and decision["action"] == "block"
    assert "does not exist" in decision["message"]
    assert not unauthorized.exists()
    monkeypatch.delenv("HERMES_KANBAN_TASK")

    with kb.connect(board=board) as conn:
        old_claim = _claim(conn, task_id, board=board, claimer="old-product-owner")
        assert kb.reclaim_task(conn, task_id, reason="exercise stale completion")
        new_claim = _claim(conn, task_id, board=board, claimer="new-product-owner")
        assert not kb.complete_task(
            conn,
            task_id,
            summary="stale backlog completion",
            expected_run_id=old_claim.current_run_id,
            board=board,
        )
        after_stale = kb.get_task(conn, task_id)
        active_run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ?",
            (new_claim.current_run_id,),
        ).fetchone()
        assert after_stale is not None
        assert after_stale.current_run_id == new_claim.current_run_id
        assert active_run["ended_at"] is None and active_run["outcome"] is None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Backlog accepted",
            expected_run_id=new_claim.current_run_id,
            board=board,
        )

        architecture = _claim(conn, task_id, board=board, claimer="architect")
        assert kb.complete_task(
            conn,
            task_id,
            summary="Architecture accepted",
            expected_run_id=architecture.current_run_id,
            board=board,
        )

        development_one = _claim(conn, task_id, board=board, claimer="developer-one")
        (story_worktree / "story.txt").write_text("needs rework\n", encoding="utf-8")
        assert kb.complete_task(
            conn,
            task_id,
            summary="Initial implementation",
            metadata={
                "ai_provenance": {
                    "writer": {"agent": "claude-code", "branch": branch}
                }
            },
            expected_run_id=development_one.current_run_id,
            board=board,
        )
        development_handoffs = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "handoff"
            and event.payload.get("from_step") == "development"
        ]
        first_development_sha = development_handoffs[-1].payload["sha"]

        failed_test = _claim(conn, task_id, board=board, claimer="tester-failed")
        assert kb.complete_task(
            conn,
            task_id,
            summary="Tester requested rework",
            metadata={
                "workflow_outcome": {
                    "verdict": "changes_requested",
                    "target_step": "development",
                    "findings": ["story.txt must contain the fixed value"],
                },
                "ai_provenance": {
                    "tester": {"agent": "hermes", "result": "failed"}
                },
            },
            expected_run_id=failed_test.current_run_id,
            board=board,
        )

        development_two = _claim(conn, task_id, board=board, claimer="developer-two")
        (story_worktree / "story.txt").write_text("fixed\n", encoding="utf-8")
        assert kb.complete_task(
            conn,
            task_id,
            summary="Reworked implementation",
            metadata={
                "ai_provenance": {
                    "writer": {"agent": "claude-code", "branch": branch}
                }
            },
            expected_run_id=development_two.current_run_id,
            board=board,
        )
        development_handoffs = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "handoff"
            and event.payload.get("from_step") == "development"
        ]
        second_development_sha = development_handoffs[-1].payload["sha"]
        assert second_development_sha != first_development_sha

        test_result = subprocess.run(
            [str(story_worktree / "scripts" / "run_tests.sh")],
            cwd=story_worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        assert test_result.returncode == 0, test_result.stderr
        passed_test = _claim(conn, task_id, board=board, claimer="tester-passed")
        assert kb.complete_task(
            conn,
            task_id,
            summary="Fixture tests passed",
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {
                    "tester": {"agent": "hermes", "result": "passed"}
                },
                "tests_run": ["scripts/run_tests.sh"],
            },
            expected_run_id=passed_test.current_run_id,
            board=board,
        )

        reviewer = kb.claim_review_task(
            conn, task_id, claimer="independent-reviewer"
        )
        assert reviewer is not None and reviewer.current_run_id is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Independent review approved",
            metadata={
                "workflow_outcome": {"verdict": "approved"},
                "ai_provenance": {
                    "writer": {"agent": "claude-code"},
                    "reviewer": {
                        "agent": "codex",
                        "verdict": "approved",
                        "reviewed_branch": branch,
                        "reviewed_commit": second_development_sha,
                    },
                },
            },
            expected_run_id=reviewer.current_run_id,
            board=board,
        )

        release = _claim(conn, task_id, board=board, claimer="release-measure")
        adapter = _FakeReleaseAdapter(rollback_target)

        def verify_candidate(candidate: Path) -> bool:
            result = subprocess.run(
                [str(candidate / "scripts" / "run_tests.sh")],
                cwd=candidate,
                check=False,
                capture_output=True,
                text=True,
            )
            return result.returncode == 0

        released = kb.release_product_task(
            conn,
            task_id,
            board,
            verify_candidate,
            adapter,
            measurement_note="Fake test/preprod smoke passed.",
            expected_run_id=release.current_run_id,
        )
        assert released.released is True and released.status == "released"

        final_task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        runs = kb.list_runs(conn, task_id, include_active=True)

    assert final_task is not None
    assert final_task.status == "done" and final_task.current_step_key == "done"
    assert final_task.rework_count == 1

    development_shas = [
        event.payload["sha"]
        for event in events
        if event.kind == "handoff"
        and event.payload.get("from_step") == "development"
    ]
    assert development_shas == [first_development_sha, second_development_sha]
    failed_run = next(run for run in runs if run.id == failed_test.current_run_id)
    passed_run = next(run for run in runs if run.id == passed_test.current_run_id)
    review_run = next(run for run in runs if run.id == reviewer.current_run_id)
    assert failed_run.outcome == "rework_requested"
    assert failed_run.metadata["workflow_outcome"]["verdict"] == "changes_requested"
    assert passed_run.outcome == "advanced"
    assert passed_run.metadata["workflow_outcome"]["verdict"] == "passed"
    assert review_run.metadata["ai_provenance"]["writer"]["agent"] == "claude-code"
    assert review_run.metadata["ai_provenance"]["reviewer"]["agent"] == "codex"

    rework = [event for event in events if event.kind == "rework_requested"]
    assert len(rework) == 1 and rework[0].payload["rework_count"] == 1
    integration = next(event for event in events if event.kind == "story_merged_to_main")
    policy = next(event for event in events if event.kind == "deployment_policy_evaluated")
    smoke = next(event for event in events if event.kind == "deployment_recorded")
    completed = next(event for event in events if event.kind == "completed")
    evidence = completed.payload["release_evidence"]
    assert evidence["test_run_id"] == passed_test.current_run_id
    assert evidence["review_run_id"] == reviewer.current_run_id
    assert evidence["integration_event_id"] == integration.id
    assert evidence["integration_sha"] == integration.payload["candidate_sha"]
    assert evidence["deployment_policy_event_id"] == policy.id
    assert evidence["deployment_record_event_id"] == smoke.id
    assert policy.payload == {
        "policy": "required",
        "deployment_required": True,
        "deployment_occurred": True,
    }
    assert smoke.payload["smoke_result"]["status"] == "passed"
    assert smoke.payload["rollback_target"] == rollback_target
    assert adapter.calls == [(task_id, evidence["integration_sha"])]

    assert (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", branch, "main"],
            check=False,
        ).returncode
        == 0
    )
    worktree_paths = [
        Path(line.removeprefix("worktree "))
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    assert worktree_paths
    assert all(_git(worktree, "status", "--porcelain") == "" for worktree in worktree_paths)
