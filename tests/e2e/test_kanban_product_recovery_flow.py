"""End-to-end acceptance coverage for a governed product recovery story.

Includes the structural no-push boundary proof: a fake ``git`` executable
on PATH that logs every engine invocation and refuses ``push`` is used to
prove that the dispatcher, integrator, snapshot, API, CLI, observer, and
migration public paths never reach a remote-write verb, and that the
temporary bare remote stays byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
import hermes_cli.kanban_story_integration as integration_module
from hermes_cli.kanban_product_outcomes import CandidateEligibility
from hermes_cli import projects_db as pdb
from hermes_cli.plugins import PluginManager


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fixture exercises the required POSIX scripts/run_tests.sh project contract",
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
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
    board_metadata["repository"] = {
        "base_ref": "refs/heads/main",
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": [
                {
                    "argv": ["bash", "scripts/run_tests.sh"],
                    "workdir": ".",
                    "timeout_seconds": 30,
                }
            ],
            "epic_release": [
                {
                    "argv": ["bash", "scripts/run_tests.sh"],
                    "workdir": ".",
                    "timeout_seconds": 30,
                }
            ],
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

    plugin_manager = PluginManager()
    plugin_manager.discover_and_load()
    governance = plugin_manager._plugins["kanban-governance"]
    assert governance.enabled is True
    assert (
        governance.manifest.config_gate
        == "plugins.kanban-governance.enabled"
    )
    assert plugin_manager.has_hook("pre_tool_call")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "not-a-card")
    unauthorized = repo / "non-card-write.txt"
    decisions = plugin_manager.invoke_hook(
        "pre_tool_call",
        tool_name="write_file",
        args={"path": str(unauthorized), "content": "blocked"},
    )
    decision = next(item for item in decisions if item.get("action") == "block")
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
                "rejected_branch": branch,
                "rejected_sha": first_development_sha,
                "epic_tip_sha": rollback_target,
            },
            expected_run_id=failed_test.current_run_id,
            board=board,
        )

        directive = kb.active_rework_directive(conn, task_id)
        assert directive is not None
        assert directive.origin_phase == "test"
        assert directive.target_phase == "development"
        assert directive.rejected_branch == branch
        assert directive.rejected_sha == first_development_sha
        assert directive.epic_tip_sha == rollback_target
        recovery_context = kb.build_worker_context(conn, task_id)
        assert "## Required rework directive" in recovery_context
        assert first_development_sha in recovery_context
        assert recovery_context.index("## Required rework directive") < recovery_context.index(
            "## Prior attempts on this task"
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
        assert kb.active_rework_directive(conn, task_id) is None

        test_result = subprocess.run(
            [str(story_worktree / "scripts" / "run_tests.sh")],
            cwd=story_worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        assert test_result.returncode == 0, test_result.stderr
        contract = kb.repository_contract_for_board(board, repo_root=repo)
        assert contract is not None
        configured_verification = kb.run_verification(
            contract.verification["story_integration"],
            story_worktree,
            source_sha=second_development_sha,
            candidate_sha=second_development_sha,
            contract_digest=contract.digest,
            scope="story_integration",
            subject_id=task_id,
        )
        assert configured_verification.status == "passed"
        passed_test = _claim(conn, task_id, board=board, claimer="tester-passed")
        test_pin = kb._prepare_test_target(
            conn, task_id, story_worktree, board=board
        )
        assert test_pin == {
            "test_branch": branch,
            "test_head_sha": second_development_sha,
        }
        assert isinstance(test_pin, dict)
        (story_worktree / "README.md").write_text(
            "test-generated evidence\n", encoding="utf-8"
        )
        assert kb.complete_task(
            conn,
            task_id,
            summary="Fixture tests passed",
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {
                    "writer": {"agent": "claude-code"},
                    "tester": {"agent": "hermes", "result": "passed"}
                },
                **test_pin,
                "tests_run": ["scripts/run_tests.sh"],
            },
            expected_run_id=passed_test.current_run_id,
            board=board,
        )
        assert (story_worktree / "README.md").read_text(encoding="utf-8") == (
            "governed recovery fixture\n"
        )

        reviewer = kb.claim_review_task(
            conn, task_id, claimer="independent-reviewer"
        )
        assert reviewer is not None and reviewer.current_run_id is not None
        review_pin = kb._prepare_review_target(
            conn, task_id, story_worktree, board=board
        )
        assert review_pin == {
            "review_branch": branch,
            "review_base_sha": rollback_target,
            "review_head_sha": second_development_sha,
        }
        assert isinstance(review_pin, dict)
        (story_worktree / "README.md").write_text(
            "review-generated evidence\n", encoding="utf-8"
        )
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
                **review_pin,
            },
            expected_run_id=reviewer.current_run_id,
            board=board,
        )
        assert (story_worktree / "README.md").read_text(encoding="utf-8") == (
            "governed recovery fixture\n"
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

    development_handoffs = [
        event
        for event in events
        if event.kind == "handoff"
        and event.payload.get("from_step") == "development"
    ]
    development_shas = [event.payload["sha"] for event in development_handoffs]
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
    directive_rows = conn.execute(
        "SELECT status, rejected_sha, resolved_by_run_id "
        "FROM product_rework_directives WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    assert len(directive_rows) == 1
    assert directive_rows[0]["status"] == "resolved"
    assert directive_rows[0]["rejected_sha"] == first_development_sha
    assert directive_rows[0]["resolved_by_run_id"] == development_two.current_run_id
    integration = next(event for event in events if event.kind == "story_merged_to_main")
    policy = next(event for event in events if event.kind == "deployment_policy_evaluated")
    smoke = next(event for event in events if event.kind == "deployment_recorded")
    completed = next(event for event in events if event.kind == "completed")
    evidence = completed.payload["release_evidence"]
    assert evidence["development_handoffs"] == [
        {"event_id": event.id, "sha": event.payload["sha"]}
        for event in development_handoffs
    ]
    assert evidence["failed_test_run_ids"] == [failed_test.current_run_id]
    assert evidence["test_run_id"] == passed_test.current_run_id
    assert evidence["review_run_id"] == reviewer.current_run_id
    assert evidence["rework_event_ids"] == [rework[0].id]
    assert evidence["rework_count"] == 1
    assert evidence["integration_event_id"] == integration.id
    assert evidence["integration_sha"] == integration.payload["candidate_sha"]
    assert evidence["deployment_policy_event_id"] == policy.id
    assert evidence["deployment_policy"] == "required"
    assert evidence["deployment_record_event_id"] == smoke.id
    assert evidence["smoke_result"] == smoke.payload["smoke_result"]
    assert evidence["rollback_target"] == rollback_target
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


@pytest.mark.parametrize(
    ("failure_code", "expected_status", "expected_rework"),
    [
        ("merge_conflict", "rework_required", 1),
        ("timeout", "attention_required", 0),
    ],
)
def test_public_reconcile_routes_integration_failure_without_approval_or_graph_growth(
    governed_profile: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_code: str,
    expected_status: str,
    expected_rework: int,
) -> None:
    repo = tmp_path / f"integration-{failure_code}"
    repo.mkdir()
    board = f"integration-{failure_code}"
    kb.ensure_product_board_defaults(
        board,
        name="Integration ownership fixture",
        default_workdir=str(repo),
    )
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["product_workflow"]["handoff_v2"] = True
    metadata["repository"] = {
        "base_ref": "refs/heads/main",
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": [
                {
                    "argv": ["bash", "scripts/run_tests.sh"],
                    "workdir": ".",
                    "timeout_seconds": 30,
                }
            ],
            "epic_release": [
                {
                    "argv": ["bash", "scripts/run_tests.sh"],
                    "workdir": ".",
                    "timeout_seconds": 30,
                }
            ],
        },
        "ci_observation": {
            "provider": "github_actions",
            "required_workflows": ["CI"],
        },
        "boundary_evidence": {
            "test_globs": ["tests/**"],
            "fixture_globs": ["tests/fixtures/**"],
            "generated_paths": [],
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    now = 1_700_000_000
    source_sha = "1" * 40
    base_sha = "2" * 40
    branch = "story/owned-failure"
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="review",
        )
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
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
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(test_metadata), now - 4, now - 3),
        )
        review_run_id = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'review', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(review_metadata), now - 2, now - 1),
        ).lastrowid
        with kb.authorized_governance_write(), kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workflow_template_id='product_epic', "
                "current_step_key='collecting_members', status='todo', assignee=NULL, "
                "running=0, blocked=0, current_run_id=NULL WHERE id=?",
                (epic_id,),
            )
            conn.execute(
                "UPDATE tasks SET current_step_key='integration_pending', "
                "status='review', assignee=NULL, running=0, blocked=0, "
                "current_run_id=NULL, branch_name=? WHERE id=?",
                (branch, story_id),
            )
            conn.execute(
                "INSERT INTO story_integration_intents "
                "(epic_id, story_id, source_sha, source_branch, review_run_id, "
                "review_base_sha, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    epic_id,
                    story_id,
                    source_sha,
                    branch,
                    review_run_id,
                    base_sha,
                    now,
                    now,
                ),
            )
        before_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        before_links = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]
        before_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]

        monkeypatch.setattr(
            integration_module,
            "candidate_eligibility",
            lambda *_args: CandidateEligibility(source_sha, True),
        )

        observed_lineages = []

        def fail_preparation(_conn, intent, **_kwargs):
            observed_lineages.append(intent.key)
            if failure_code == "merge_conflict":
                raise RuntimeError("merge conflict")
            raise kb.IntegrationCandidateError("safe forced failure", code=failure_code)

        monkeypatch.setattr(
            integration_module,
            "prepare_claimed_intent",
            fail_preparation,
        )
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        if not expected_rework:
            retry_result = kb.reconcile(conn, board=board, spawn_ready=False)
            assert retry_result.integrated == []

        intent = conn.execute(
            "SELECT status, last_failure_code, attempt_count "
            "FROM story_integration_intents WHERE story_id=?",
            (story_id,),
        ).fetchone()
        task = kb.get_task(conn, story_id)
        directive = kb.active_rework_directive(conn, story_id)
        after_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        after_links = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]
        after_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        event_kinds = {
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=?",
                (story_id,),
            ).fetchall()
        }

    assert result.integrated == []
    expected_attempts = 1 if expected_rework else 2
    assert tuple(intent) == (expected_status, failure_code, expected_attempts)
    assert len(observed_lineages) == expected_attempts
    assert len(set(observed_lineages)) == 1
    assert task is not None and task.rework_count == expected_rework
    assert task.current_step_key == (
        "development" if expected_rework else "integration_pending"
    )
    assert task.current_step_key != "release_measure"
    assert (directive is not None) is bool(expected_rework)
    assert (after_tasks, after_links, after_runs) == (
        before_tasks,
        before_links,
        before_runs,
    )
    assert not event_kinds.intersection(
        {"approval_requested", "release_requested", "release_approved"}
    )


# ---------------------------------------------------------------------------
# No-push boundary proof (fake git across every public engine path)
# ---------------------------------------------------------------------------

def test_no_push_boundary_across_all_public_paths(
    governed_profile: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push-refusing fake git proves no engine public path issues a
    remote-write verb and the bare remote stays byte-for-byte unchanged.

    Exercises the dispatcher (``reconcile``), the integrator (story
    integration intent lifecycle), the snapshot path (prepare +
    invalidate), the dashboard API, the release-state CLI, the CI
    observer, and the v2-migrate CLI dry-run — all through the fake git.
    """
    from tests.e2e.test_kanban_epic_integration_release import (
        FakeGit,
        _ProductFixture,
        _create_epic,
        _create_epic_member,
        _default_product_board_metadata,
        _drive_story_to_review,
        _load_api_module,
    )

    fake_git = FakeGit(tmp_path / "fake-git", monkeypatch)

    board = "no-push-boundary"
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    remote.mkdir()
    clone.mkdir(parents=True)
    fake_git.real(tmp_path, "init", "--bare", "-b", "main", str(remote))
    fake_git.real(tmp_path, "clone", str(remote), str(clone))
    fake_git.real(clone, "config", "user.email", "no-push@e2e.test")
    fake_git.real(clone, "config", "user.name", "No Push Boundary")
    script_dir = clone / "tests" / "e2e_scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "run_tests.sh").write_text(
        "#!/bin/sh\nset -eu\necho ok\nexit 0\n", encoding="utf-8",
    )
    (script_dir / "run_tests.sh").chmod(0o755)
    (clone / ".gitignore").write_text("*.pyc\n__pycache__/\n")
    _default_product_board_metadata(board, clone)
    fake_git.real(clone, "add", ".gitignore", "tests/e2e_scripts/run_tests.sh")
    fake_git.real(clone, "commit", "-m", "initial")
    initial_sha = fake_git.real(clone, "rev-parse", "HEAD").stdout.strip()
    fake_git.real(clone, "push", "origin", "main")
    fake_git.reset_log()

    remote_refs_before = fake_git.real(remote, "show-ref").stdout

    product = _ProductFixture(
        board=board, repo=clone, remote=remote,
        fake_git=fake_git, initial_sha=initial_sha,
    )

    with kb.connect(board=board) as conn:
        epic_id = _create_epic(conn, "Epic: no-push proof")
        story_id, worktree, branch = _create_epic_member(
            conn, product, board, epic_id,
        )

        # Dispatcher + integrator: full story lifecycle through reconcile.
        _drive_story_to_review(conn, story_id, worktree, branch, board)
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert story_id in result.integrated

        # Snapshot path: prepare + drift invalidation.
        snap = kb.prepare_epic_release_snapshot(conn, epic_id, board=board)
        assert snap.status == "awaiting_push"
        fake_git.real(clone, "switch", "main")
        (clone / "marker.txt").write_text("advance\n", encoding="utf-8")
        fake_git.real(clone, "add", "marker.txt")
        fake_git.real(clone, "commit", "-m", "main advance")
        inv = kb.invalidate_epic_release_snapshot(conn, epic_id, board=board)
        assert inv.kind == "invalidated"

        # Observer path: read-only CI observation (target pre-image drift
        # invalidates again; nothing is pushed).
        obs = kb.observe_epic_release_ci(conn, epic_id, board=board)
        assert obs.kind in {"invalidated", "missing", "unavailable"}

        # API path: read-only release-state endpoints.
        dashboard = _load_api_module()
        app = FastAPI()
        app.include_router(dashboard.router, prefix="/api/plugins/kanban")
        api = TestClient(app)
        resp = api.get(
            f"/api/plugins/kanban/tasks/{epic_id}/release-state?board={board}",
        )
        assert resp.status_code == 200, resp.text
        resp2 = api.get(
            f"/api/plugins/kanban/tasks/{story_id}?board={board}",
        )
        assert resp2.status_code == 200, resp2.text

        # CLI path: release-state read model + v2-migrate dry-run handler.
        from hermes_cli import kanban as kanban_cli
        state = kanban_cli._task_release_state(conn, epic_id, board=board)
        assert state["kind"] == "epic"

        live_db = kb.kanban_db_path(board=board)
        with sqlite3.connect(str(live_db)) as raw:
            raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        scratch_db = tmp_path / "scratch-cli.db"
        shutil.copy2(live_db, scratch_db)
        migrate_args = argparse.Namespace(
            db_path=str(scratch_db), apply=False,
            recovery_root=None, json=True,
        )
        assert kanban_cli._cmd_v2_migrate(migrate_args) == 0

    remote_refs_after = fake_git.real(remote, "show-ref").stdout

    # The structural guarantee: zero push invocations across every public
    # path exercised above, and the remote untouched.
    assert fake_git.invocations, (
        "fake git observed no engine invocations — PATH wiring is broken"
    )
    assert fake_git.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fake_git.push_invocations}"
    )
    assert remote_refs_after == remote_refs_before, (
        "bare remote changed during engine activity:\n"
        f"before:\n{remote_refs_before}\nafter:\n{remote_refs_after}"
    )
