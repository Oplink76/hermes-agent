"""End-to-end acceptance coverage for the bounded Hermes Resolver tier."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from tools import kanban_tools as kt
from tests.e2e.test_kanban_epic_integration_release import (
    fake_git, governed_profile, product_fixture,  # noqa: F401 — shared pytest fixtures
)


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


def test_recovered_story_review_and_integration(
    product_fixture, monkeypatch,
):
    from tests.e2e.test_kanban_epic_integration_release import (
        _claim_and_complete, _create_epic, _create_epic_member,
    )

    product = product_fixture
    board = product.board

    def identity(task):
        # Review deliberately uses the same provider as Resolver, not Developer.
        provider = "claude-cli" if task.assignee in {"reviewer", "resolver"} else "codex"
        return {"profile": task.assignee, "provider": provider, "model": "fixture",
                "effort": "high", "surface": "hermes-primary", "source": "dispatcher", "version": 1}

    monkeypatch.setattr(kb, "_resolve_worker_runtime_identity", identity)
    with kb.connect(board=board) as conn:
        epic = _create_epic(conn, "Epic: recovered story proof")
        tid, workspace, branch = _create_epic_member(conn, product, board, epic)
        _claim_and_complete(conn, tid, "po", board, summary="Accepted")
        _claim_and_complete(conn, tid, "architect", board, summary="Designed")
        resolver_id = _route_to_resolver(conn, tid, board)
        resolver = kb.get_task(conn, tid)
        kb._stamp_run_executor_identity(conn, resolver)
        expected = kb.resolver_expected_snapshot(conn, tid)
        assert kb.resolve_product_preflight(
            conn, tid, board=board, resolver_profile="resolver", resolver_model="fixture",
            request={"decision": "repair", "fault_domain": "task_state",
                     "diagnosis": "Temporary recovery fixture is understood",
                     "reason": "Resume the existing Development phase", "expected": expected,
                     "repair": {"workflow": {"phase": "development", "assignee": "developer"}}},
        )
        recovery_before = kb.get_run(conn, resolver_id)

        developer = kb.claim_task(conn, tid, board=board)
        assert developer and developer.current_run_id
        kb._stamp_run_executor_identity(conn, developer)
        (workspace / "story.txt").write_text("recovered delivery\n", encoding="utf-8")
        assert kb.complete_task(
            conn, tid, board=board, expected_run_id=developer.current_run_id,
            summary="Implementation complete", metadata={"ai_provenance": {"writer": {"agent": "codex"}}},
        )
        head = _git(workspace, "rev-parse", "HEAD")
        tester = kb.claim_task(conn, tid, board=board)
        assert tester and tester.current_run_id
        kb._stamp_run_executor_identity(conn, tester)
        test_pin = kb._prepare_test_target(conn, tid, workspace, board=board)
        # Actually run the fixture's test command, not merely report a pass.
        verified = subprocess.run(["bash", "tests/e2e_scripts/run_tests.sh"], cwd=workspace,
                                  capture_output=True, text=True, timeout=60)
        assert verified.returncode == 0, verified.stderr
        assert kb.complete_task(
            conn, tid, board=board, expected_run_id=tester.current_run_id,
            summary="Fixture tests passed", metadata={"workflow_outcome": {"verdict": "passed"}, **test_pin},
        )
        reviewer = kb.claim_review_task(conn, tid, claimer="independent-reviewer")
        assert reviewer and reviewer.current_run_id
        kb._stamp_run_executor_identity(conn, reviewer)
        review_pin = kb._prepare_review_target(conn, tid, workspace, board=board)
        assert review_pin["review_head_sha"] == test_pin["test_head_sha"] == head
        assert kb.complete_task(
            conn, tid, board=board, expected_run_id=reviewer.current_run_id,
            summary="Independent review approved", metadata={"workflow_outcome": {"verdict": "approved"}, **review_pin},
        )
        records = kb._terminal_run_records(conn, tid)
        approved = kb.latest_review_authority(records)
        assert approved.writer_provider == "codex"
        assert approved.reviewer_provider == "claude-cli"
        assert resolver_id not in {record.run_id for record in records}
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert tid in result.integrated
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_run(conn, resolver_id) == recovery_before
        assert _git(product.repo, "show", f"{kb.epic_branch_for(epic)}:story.txt") == "recovered delivery"
        assert product.fake_git.push_invocations == []


@pytest.mark.parametrize("recovery_case", ["development", "test", "changed_candidate", "failed_test", "missing_pin"])
def test_failed_custom_recovery_journey(
    product_fixture, monkeypatch, recovery_case,
):
    """Recover through the public DB API, then real Test/Review/integration."""
    from tests.e2e.test_kanban_epic_integration_release import (
        _claim_and_complete, _create_epic, _create_epic_member,
    )

    product = product_fixture
    board = product.board
    review_provider = "codex"

    def identity(task):
        provider = (review_provider if task.assignee == "reviewer" else
                    "claude-cli" if task.assignee == "custom-recovery" else "codex")
        return {"profile": task.assignee, "provider": provider, "model": "fixture",
                "effort": "high", "surface": "hermes-primary", "source": "dispatcher", "version": 1}

    monkeypatch.setattr(kb, "_resolve_worker_runtime_identity", identity)
    with kb.connect(board=board) as conn:
        epic = _create_epic(conn, "Epic: custom recovery authority")
        tid, workspace, branch = _create_epic_member(conn, product, board, epic)
        _claim_and_complete(conn, tid, "po", board, summary="Accepted")
        _claim_and_complete(conn, tid, "architect", board, summary="Designed")
        developer = kb.claim_task(conn, tid, board=board)
        kb._stamp_run_executor_identity(conn, developer)
        (workspace / "story.txt").write_text("recovered delivery\n", encoding="utf-8")
        assert kb.complete_task(conn, tid, board=board, expected_run_id=developer.current_run_id,
                                summary="Implementation complete", metadata={"ai_provenance": {"writer": {"agent": "codex"}}})
        developer_before = kb.get_run(conn, developer.current_run_id)

        def pass_test():
            tester = kb.claim_task(conn, tid, board=board)
            assert tester and tester.current_run_id
            kb._stamp_run_executor_identity(conn, tester)
            pins = kb._prepare_test_target(conn, tid, workspace, board=board)
            verified = subprocess.run(["bash", "tests/e2e_scripts/run_tests.sh"], cwd=workspace,
                                      capture_output=True, text=True, timeout=60)
            assert verified.returncode == 0, verified.stderr
            assert kb.complete_task(conn, tid, board=board, expected_run_id=tester.current_run_id,
                                    summary="Fixture tests passed", metadata={"workflow_outcome": {"verdict": "passed"}, **pins})
            return tester.current_run_id, pins

        test_id, pins = (pass_test()
                        if recovery_case != "development" else (None, {}))
        if recovery_case == "missing_pin":
            # Reconstruct legacy evidence missing pins. Modern completion
            # correctly refuses to create this historical shape.
            legacy = dict(kb.get_run(conn, test_id).metadata)
            legacy.pop("test_branch")
            legacy.pop("test_head_sha")
            conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(legacy), test_id))
            conn.commit()
            pins = {}
        # Reproduce a persisted recovery phase via the existing phase API;
        # do not insert an ordinary failed Test just to route the happy path.
        phase = "development" if recovery_case == "development" else "test"
        assert kb.set_phase(conn, tid, phase, board=board)
        kb.assign_task(conn, tid, "developer" if phase == "development" else "tester")
        assert kb.block_task(conn, tid, board=board, kind="needs_input", reason="Recovery diagnosis needed",
                             human_escalation_assignee="custom-recovery")
        failed = kb.claim_task(conn, tid, board=board)
        kb._stamp_run_executor_identity(conn, failed)
        kb._set_worker_pid(conn, tid, 987654321)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda pid: ("nonzero_exit", 1))
        assert kb.detect_crashed_workers(conn) == [tid]
        failed_before = kb.get_run(conn, failed.current_run_id)
        assert failed_before.outcome == "crashed"
        assert kb._latest_product_step_executor(conn, tid, "development")["provider"] == "codex"
        assert kb._latest_test_target(conn, tid) == (pins if test_id is not None else None)

        recovery = kb.claim_task(conn, tid, board=board)
        kb._stamp_run_executor_identity(conn, recovery)
        # Custom profile support is this DB API; the literal-resolver tool
        # permission boundary remains covered by the existing tool cases.
        assert kb.resolve_product_preflight(
            conn, tid, board=board, resolver_profile="custom-recovery", resolver_model="fixture",
            request={"decision": "repair", "fault_domain": "task_state", "diagnosis": "Existing candidate is recoverable",
                     "reason": "Resume ordinary evidence gates", "expected": kb.resolver_expected_snapshot(conn, tid),
                     "repair": {"workflow": {"phase": "test", "assignee": "tester"}}},
        )
        recovery_before = kb.get_run(conn, recovery.current_run_id)
        if recovery_case == "changed_candidate":
            (workspace / "extra.txt").write_text("candidate changed\n", encoding="utf-8")
            _git(workspace, "add", "extra.txt")
            _git(workspace, "commit", "-m", "fixture: change recovered candidate")
        elif recovery_case == "failed_test":
            failed_test = kb.claim_task(conn, tid, board=board)
            kb._stamp_run_executor_identity(conn, failed_test)
            kb._record_spawn_failure(conn, tid, "ordinary test failed", failure_limit=5)

        if recovery_case == "development":
            test_id, pins = pass_test()
        else:
            assert kb.set_phase(conn, tid, "review", board=board)
            kb.assign_task(conn, tid, "reviewer")
            if recovery_case != "test":
                rejected = kb.claim_review_task(conn, tid)
                kb._stamp_run_executor_identity(conn, rejected)
                with pytest.raises(kb.ReviewTargetPreparationError, match="Test pin|tested SHA"):
                    kb._prepare_review_target(conn, tid, workspace, board=board)
                kb._record_spawn_failure(conn, tid, "fresh Test required", failure_limit=5)
                assert kb.set_phase(conn, tid, "test", board=board)
                kb.assign_task(conn, tid, "tester")
                test_id, pins = pass_test()

        reviewer = kb.claim_review_task(conn, tid)
        assert reviewer and reviewer.current_run_id
        kb._stamp_run_executor_identity(conn, reviewer)
        # Same actual Developer provider is forbidden even though it differs
        # from Resolver. A failed review attempt does not grant approval.
        rejected_pin = kb._prepare_review_target(conn, tid, workspace, board=board)
        with pytest.raises(ValueError, match="independen|different|same"):
            kb.complete_task(conn, tid, board=board, expected_run_id=reviewer.current_run_id,
                             summary="Writer reviewing own work", metadata={"workflow_outcome": {"verdict": "approved"}, **rejected_pin})
        kb._record_spawn_failure(conn, tid, "reviewer must be independent", failure_limit=5)
        review_provider = "claude-cli"
        reviewer = kb.claim_review_task(conn, tid)
        kb._stamp_run_executor_identity(conn, reviewer)
        review_pin = kb._prepare_review_target(conn, tid, workspace, board=board)
        assert review_pin["review_head_sha"] == pins["test_head_sha"]
        assert kb.latest_test_authority(kb._terminal_run_records(conn, tid), pins["test_head_sha"]).run_id == test_id
        assert kb.complete_task(conn, tid, board=board, expected_run_id=reviewer.current_run_id,
                                summary="Independent review approved", metadata={"workflow_outcome": {"verdict": "approved"}, **review_pin})
        records = kb._terminal_run_records(conn, tid)
        approved = kb.latest_review_authority(records)
        assert approved.writer_provider == "codex" and approved.reviewer_provider == "claude-cli"
        assert not {failed.current_run_id, recovery.current_run_id} & {r.run_id for r in records}
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert tid in result.integrated
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_run(conn, developer.current_run_id) == developer_before
        assert kb.get_run(conn, failed.current_run_id) == failed_before
        assert kb.get_run(conn, recovery.current_run_id) == recovery_before
        assert _git(product.repo, "show", f"{kb.epic_branch_for(epic)}:story.txt") == "recovered delivery"
        assert product.fake_git.push_invocations == []
