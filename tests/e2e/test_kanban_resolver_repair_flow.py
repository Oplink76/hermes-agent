"""End-to-end acceptance coverage for the bounded Hermes Resolver tier."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from tools import kanban_tools as kt


@pytest.fixture
def resolver_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()
    return home


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "resolver-fixture@example.com")
    _git(repo, "config", "user.name", "Resolver Fixture")
    (repo / "README.md").write_text("resolver fixture\n", encoding="utf-8")


def _expected(conn, task_id: str) -> dict:
    task = kb.get_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    preflight = [
        event for event in kb.list_events(conn, task_id)
        if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT
    ][-1]
    return {
        "run_id": task.current_run_id,
        "preflight_event_id": preflight.id,
        "status": task.status,
        "phase": task.current_step_key,
        "assignee": task.assignee,
        "project_id": task.project_id,
        "workflow_template_id": task.workflow_template_id,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "running": task.running,
        "blocked": task.blocked,
    }


def _resolver_args(task_id: str, expected: dict, **overrides) -> dict:
    request = {
        "task_id": task_id,
        "decision": "resume",
        "fault_domain": "task_state",
        "diagnosis": "The task-local workflow state is recoverable",
        "reason": "Return the card to the governed product flow",
        "expected": expected,
    }
    request.update(overrides)
    return request


def _resolver_state(conn, task_id: str) -> dict:
    return {
        "task": tuple(
            conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        ),
        "runs": [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        ],
        "events": [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        ],
        "links": [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_links WHERE parent_id=? OR child_id=? "
                "ORDER BY parent_id, child_id",
                (task_id, task_id),
            ).fetchall()
        ],
    }


def _route_to_resolver(conn, task_id: str, board: str) -> int:
    ordinary = kb.claim_task(conn, task_id, board=board)
    assert ordinary is not None and ordinary.current_run_id is not None
    assert kb.block_task(
        conn,
        task_id,
        reason="The recorded task state needs diagnosis",
        kind="needs_input",
        attempted_resolutions=["Inspected the task, run, and event history"],
        expected_run_id=ordinary.current_run_id,
        board=board,
        human_escalation_assignee="resolver",
    )
    resolver = kb.claim_task(conn, task_id, board=board)
    assert resolver is not None and resolver.current_run_id is not None
    return resolver.current_run_id


def test_framework_classifier_defect_only_escalates(
    resolver_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = "resolver-framework-incident"
    kb.ensure_product_board_defaults(board, name="Resolver Framework Incident")

    with kb.connect(board=board) as conn:
        dependency_id = kb.create_task(conn, title="Verified dependency", board=board)
        assert kb.complete_task(conn, dependency_id, summary="Dependency satisfied")
        task_id = kb.create_task(
            conn,
            title="Legacy release card with classifier defect",
            assignee="productowner",
            parents=[dependency_id],
            workflow_template_id="product",
            current_step_key="release_measure",
            board=board,
        )
        run_id = _route_to_resolver(conn, task_id, board)
        expected = _expected(conn, task_id)
        before_failed_repair = _resolver_state(conn, task_id)

    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", "resolver")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "resolver-e2e")

    rejected = json.loads(kt._handle_resolve(_resolver_args(
        task_id,
        expected,
        decision="repair",
        fault_domain="framework",
        repair={"workflow": {"phase": "development"}},
    )))
    assert "framework faults must escalate" in rejected["error"]
    with kb.connect(board=board) as conn:
        assert _resolver_state(conn, task_id) == before_failed_repair

    escalated = json.loads(kt._handle_resolve(_resolver_args(
        task_id,
        expected,
        decision="escalate",
        fault_domain="framework",
        diagnosis="The active release classifier is a framework defect",
        reason="Ole must repair the framework before this card can continue",
    )))
    assert escalated["ok"] is True

    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        links = kb.parent_ids(conn, task_id)
    assert task is not None
    assert task.status == "blocked" and task.blocked is True
    assert task.assignee == "default"
    assert task.current_step_key == "release_measure"
    assert task.workflow_template_id == "product"
    assert links == [dependency_id]
    assert not any(event.kind in {"completed", "released"} for event in events)
    resolved = [
        event for event in events if event.kind == "human_input_preflight_resolved"
    ][-1]
    assert resolved.payload["action"] == "escalate"
    assert resolved.payload["fault_domain"] == "framework"


def test_legacy_project_card_repairs_then_uses_normal_evidence_gates(
    resolver_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "product"
    _init_repo(repo)
    board = "resolver-legacy-incident"
    kb.ensure_product_board_defaults(
        board,
        name="Resolver Legacy Incident",
        default_workdir=str(repo),
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fixture: initialize product")

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Resolver Legacy Product",
            folders=[str(repo)],
            primary_path=str(repo),
            board_slug=board,
        )

    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Legacy card with recoverable task-local state",
            assignee="developer",
            project_id=project_id,
            workflow_template_id="product",
            current_step_key="development",
            board=board,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        workspace = kb.resolve_workspace(task, board=board)
        kb.set_workspace_path(conn, task_id, workspace)
        (workspace / "feature.py").write_text("value = 1\n", encoding="utf-8")
        _git(workspace, "add", "feature.py")
        _git(workspace, "commit", "-m", "feat: preserve recovered work")
        adopted_sha = _git(workspace, "rev-parse", "HEAD")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "handoff",
                {
                    "from_step": "development",
                    "to_step": "test",
                    "sha": adopted_sha,
                    "assignee": "tester",
                    "summary": "Audited Development handoff from the lost run",
                },
            )
        resolver_run_id = _route_to_resolver(conn, task_id, board)
        expected = _expected(conn, task_id)
        runs_before = [run.id for run in kb.list_runs(conn, task_id, include_active=True)]

    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(resolver_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "resolver")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "resolver-e2e")
    repaired = json.loads(kt._handle_resolve(_resolver_args(
        task_id,
        expected,
        decision="repair",
        repair={
            "workflow": {
                "phase": "development",
                "assignee": "developer",
                "project_id": project_id,
            },
            "adopt_handoff_sha": adopted_sha,
        },
    )))
    assert repaired["ok"] is True

    with kb.connect(board=board) as conn:
        repaired_task = kb.get_task(conn, task_id)
        resolver_run = kb.get_run(conn, resolver_run_id)
        resolver_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind in {"resolver_repair_applied", "needs_ole"}
        ]
        assert repaired_task is not None
        assert repaired_task.status == "ready"
        assert repaired_task.current_step_key == "development"
        assert repaired_task.assignee == "developer"
        assert repaired_task.project_id == project_id
        assert repaired_task.workspace_path == str(workspace)
        assert repaired_task.branch_name
        assert not repaired_task.running and not repaired_task.blocked
        assert resolver_run is not None
        assert resolver_run.outcome == "preflight_repaired"
        assert "ai_provenance" not in resolver_run.metadata
        assert "workflow_outcome" not in resolver_run.metadata
        assert [run.id for run in kb.list_runs(conn, task_id, include_active=True)] == runs_before
        assert {event.kind for event in resolver_events} == {
            "resolver_repair_applied", "needs_ole",
        }

        monkeypatch.setenv("HERMES_PROFILE", "developer")
        development = kb.claim_task(conn, task_id, board=board)
        assert development is not None and development.current_run_id is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Adopt the already committed Development handoff",
            metadata={
                "ai_provenance": {"writer": {"agent": "claude-code"}}
            },
            expected_run_id=development.current_run_id,
            board=board,
        )
        assert _git(workspace, "status", "--porcelain") == ""

        tester = kb.claim_task(conn, task_id, board=board)
        assert tester is not None and tester.current_run_id is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Recovered implementation tests passed",
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {
                    "tester": {"agent": "hermes", "result": "passed"}
                },
            },
            expected_run_id=tester.current_run_id,
            board=board,
        )

        reviewer = kb.claim_review_task(conn, task_id, claimer="independent-codex")
        assert reviewer is not None and reviewer.current_run_id is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Independent review approved the recovered handoff",
            metadata={
                "workflow_outcome": {"verdict": "approved"},
                "ai_provenance": {
                    "writer": {"agent": "claude-code"},
                    "reviewer": {
                        "agent": "codex",
                        "verdict": "approved",
                        "reviewed_branch": repaired_task.branch_name,
                        "reviewed_commit": adopted_sha,
                    },
                },
            },
            expected_run_id=reviewer.current_run_id,
            board=board,
        )
        final_task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id, include_active=True)
        events = kb.list_events(conn, task_id)

    assert final_task is not None
    assert final_task.current_step_key == "release_measure"
    assert final_task.status != "done"
    development_run = next(run for run in runs if run.id == development.current_run_id)
    test_run = next(run for run in runs if run.id == tester.current_run_id)
    review_run = next(run for run in runs if run.id == reviewer.current_run_id)
    assert development_run.metadata["ai_provenance"]["writer"]["agent"] == "claude-code"
    assert test_run.metadata["workflow_outcome"] == {"verdict": "passed"}
    assert review_run.metadata["workflow_outcome"] == {"verdict": "approved"}
    handoffs = [event for event in events if event.kind == "handoff"]
    assert handoffs[-3].payload["sha"] == adopted_sha
    assert handoffs[-2].payload["from_step"] == "test"
    assert handoffs[-1].payload["from_step"] == "review"
