"""Kanban DB workflows tests, isolated to keep per-file CI work bounded."""

from __future__ import annotations
import json
import os
import subprocess
import time
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
import hermes_cli.kanban_db_connect as kanban_db_connect
import hermes_cli.kanban_db_workspace as kanban_db_workspace
import shutil as shutil
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw

from tests.hermes_cli.test_kanban_db import kanban_home as kanban_home
from tests.hermes_cli.test_kanban_db import (
    _card_snapshot,
    _commit_file,
    _exited_status,
    _full_tables_state,
    _head_sha,
    _init_git_repo,
    _resolve_preflight,
    _resolver_expected,
    _resolver_project_fixture,
    _resolver_request,
    _resolver_state,
    _route_existing_task_to_resolver,
    _route_project_task_with_audited_handoff,
    _route_task_to_resolver,
    _seed_product_test_worktree,
    _seed_stale_terminal_card,
    _seed_v2_card,
    _v2_product_board,
    _v2_product_board_with_repo,
)


def test_clear_terminal_state_clears_only_the_stale_generic_terminal_flag(kanban_home):
    board = "clear-terminal-success"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)

    with kanban_db_connect.connect(board=board) as conn:
        before = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        before_runs = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ]
        before_events = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ]

        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=completed_at,
            expected_phase="development",
            expected_latest_event_id=event_id,
            actor="operator",
            reason="clear stale generic terminal state",
        )
        assert kb.clear_terminal_state(conn, request) is True

        after = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        assert after["status"] == "ready"
        assert after["completed_at"] is None
        for field, value in before.items():
            if field not in {"status", "completed_at"}:
                assert after[field] == value, field
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ] == before_runs

        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        assert [tuple(row) for row in events[:-1]] == before_events
        assert events[-1]["kind"] == "terminal_state_cleared"
        payload = json.loads(events[-1]["payload"])
        assert payload == {
            "operation": "clear_terminal_state",
            "actor": "operator",
            "reason": "clear stale generic terminal state",
            "expected": {
                "status": "done",
                "completed_at": completed_at,
                "phase": "development",
                "latest_event_id": event_id,
            },
        }


@pytest.mark.parametrize("field", ["expected_completed_at", "expected_latest_event_id"])
def test_clear_terminal_state_refuses_stale_cas_fields(kanban_home, field):
    board = f"clear-terminal-stale-{field}"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)
    with kanban_db_connect.connect(board=board) as conn:
        before = {
            "task": tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
            "events": [
                tuple(row)
                for row in conn.execute(
                    "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
                ).fetchall()
            ],
        }
        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=(completed_at + 1 if field == "expected_completed_at" else completed_at),
            expected_phase="development",
            expected_latest_event_id=(event_id + 1 if field == "expected_latest_event_id" else event_id),
            actor="operator",
            reason="stale CAS must refuse",
        )
        assert kb.clear_terminal_state(conn, request) is False
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()) == before["task"]
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ] == before["events"]


def test_clear_terminal_state_refuses_non_done_status(kanban_home):
    board = "clear-terminal-non-done"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)
    with kanban_db_connect.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        before = {
            "task": tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
            "events": len(kb.list_events(conn, task_id)),
        }
        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=completed_at,
            expected_phase="development",
            expected_latest_event_id=event_id,
            actor="operator",
            reason="non-done must refuse",
        )
        assert kb.clear_terminal_state(conn, request) is False
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()) == before["task"]
        assert len(kb.list_events(conn, task_id)) == before["events"]


def test_clear_terminal_state_refuses_already_terminal_phase(kanban_home):
    board = "clear-terminal-terminal-phase"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board, phase="done")
    with kanban_db_connect.connect(board=board) as conn:
        before = {
            "task": tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
            "events": len(kb.list_events(conn, task_id)),
        }
        request = kb.ClearTerminalStateRequest(
            task_id=task_id,
            expected_completed_at=completed_at,
            expected_phase="done",
            expected_latest_event_id=event_id,
            actor="operator",
            reason="terminal phase must refuse",
        )
        assert kb.clear_terminal_state(conn, request) is False
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()) == before["task"]
        assert len(kb.list_events(conn, task_id)) == before["events"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("actor", ""), ("actor", "  "), ("reason", ""), ("reason", "  ")],
)
def test_clear_terminal_state_refuses_empty_actor_or_reason(kanban_home, field, value):
    board = f"clear-terminal-empty-{field}-{len(value)}"
    _v2_product_board(board)
    task_id, completed_at, event_id = _seed_stale_terminal_card(board)
    values = {
        "task_id": task_id,
        "expected_completed_at": completed_at,
        "expected_phase": "development",
        "expected_latest_event_id": event_id,
        "actor": "operator",
        "reason": "required reason",
    }
    values[field] = value
    with kanban_db_connect.connect(board=board) as conn:
        with pytest.raises(ValueError, match=field):
            kb.clear_terminal_state(conn, kb.ClearTerminalStateRequest(**values))


@pytest.mark.parametrize("step", sorted(kb.PRODUCT_WORKFLOW_STEP_SET))
def test_create_task_accepts_each_product_step(kanban_home, step):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Story: valid state",
            workflow_template_id="product",
            current_step_key=step,
        )
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == step


def test_create_task_infers_missing_product_step_from_explicit_intent(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Implementation work",
            assignee="developer",
            workflow_template_id="product",
        )
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == "development"


def test_create_task_allows_custom_workflow_step(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Custom flow",
            workflow_template_id="custom",
            current_step_key="bespoke-review",
        )
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == "bespoke-review"


def test_create_task_keeps_legacy_step_without_product_template(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Legacy flow", current_step_key="in_progress")
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.workflow_template_id is None
    assert task.current_step_key == "in_progress"


def test_create_task_rejects_unknown_project(kanban_home):
    with kanban_db_connect.connect() as conn:
        with pytest.raises(ValueError, match="unknown project"):
            kb.create_task(conn, title="Lost governance", project_id="missing-project")


def test_decomposed_child_preserves_complete_execution_context(kanban_home):
    with kanban_db_connect.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="Context root",
            body="root body",
            assignee="orchestrator",
            workspace_kind="worktree",
            workspace_path="/repo/.worktrees/root",
            branch_name="project/root",
            tenant="tenant-a",
            max_runtime_seconds=120,
            skills=["skill-a"],
            max_retries=2,
            model_override="model-a",
            provider_override="provider-a",
            reasoning_effort="high",
            goal_mode=True,
            goal_max_turns=7,
            workflow_template_id="custom",
            current_step_key="implementation",
            source_commit_required=True,
            source_commit_forbidden=False,
            triage=True,
        )
        conn.execute(
            "UPDATE tasks SET project_id = ?, work_contract_id = ? WHERE id = ?",
            ("project-a", "contract-a", root_id),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "Generated child",
                    "body": "child body",
                    "assignee": "developer",
                },
                {
                    "title": "Overridden workspace",
                    "assignee": "tester",
                    "workspace_kind": "dir",
                    "workspace_path": "/tmp/generated-child",
                },
            ],
        )

        child = kb.get_task(conn, child_ids[0])
        overridden = kb.get_task(conn, child_ids[1])

    assert child is not None
    assert child.title == "Generated child"
    assert child.body == "child body"
    assert child.assignee == "developer"
    assert child.project_id == "project-a"
    assert child.branch_name is None
    assert child.tenant == "tenant-a"
    assert child.workspace_kind == "worktree"
    assert child.workspace_path is None
    assert child.max_runtime_seconds == 120
    assert child.skills == ["skill-a"]
    assert child.max_retries == 2
    assert child.model_override == "model-a"
    assert child.provider_override == "provider-a"
    assert child.reasoning_effort == "high"
    assert child.goal_mode is True
    assert child.goal_max_turns == 7
    assert child.workflow_template_id == "custom"
    assert child.current_step_key == "implementation"
    # Contracts are one-to-one with cards; reusing the root contract would
    # violate idx_tasks_work_contract_unique.
    assert child.work_contract_id is None
    assert child.work_item_kind == "card"
    assert child.source_commit_required is True
    assert child.source_commit_forbidden is False
    assert overridden is not None
    assert overridden.assignee == "tester"
    assert overridden.workspace_kind == "dir"
    assert overridden.workspace_path == "/tmp/generated-child"


@pytest.mark.parametrize(
    ("step", "verdict", "target"),
    [
        ("test", "changes_requested", "development"),
        ("review", "changes_requested", "development"),
        ("review", "architecture_invalid", "architecture"),
    ],
)
def test_product_rejection_routes_backward(
    kanban_home, step, verdict, target
):
    board = f"rework-{step}-{target}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: rework",
            assignee="tester" if step == "test" else "reviewer",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="Needs revision",
            metadata={
                "workflow_outcome": {
                    "verdict": verdict,
                    "target_step": target,
                    "findings": ["Concrete finding"],
                }
            },
            expected_run_id=claimed.current_run_id,
            board=board,
            product_role_assignees={
                "developer": "custom-developer",
                "architect": "custom-architect",
            },
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.current_step_key == target
    assert task.rework_count == 1
    assert task.assignee == (
        "custom-developer" if target == "development" else "custom-architect"
    )
    assert any(event.kind == "rework_requested" for event in events)


def test_rework_directive_is_append_only_with_one_active_row(kanban_home):
    board = "rework-directive-append-only"
    _v2_product_board(board)
    rejected_sha = "a" * 40
    replacement_sha = "b" * 40
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: durable directive",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        first = kb.create_rework_directive(
            conn,
            tid,
            origin_kind="test",
            origin_run_id=11,
            origin_intent_key="intent-1",
            origin_phase="test",
            target_phase="development",
            rejected_branch="story/durable-directive",
            rejected_sha=rejected_sha,
            epic_tip_sha="c" * 40,
            findings=["first finding"],
        )
        assert first.status == "active"
        assert kb.active_rework_directive(conn, tid) == first

        second = kb.create_rework_directive(
            conn,
            tid,
            origin_kind="review",
            origin_run_id=12,
            origin_intent_key="intent-2",
            origin_phase="review",
            target_phase="development",
            rejected_branch="story/durable-directive",
            rejected_sha=replacement_sha,
            epic_tip_sha="d" * 40,
            findings=["replacement finding"],
        )
        rows = conn.execute(
            "SELECT status FROM product_rework_directives "
            "WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()

    assert second.status == "active"
    assert [row["status"] for row in rows] == ["superseded", "active"]


def test_rework_directive_routes_with_exact_sha_and_precedes_attempts(
    kanban_home,
):
    board = "rework-directive-context"
    _v2_product_board(board)
    rejected_sha = "e" * 40
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: visible rework",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid, board=board, claimer="tester")
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="Test rejected the candidate",
            metadata={
                "workflow_outcome": {
                    "verdict": "changes_requested",
                    "target_step": "development",
                    "findings": ["the candidate is incomplete"],
                },
                "rejected_branch": "story/visible-rework",
                "rejected_sha": rejected_sha,
                "epic_tip_sha": "f" * 40,
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        directive = kb.active_rework_directive(conn, tid)
        context = kb.build_worker_context(conn, tid)

    assert directive is not None
    assert directive.origin_phase == "test"
    assert directive.target_phase == "development"
    assert directive.rejected_branch == "story/visible-rework"
    assert directive.rejected_sha == rejected_sha
    assert directive.findings == ("the candidate is incomplete",)
    assert "## Required rework directive" in context
    assert rejected_sha in context
    assert context.index("## Required rework directive") < context.index(
        "## Prior attempts on this task"
    )


def test_rework_directive_resolves_only_after_new_development_sha(
    kanban_home, tmp_path
):
    board = "rework-directive-resolution"
    _v2_product_board(board)
    repo = tmp_path / "directive-repo"
    _init_git_repo(repo)
    rejected_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: resolve rework",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="main",
            board=board,
        )
        kb.create_rework_directive(
            conn,
            tid,
            origin_kind="test",
            origin_run_id=21,
            origin_phase="test",
            target_phase="development",
            rejected_branch="story/resolve-rework",
            rejected_sha=rejected_sha,
            findings=["fix the implementation"],
        )
        assert not kb.resolve_rework_directive(
            conn,
            tid,
            new_sha=rejected_sha,
            resolved_by_run_id=22,
        )
        assert kb.active_rework_directive(conn, tid) is not None

        claimed = kb.claim_task(conn, tid, board=board, claimer="developer")
        assert claimed is not None and claimed.current_run_id is not None
        (repo / "fixed.txt").write_text("fixed\n", encoding="utf-8")
        assert kb.handoff(
            conn,
            tid,
            board=board,
            summary="Implemented the requested fix",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
            expected_run_id=claimed.current_run_id,
            expected_phase="development",
        )
        resolved = conn.execute(
            "SELECT status, resolved_by_run_id FROM product_rework_directives "
            "WHERE task_id = ?",
            (tid,),
        ).fetchone()

    assert resolved["status"] == "resolved"
    assert resolved["resolved_by_run_id"] == claimed.current_run_id


@pytest.mark.parametrize(
    "findings",
    [[], "not-a-list", [""], [1], ["Concrete", ""]],
)
def test_product_rework_requires_nonempty_string_findings(kanban_home, findings):
    board = "rework-findings"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: rework",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        with pytest.raises(kb.ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                tid,
                summary="Needs revision",
                metadata={
                    "workflow_outcome": {
                        "verdict": "changes_requested",
                        "target_step": "development",
                        "findings": findings,
                    }
                },
                expected_run_id=claimed.current_run_id,
                board=board,
            )
        assert raised.value.code == "invalid_findings"
        task = kb.get_task(conn, tid)
    assert task is not None and task.current_step_key == "test"


def test_product_rework_requires_expected_run_id(kanban_home):
    board = "rework-run-required"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: rework",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        with pytest.raises(ValueError, match="expected_run_id"):
            kb.complete_task(
                conn,
                tid,
                summary="Needs revision",
                metadata={
                    "workflow_outcome": {
                        "verdict": "changes_requested",
                        "target_step": "development",
                        "findings": ["Concrete finding"],
                    }
                },
                board=board,
            )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "running"
    assert task.current_step_key == "test"
    assert task.rework_count == 0
    assert task.current_run_id == claimed.current_run_id


@pytest.mark.parametrize(
    ("step", "verdict", "next_step", "provenance"),
    [
        (
            "test",
            "passed",
            "review",
            {"tester": {"agent": "hermes", "result": "passed"}},
        ),
        (
            "review",
            "approved",
            "release_measure",
            {
                "writer": {"agent": "claude-code"},
                "reviewer": {"agent": "codex"},
            },
        ),
    ],
)
def test_product_positive_rework_outcome_uses_forward_handoff(
    kanban_home, step, verdict, next_step, provenance
):
    board = f"rework-positive-{step}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: accepted",
            assignee="tester" if step == "test" else "reviewer",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(
            conn,
            tid,
            summary="Accepted",
            metadata={
                "workflow_outcome": {"verdict": verdict},
                "ai_provenance": provenance,
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.current_step_key == next_step
    # handoff_v2 reads board role policy; the important contract is that a
    # positive outcome advances normally rather than incrementing rework.
    assert task.rework_count == 0
    assert any(event.kind == "handoff" for event in events)
    assert not any(event.kind == "rework_requested" for event in events)


@pytest.mark.parametrize(
    ("step", "verdict", "provenance"),
    [
        (
            "test",
            "approved",
            {"tester": {"agent": "hermes", "result": "passed"}},
        ),
        (
            "review",
            "passed",
            {
                "writer": {"agent": "claude-code"},
                "reviewer": {"agent": "codex"},
            },
        ),
    ],
)
def test_product_positive_rework_verdict_must_match_phase(
    kanban_home, step, verdict, provenance
):
    board = f"rework-positive-invalid-{step}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: invalid verdict",
            assignee="tester" if step == "test" else "reviewer",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        with pytest.raises(kb.ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                tid,
                summary="Wrong verdict",
                metadata={
                    "workflow_outcome": {"verdict": verdict},
                    "ai_provenance": provenance,
                },
                expected_run_id=claimed.current_run_id,
                board=board,
            )
        assert raised.value.code == "phase_mismatch"
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == step
    assert task.status == "running"


def test_product_positive_rework_rejects_same_run_phase_change_before_handoff(
    kanban_home, monkeypatch
):
    board = "rework-positive-phase-cas"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: phase-bound verdict",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None

        original_route = kb._route_product_rework_if_requested

        def route_then_change_phase(*args, **kwargs):
            routed = original_route(*args, **kwargs)
            assert routed is None
            assert kb.set_phase(conn, tid, "review", board=board)
            return routed

        monkeypatch.setattr(
            kb, "_route_product_rework_if_requested", route_then_change_phase
        )
        completed = kb.complete_task(
            conn,
            tid,
            summary="Test verdict must stay bound to Test",
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {
                    "tester": {"agent": "hermes", "result": "passed"},
                    "writer": {"agent": "claude-code"},
                    "reviewer": {"agent": "codex"},
                },
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert completed is False
    assert task is not None
    assert task.current_step_key == "review"
    assert task.status == "running"
    assert task.running is True
    assert task.current_run_id == claimed.current_run_id
    assert not any(event.kind == "handoff" for event in events)


def test_invalid_product_rework_does_not_commit_workflow_repair(kanban_home):
    board = "rework-invalid-no-repair"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: legacy tester card",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            board=board,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        conn.execute(
            "UPDATE tasks SET workflow_template_id=NULL, current_step_key=NULL "
            "WHERE id=?",
            (tid,),
        )
        conn.commit()

        with pytest.raises(ValueError, match="invalid workflow_outcome"):
            kb.complete_task(
                conn,
                tid,
                summary="Invalid route",
                metadata={
                    "workflow_outcome": {
                        "verdict": "architecture_invalid",
                        "target_step": "architecture",
                        "findings": ["Not valid from Test"],
                    }
                },
                expected_run_id=claimed.current_run_id,
                board=board,
            )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.workflow_template_id is None
    assert task.current_step_key is None
    assert not any(event.kind == "workflow_repaired" for event in events)


def test_fourth_product_rejection_routes_to_human_block(kanban_home):
    board = "rework-limit"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story: bounded rework",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            board=board,
        )
        conn.execute("UPDATE tasks SET rework_count=3 WHERE id=?", (tid,))
        conn.commit()
        claimed = kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            summary="Fourth rejection",
            metadata={
                "workflow_outcome": {
                    "verdict": "changes_requested",
                    "target_step": "development",
                    "findings": ["Still unsafe"],
                }
            },
            expected_run_id=claimed.current_run_id,
            board=board,
        )
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.status == "blocked"
    assert task.blocked is True
    assert task.rework_count == 4
    blocked = [event for event in events if event.kind == "blocked"]
    assert blocked
    assert blocked[-1].payload["kind"] == "rework_limit"
    assert blocked[-1].payload["findings"] == ["Still unsafe"]


def test_create_task_refuses_resolver_assignee(kanban_home):
    """A card cannot be authored into the privileged Resolver lane.

    A brand-new card has no preflight, so the Resolver it would spawn
    would hold `resolver_readonly` and no lifecycle exit at all.
    """
    with kanban_db_connect.connect() as conn:
        with pytest.raises(ValueError, match="resolver"):
            kb.create_task(conn, title="Ordinary goal", assignee="resolver")
        assert kb.list_tasks(conn) == []


def test_assign_task_refuses_resolver_without_unresolved_preflight(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Ordinary goal", assignee="developer")
        with pytest.raises(ValueError, match="resolver"):
            kb.assign_task(conn, tid, "resolver")
        task = kb.get_task(conn, tid)
    assert task is not None and task.assignee == "developer"


def test_assign_task_allows_resolver_on_unresolved_preflight(kanban_home):
    """The valid path: a product card already displaced to a preflight."""
    board = "resolver-assign-valid"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _run_id = _route_task_to_resolver(conn, board)
        assert kb.has_unresolved_product_preflight(conn, tid)
        conn.execute(
            "UPDATE tasks SET claim_lock=NULL, status='ready' WHERE id=?", (tid,)
        )
        conn.commit()
        assert kb.assign_task(conn, tid, "resolver")
        task = kb.get_task(conn, tid)
    assert task is not None and task.assignee == "resolver"


def test_dispatch_refuses_resolver_routing_before_creating_a_run(
    kanban_home, all_assignees_spawnable
):
    """The 2026-08-02 deadlock: an ordinary goal card assigned to ``resolver``
    spawned a worker with no lifecycle exit and hung.

    The refusal lives in the claim transaction, so an incompatible card never
    becomes a run at all — no run row, no ``claimed`` event, no subprocess.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Ordinary goal", assignee="developer")
        # Bypass the routing guard the way the incident's cards got there.
        conn.execute("UPDATE tasks SET assignee='resolver' WHERE id=?", (tid,))
        conn.commit()

        dry = kbd.dispatch_once(conn, spawn_fn=fake_spawn, dry_run=True)
        assert tid not in [row[0] for row in dry.spawned]
        assert tid in dry.skipped_nonspawnable

        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)
        ).fetchone()[0]
        kinds = [event.kind for event in kb.list_events(conn, tid)]
        error = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id=?", (tid,)
        ).fetchone()[0]

    assert tid not in spawned_ids
    assert tid not in [row[0] for row in res.spawned]
    assert task is not None and task.status == "blocked"
    assert runs == 0
    assert "claimed" not in kinds
    assert "claim_rejected" in kinds
    assert "resolver" in (error or "") and "preflight" in (error or "")


def test_dispatch_allows_resolver_on_unresolved_preflight(
    kanban_home, all_assignees_spawnable
):
    board = "resolver-dispatch-valid"
    _v2_product_board(board)
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kanban_db_connect.connect(board=board) as conn:
        tid, _run_id = _route_task_to_resolver(conn, board)
        conn.execute(
            "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL, "
            "current_run_id=NULL, status='ready', running=0 WHERE id=?",
            (tid,),
        )
        conn.commit()
        kbd.dispatch_once(conn, spawn_fn=fake_spawn, board=board)

    assert spawned_ids == [tid]


def test_complete_task_refuses_unresolved_preflight_without_mutation(kanban_home):
    board = "resolver-complete-refusal"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="use kanban_resolve"):
            kb.complete_task(
                conn,
                tid,
                summary="Ordinary completion must not resolve preflight",
                expected_run_id=run_id,
                board=board,
            )
        assert _resolver_state(conn, tid) == before


@pytest.mark.parametrize(
    ("step", "metadata", "code"),
    [
        ("test", None, "missing"),
        (
            "review",
            {"workflow_outcome": {"verdict": "approved", "unexpected": True}},
            "invalid_shape",
        ),
    ],
)
def test_complete_task_rejects_unresolved_test_review_without_mutation(
    kanban_home, step, metadata, code
):
    """Ordinary completion validates Test/Review before the Resolver path."""
    board = f"resolver-complete-outcome-{step}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board, step=step)
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                tid,
                summary="Ordinary completion without a canonical outcome",
                metadata=metadata,
                expected_run_id=run_id,
                board=board,
            )
        after = _resolver_state(conn, tid)

    assert raised.value.code == code
    assert after["task"] == before["task"]
    assert after["runs"] == before["runs"]
    assert after["links"] == before["links"]
    assert after["events"][:-1] == before["events"]
    rejection = [
        event for event in after["events"]
        if event[3] == "completion_rejected_outcome"
    ]
    assert len(rejection) == 1
    assert json.loads(rejection[0][4]) == {
        "run_id": run_id,
        "phase": step,
        "code": code,
    }


def test_resolve_product_preflight_resume_uses_complete_snapshot(kanban_home):
    board = "resolver-entry-resume"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(_resolver_expected(conn, tid, run_id))
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        task = kb.get_task(conn, tid)
        run = kb.get_run(conn, run_id)
        events = kb.list_events(conn, tid)
    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "developer"
    assert task.current_run_id is None
    assert run is not None and run.ended_at is not None
    assert events[-1].kind == "human_input_preflight_resolved"


def test_resolve_product_preflight_rejects_legacy_fix_task_shape_without_mutation(kanban_home):
    """The legacy create_fix_task resolver shape is dead: reject, zero mutation.

    Resolver is a task-local repair/preflight resolver only — it cannot
    create or link work. Even a well-formed legacy request (a real fix task
    already linked as a child, exactly the shape the old contract accepted)
    must be rejected, leaving tasks / task_runs / task_events / task_links
    byte-for-byte unchanged.
    """
    board = "resolver-legacy-fix-shape"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        fix_id = kb.create_task(
            conn,
            title="Fix the blocker",
            assignee="developer",
            created_by="resolver",
            parents=[tid],
            board=board,
        )
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="create_fix_task",
            fix_task_id=fix_id,
        )
        before = _full_tables_state(conn)
        with pytest.raises(
            ValueError,
            match="decision must be resume, repair, or escalate",
        ):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _full_tables_state(conn) == before


def test_resolve_product_preflight_escalates_without_completion_gate(kanban_home):
    board = "resolver-entry-escalate"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="escalate",
            fault_domain="framework",
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None and task.status == "blocked" and task.blocked
    assert any(event.kind == "blocked" for event in events)


def test_resolve_product_preflight_rejects_stale_event_with_zero_mutation(kanban_home):
    board = "resolver-stale-event"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        expected = _resolver_expected(conn, tid, run_id)
        expected["preflight_event_id"] += 1
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(expected),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolve_product_preflight_rejects_changed_snapshot_field_with_zero_mutation(kanban_home):
    board = "resolver-stale-field"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        expected = _resolver_expected(conn, tid, run_id)
        expected["branch_name"] = "changed-after-inspection"
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(expected),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolve_product_preflight_rejects_wrong_run_profile(kanban_home):
    board = "resolver-wrong-run-profile"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        conn.execute("UPDATE task_runs SET profile='developer' WHERE id=?", (run_id,))
        conn.commit()
        before = _resolver_state(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(_resolver_expected(conn, tid, run_id)),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolve_product_preflight_rejects_preflight_routed_to_other_profile(
    kanban_home,
):
    board = "resolver-wrong-preflight-profile"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        preflight = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (tid, kb.PRODUCT_WORKFLOW_PRECHECK_EVENT),
        ).fetchone()
        payload = json.loads(preflight["payload"])
        payload["hermes_assignee"] = "architect"
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), preflight["id"]),
        )
        conn.commit()
        before = _resolver_state(conn, tid)

        with pytest.raises(kb.TaskSnapshotConflict):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(_resolver_expected(conn, tid, run_id)),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_rejects_overbound_governed_assignee_atomically(kanban_home):
    board = "resolver-repair-assignee-bound"
    _v2_product_board(board)
    metadata = kb.read_board_metadata(board)
    metadata.setdefault("product_workflow", {}).setdefault("assignees", {})["developer"] = "D" * 300
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")

    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "development"}},
        )
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="assignee"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model="test-model",
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_rejects_overbound_derived_workspace_path_atomically(
    kanban_home, tmp_path
):
    from hermes_cli import projects_db as pdb

    board = "resolver-repair-workspace-bound"
    _v2_product_board(board)
    long_primary_path = str(tmp_path / ("p" * 4100))
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Long Resolver Project",
            primary_path=long_primary_path,
            board_slug=board,
        )

    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"project_id": project_id}},
        )
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="workspace_path"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model="test-model",
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_workflow_repair_is_atomic_and_returns_to_ordinary_role(kanban_home):
    board = "resolver-workflow-repair"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "test", "assignee": "tester"}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == "test"
    assert task.assignee == "tester"
    assert task.status == "ready"
    assert not task.running and not task.blocked
    assert task.current_run_id is None


def test_resolver_repair_requires_at_least_one_semantic_field(kanban_home):
    board = "resolver-empty-repair"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="repair"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_rejects_unknown_project(kanban_home):
    board = "resolver-unknown-project"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="unknown project"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"workflow": {"project_id": "missing-project"}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_repair_derives_project_worktree_and_branch(kanban_home, tmp_path):
    from hermes_cli import projects_db as pdb

    board = "resolver-project-repair"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board(board)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Resolver Project",
            primary_path=str(repo),
            board_slug=board,
        )
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"project_id": project_id}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.project_id == project_id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(repo / ".worktrees" / tid)
    assert task.branch_name.startswith("resolver-project/")


def test_resolver_project_adoption_preserves_existing_canonical_worktree(
    kanban_home, tmp_path, monkeypatch
):
    board = "resolver-project-adoption"
    shared_board_home = tmp_path / "shared-board-root"
    shared_board_home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(shared_board_home))
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    resolver_home = kanban_home / "profiles" / "resolver"
    resolver_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(resolver_home))
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Legacy project adoption",
            assignee="developer",
            workspace_kind="worktree",
            board=board,
        )
        existing_workspace = repo / ".worktrees" / tid
        existing_workspace.mkdir(parents=True)
        kanban_db_workspace.set_workspace_path(conn, tid, existing_workspace)
        kanban_db_workspace.set_branch_name(conn, tid, f"wt/{tid}")
        _tid, run_id = _route_existing_task_to_resolver(conn, tid, board)

        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=_resolver_request(
                _resolver_expected(conn, tid, run_id),
                decision="repair",
                repair={"workflow": {"project_id": project_id}},
            ),
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.project_id == project_id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(existing_workspace)
    assert task.branch_name == f"wt/{tid}"


def test_resolver_project_adoption_rewrites_unsafe_workspace_path(
    kanban_home, tmp_path, monkeypatch
):
    from hermes_cli import projects_db as pdb

    board = "resolver-project-unsafe-adoption"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Unsafe project adoption",
            assignee="developer",
            workspace_kind="worktree",
            board=board,
        )
        monkeypatch.chdir(repo)
        unsafe_workspace = Path(".worktrees") / tid
        kanban_db_workspace.set_workspace_path(conn, tid, unsafe_workspace)
        kanban_db_workspace.set_branch_name(conn, tid, f"wt/{tid}")
        _tid, run_id = _route_existing_task_to_resolver(conn, tid, board)

        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=_resolver_request(
                _resolver_expected(conn, tid, run_id),
                decision="repair",
                repair={"workflow": {"project_id": project_id}},
            ),
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, tid)
    with pdb.connect_closing() as project_conn:
        project = pdb.get_project(project_conn, project_id)

    assert task is not None
    assert project is not None
    assert task.project_id == project_id
    assert task.workspace_path == str(repo / ".worktrees" / tid)
    assert task.branch_name == f"{project.slug}/{tid}-unsafe-project-adoption"


def test_resolver_repair_rejects_phase_assignee_mismatch(kanban_home):
    board = "resolver-role-mismatch"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="assignee"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"workflow": {"phase": "test", "assignee": "developer"}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


@pytest.mark.parametrize("phase", ["release_measure", "done", "archived"])
def test_resolver_repair_cannot_target_release_done_or_archived(kanban_home, phase):
    board = f"resolver-terminal-{phase}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="phase"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"workflow": {"phase": phase}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_framework_fault_can_only_escalate(kanban_home):
    board = "resolver-framework-only-escalate"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        with pytest.raises(ValueError, match="must escalate"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    fault_domain="framework",
                    repair={"workflow": {"phase": "development"}},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )


def test_resolver_repair_does_not_change_test_or_review_runs(kanban_home):
    board = "resolver-preserve-runs"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board, step="test")
        prior_before = [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND id<>? ORDER BY id",
                (tid, run_id),
            ).fetchall()
        ]
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "review", "assignee": "reviewer"}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model=None,
        )
        prior_after = [
            tuple(row) for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND id<>? ORDER BY id",
                (tid, run_id),
            ).fetchall()
        ]
    assert prior_after == prior_before


def test_successful_repair_appends_audit_and_needs_ole_events(kanban_home):
    board = "resolver-repair-audit"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "architecture", "assignee": "architect"}},
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        events = kb.list_events(conn, tid)
    audit = [event for event in events if event.kind == "resolver_repair_applied"][-1]
    attention = [event for event in events if event.kind == "needs_ole"][-1]
    assert audit.payload["fault_domain"] == "task_state"
    assert audit.payload["resolver"] == {
        "profile": "resolver",
        "model": "test-model",
        "run_id": run_id,
    }
    assert set(audit.payload["before"]) <= {
        "phase", "assignee", "project_id", "status", "workspace_kind",
        "workspace_path", "branch_name", "adopt_handoff_sha",
    }
    assert attention.payload["reason"] == "resolver_repair"


@pytest.mark.parametrize(
    "forbidden",
    [
        {"task_links": []},
        {"epic_memberships": []},
        {"work_contract": {"phase": "development"}},
    ],
)
def test_resolver_cannot_change_epic_membership_or_dependencies(
    kanban_home, forbidden,
):
    board = "resolver-immutable-relations"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        parent = kb.create_task(conn, title="Dependency", board=board)
        kb.link_tasks(conn, parent, tid)
        assert kb.parent_ids(conn, tid) == [parent]
        before = _resolver_state(conn, tid)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "development"}},
        )
        request.update(forbidden)
        with pytest.raises(ValueError, match="unexpected"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_resolver_cannot_override_release_classification(kanban_home):
    board = "resolver-immutable-release"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        before = _resolver_state(conn, tid)
        request = _resolver_request(
            _resolver_expected(conn, tid, run_id),
            decision="repair",
            repair={"workflow": {"phase": "development"}},
            release_path="standalone",
        )
        with pytest.raises(ValueError, match="unexpected"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_adopt_handoff_sha_requires_same_task_development_handoff_event(
    kanban_home, tmp_path,
):
    board = "resolver-adopt-same-task"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id, _workspace, sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        conn.execute(
            "DELETE FROM task_events WHERE task_id=? AND kind='handoff'", (tid,),
        )
        conn.commit()
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="Development handoff"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"adopt_handoff_sha": sha},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_adopt_handoff_sha_requires_current_project_branch_head(
    kanban_home, tmp_path,
):
    board = "resolver-adopt-current-head"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id, workspace, old_sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        _commit_file(workspace, "later.py", "value = 2\n", "later")
        before = _resolver_state(conn, tid)
        with pytest.raises(ValueError, match="branch HEAD"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"adopt_handoff_sha": old_sha},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before


def test_development_handoff_uses_valid_adopted_sha_when_tree_is_clean(
    kanban_home, tmp_path,
):
    board = "resolver-adopt-clean-handoff"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id, workspace, sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        assert kb.resolve_product_preflight(
            conn,
            tid,
            board=board,
            request=_resolver_request(
                _resolver_expected(conn, tid, run_id),
                decision="repair",
                repair={"adopt_handoff_sha": sha},
            ),
            resolver_profile="resolver",
            resolver_model="test-model",
        )
        assert subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout == ""
        assert kb.handoff(
            conn,
            tid,
            board=board,
            summary="Adopt the already committed Development handoff",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        task = kb.get_task(conn, tid)
        handoffs = [event for event in kb.list_events(conn, tid) if event.kind == "handoff"]
    assert task is not None and task.current_step_key == "test"
    assert handoffs[-1].payload["sha"] == sha


def test_invalid_adopted_sha_leaves_task_and_git_untouched(kanban_home, tmp_path):
    board = "resolver-adopt-invalid-atomic"
    repo, project_id = _resolver_project_fixture(kanban_home, tmp_path, board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id, workspace, _sha = _route_project_task_with_audited_handoff(
            conn, board, repo, project_id,
        )
        before = _resolver_state(conn, tid)
        head_before = _head_sha(workspace)
        status_before = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        with pytest.raises(ValueError, match="Development handoff"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=_resolver_request(
                    _resolver_expected(conn, tid, run_id),
                    decision="repair",
                    repair={"adopt_handoff_sha": "0" * 40},
                ),
                resolver_profile="resolver",
                resolver_model=None,
            )
        assert _resolver_state(conn, tid) == before
        assert _head_sha(workspace) == head_before
        assert subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout == status_before


@pytest.mark.parametrize("step", ["test", "review"])
def test_product_preflight_resolver_validation_precedes_rework(
    kanban_home, step
):
    board = f"resolver-before-rework-{step}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board, step=step)
        target = "development"
        with pytest.raises(ValueError, match="kanban_resolve"):
            kb.complete_task(
                conn,
                tid,
                summary="Trying to bypass resolver",
                metadata={
                    "workflow_outcome": {
                        "verdict": "changes_requested",
                        "target_step": target,
                        "findings": ["Workflow finding"],
                    }
                },
                expected_run_id=run_id,
                board=board,
            )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == step
    assert task.status == "running"
    assert task.rework_count == 0
    assert task.current_run_id == run_id


def test_product_preflight_requires_structured_resolver_action(kanban_home):
    board = "resolver-required"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        context = kb.build_worker_context(conn, tid)
        assert "## Required resolver action" in context
        assert "Original blocker:" in context
        assert "Attempted resolutions:" in context
        assert "Board policy:" in context
        assert "Resolve only with kanban_resolve" in context
        with pytest.raises(ValueError, match="kanban_resolve"):
            kb.complete_task(
                conn,
                tid,
                summary="I think it is fine",
                expected_run_id=run_id,
                board=board,
            )


@pytest.mark.parametrize(
    "request_mutation",
    [
        {"unexpected": True},
        {"fix_task_id": "t_not_allowed_for_resume"},
        {"diagnosis": ""},
    ],
)
def test_product_preflight_resolver_action_requires_exact_shape(
    kanban_home, request_mutation
):
    board = "resolver-exact-shape"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        request = _resolver_request(_resolver_expected(conn, tid, run_id))
        request.update(request_mutation)
        with pytest.raises(ValueError, match="resolver request|diagnosis"):
            kb.resolve_product_preflight(
                conn,
                tid,
                board=board,
                request=request,
                resolver_profile="resolver",
                resolver_model=None,
            )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == run_id


def test_product_preflight_resume_restores_original_step(kanban_home):
    board = "resolver-resume"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        assert _resolve_preflight(
            conn, tid, run_id, board,
            reason="Use the configured test token source",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.current_step_key == "development"
    assert task.assignee == "developer"
    assert task.status == "ready"
    assert task.running is False
    assert task.blocked is False


def test_product_preflight_escalate_enters_human_block(kanban_home):
    board = "resolver-escalate"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, run_id = _route_task_to_resolver(conn, board)
        assert _resolve_preflight(
            conn, tid, run_id, board,
            decision="escalate",
            fault_domain="framework",
            reason="Docs and local config are insufficient",
        )
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task is not None and task.status == "blocked" and task.blocked is True
    blocked = [event for event in events if event.kind == "blocked"]
    assert blocked
    assert blocked[-1].payload["kind"] == "resolver_escalation"
    assert blocked[-1].payload["resolution"] == "Docs and local config are insufficient"


def test_story_title_infers_product_without_role_on_product_board(kanban_home):
    board = "story-intent"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(conn, title="Story: explicit user intent", board=board)
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.workflow_template_id == "product"
    assert task.current_step_key == "backlog"


def test_set_phase_v2_board_updates_step_and_syncs_status(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-phase"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kanban_db_connect.connect(board=board) as conn:
        result = kb.set_phase(conn, tid, "review", board=board)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["current_step_key"] == "review"
    assert row["status"] == "review"
    assert row["status"] == kb._legacy_status(row, meta)


def test_set_running_v2_board_sets_flag_and_syncs_status(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kanban_db_connect.connect(board=board) as conn:
        result = kb.set_running(conn, tid, True, board=board)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["running"] == 1
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta)


def test_set_blocked_v2_board_sets_flag_and_syncs_status(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-blocked"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kanban_db_connect.connect(board=board) as conn:
        result = kb.set_blocked(conn, tid, True, board=board, reason="waiting on human")
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["blocked"] == 1
    assert row["status"] == "blocked"
    assert row["status"] == kb._legacy_status(row, meta)


def test_set_blocked_false_clears_back_to_phase_status(kanban_home, monkeypatch):
    """set_blocked(False) after a block clears back to the phase's base status."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-unblock"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kanban_db_connect.connect(board=board) as conn:
        kb.set_blocked(conn, tid, True, board=board)
        result = kb.set_blocked(conn, tid, False, board=board)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert result is True
    assert row["blocked"] == 0
    assert row["status"] == "ready"


# NOTE: test_set_running_and_blocked_precedence_matches_legacy_status (T1.3)
# is superseded by T1.4's _assert_card_consistent invariant: a card can no
# longer be both running and blocked, so that scenario now raises + rolls
# back instead of committing "blocked" precedence. See
# test_set_blocked_after_running_raises_limbo_and_leaves_card_unchanged below,
# which covers the identical setup and asserts the new behavior.


def test_set_phase_legacy_board_is_noop(kanban_home):
    """Legacy (non-v2) boards must be byte-for-byte unchanged."""
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        result = kb.set_phase(conn, tid, "review")
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert result is False
    assert after == before


def test_set_running_legacy_board_is_noop(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        result = kb.set_running(conn, tid, True)
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert result is False
    assert after == before


def test_set_blocked_legacy_board_is_noop(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        result = kb.set_blocked(conn, tid, True, reason="whatever")
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert result is False
    assert after == before


def test_set_phase_product_board_without_handoff_v2_is_noop(kanban_home, monkeypatch):
    """A product-preset board that has NOT opted into handoff_v2 also no-ops."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-no-v2"
    kb.create_board(board, name="Product No V2", preset="product")
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="Story", workflow_template_id="product", current_step_key="development",
        )
        result = kb.set_phase(conn, tid, "review", board=board)
        row = conn.execute(
            "SELECT current_step_key FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert result is False
    assert row["current_step_key"] == "development"


def test_set_phase_missing_task_returns_false(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-missing-phase"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.set_phase(conn, "does-not-exist", "review", board=board)
    assert result is False


def test_set_running_missing_task_returns_false(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-missing-running"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.set_running(conn, "does-not-exist", True, board=board)
    assert result is False


def test_set_blocked_missing_task_returns_false(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-missing-blocked"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.set_blocked(conn, "does-not-exist", True, board=board)
    assert result is False


# ---------------------------------------------------------------------------
# _assert_card_consistent invariant -- limbo is unrepresentable (T1.4)
# ---------------------------------------------------------------------------

def test_assert_card_consistent_running_only_is_valid():
    assert kb._assert_card_consistent({"running": 1, "blocked": 0}) is None


def test_assert_card_consistent_blocked_only_is_valid():
    assert kb._assert_card_consistent({"running": 0, "blocked": 1}) is None


def test_assert_card_consistent_neither_is_valid():
    assert kb._assert_card_consistent({"running": 0, "blocked": 0}) is None


def test_assert_card_consistent_running_and_blocked_raises():
    with pytest.raises(ValueError, match="running and blocked"):
        kb._assert_card_consistent({"running": 1, "blocked": 1})


def test_set_blocked_after_running_raises_limbo_and_leaves_card_unchanged(kanban_home, monkeypatch):
    """A running card cannot also become blocked: set_blocked(True) must
    raise ValueError, and (because the assert runs inside write_txn) the
    transaction rolls back -- the card is byte-for-byte unchanged."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-limbo-running-then-blocked"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kanban_db_connect.connect(board=board) as conn:
        kb.set_running(conn, tid, True, board=board)
        before = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

        with pytest.raises(ValueError, match="running and blocked"):
            kb.set_blocked(conn, tid, True, board=board)

        after = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert after == before
    assert after["running"] == 1
    assert after["blocked"] == 0


def test_set_running_after_blocked_raises_limbo_and_leaves_card_unchanged(kanban_home, monkeypatch):
    """Symmetric case: a blocked card cannot also become running."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-limbo-blocked-then-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kanban_db_connect.connect(board=board) as conn:
        kb.set_blocked(conn, tid, True, board=board)
        before = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

        with pytest.raises(ValueError, match="running and blocked"):
            kb.set_running(conn, tid, True, board=board)

        after = dict(conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert after == before
    assert after["blocked"] == 1
    assert after["running"] == 0


# ---------------------------------------------------------------------------
# Task creation + status inference
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Links + dependency resolution
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Atomic claim (CAS)
# ---------------------------------------------------------------------------



def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        assert any(e.kind == "scheduled" and e.payload == {"reason": "run next week"} for e in events)








def test_stale_claim_reclaim_event_records_diagnostic_payload(
    kanban_home, monkeypatch,
):
    """``reclaimed`` events should carry claim_expires, last_heartbeat_at,
    and worker_pid so operators can diagnose why a claim went stale
    (#23025: previous payload only had ``stale_lock`` which gives no
    timing context)."""
    import json
    import hermes_cli.kanban_db as _kb

    with kbc.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kbd._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 3600
        hb_at = int(time.time()) - 1800
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, hb_at, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (t,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["claim_expires"] == old_expires
        assert payload["last_heartbeat_at"] == hb_at
        assert payload["worker_pid"] == 12345
        assert payload["host_local"] is True




def test_rate_limit_exit_requeues_without_counting_failure(
    kanban_home, monkeypatch,
):
    """A rate-limit sentinel exit releases the task to ``ready`` and leaves
    ``consecutive_failures`` untouched — the breaker must never trip on a
    transient throttle, even across many quota-wall hits."""
    import hermes_cli.kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kbc.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")

        # Simulate FAR more quota-wall hits than DEFAULT_FAILURE_LIMIT (2).
        # If any of these counted as a failure the task would be blocked.
        for i in range(6):
            pid = 70000 + i
            # Claim to open a real run (so detect_crashed_workers can close
            # it with a rate_limited outcome), then point the claim at this
            # host + a dead pid so the crash path acts on it.
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? "
                "WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            _kbd._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            )

            crashed = kbd.detect_crashed_workers(conn)
            # Rate-limited requeues are NOT crashes.
            assert tid not in crashed
            rl = getattr(_kbd.detect_crashed_workers, "_last_rate_limited", [])
            assert tid in rl

            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.consecutive_failures == 0, (
                f"hit {i}: rate-limit must not count a failure, "
                f"got {task.consecutive_failures}"
            )

        # Last failure error stamped so the respawn guard recognizes the
        # quota wall.
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # A ``rate_limited`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "rate_limited" in outcomes
        assert "crashed" not in outcomes




def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None








# ---------------------------------------------------------------------------
# Complete / block / unblock / archive / assign
# ---------------------------------------------------------------------------





@pytest.mark.parametrize(
    ("step", "expected_assignee"),
    [
        ("architecture", "architect"),
        ("development", "developer"),
        ("test", "tester"),
    ],
)
def test_unblock_restores_product_step_assignee(
    kanban_home, step, expected_assignee
):
    board = f"unblock-restores-{step}"
    kb.ensure_product_board_defaults(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Escalated card",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == expected_assignee


def test_unblock_keeps_release_measure_unassigned(kanban_home):
    board = "unblock-keeps-release-unassigned"
    kb.ensure_product_board_defaults(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Release gate",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="release_measure",
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee is None


def test_unblock_does_not_clear_unmapped_executable_phase(kanban_home):
    board = "unblock-keeps-unmapped-executable-phase"
    kb.ensure_product_board_defaults(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Temporarily unmapped phase",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="development",
            board=board,
        )
        meta_path = kb.board_metadata_path(board)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["qualification"]["required"] = True
        meta["qualification"]["phase_assignees"]["development"] = None
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "default"


def test_unblock_keeps_custom_workflow_assignee_on_product_board(kanban_home):
    board = "unblock-keeps-custom-workflow-route"
    kb.ensure_product_board_defaults(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Custom workflow task",
            assignee="custom-worker",
            initial_status="blocked",
            workflow_template_id="custom",
            current_step_key="development",
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "custom-worker"


def test_unblock_uses_custom_non_strict_product_assignee(kanban_home):
    board = "unblock-custom-product-route"
    kb.ensure_product_board_defaults(board)
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["product_workflow"]["assignees"]["developer"] = "custom-developer"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Custom-routed product task",
            assignee="default",
            initial_status="blocked",
            workflow_template_id="product",
            current_step_key="development",
            board=board,
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "custom-developer"


def test_unblock_keeps_non_product_assignee_unchanged(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Generic blocked task",
            assignee="default",
            initial_status="blocked",
            current_step_key="development",
        )

        assert kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert task.assignee == "default"


def test_unblock_resets_failure_counters(kanban_home):
    """unblock_task must reset consecutive_failures and last_failure_error."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input")
        # Simulate accumulated failures from the circuit breaker
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 5, "
            "last_failure_error = 'test error' WHERE id = ?",
            (t,),
        )
        conn.commit()
        assert kb.unblock_task(conn, t)
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_recompute_ready_skips_tasks_at_failure_limit(kanban_home):
    """recompute_ready must not auto-recover tasks whose consecutive_failures
    has reached the circuit-breaker limit (#35072).

    Without this guard, a task that repeatedly exhausts its iteration
    budget would cycle forever: block → auto-recover (counter reset)
    → respawn → budget exhausted → block → …
    """
    with kanban_db_connect.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a",
                               parents=[parent])
        # Complete the parent so the child's dependencies are satisfied.
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, summary="done")

        # Simulate the child having exhausted its budget twice,
        # hitting the default failure limit (2).
        kb.claim_task(conn, child)
        kbd._record_task_failure(
            conn, child, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        kbd._record_task_failure(
            conn, child, error="budget exhausted 2",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.consecutive_failures >= 2

        # recompute_ready must NOT promote this task — the circuit
        # breaker has tripped and it should stay blocked.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"

        # Explicit unblock should still work and reset the counter.
        assert kb.unblock_task(conn, child)
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_recompute_ready_recovers_below_limit(kanban_home):
    """recompute_ready auto-recovers blocked tasks that haven't hit the
    failure limit yet — the counter is preserved across recovery."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="task", assignee="a")
        kb.claim_task(conn, t)
        # One failure, below the default limit of 2.
        kbd._record_task_failure(
            conn, t, error="budget exhausted 1",
            outcome="timed_out", release_claim=True, end_run=True,
            failure_limit=2,
        )
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Simulate being blocked by something else (not circuit breaker).
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        # Counter must be preserved, not reset.
        assert task.consecutive_failures == 1


def test_recompute_ready_honours_dispatcher_failure_limit(kanban_home):
    """The guard's effective limit must follow the same resolution order
    as the circuit breaker (#35072): per-task max_retries → dispatcher
    failure_limit → DEFAULT_FAILURE_LIMIT.

    Without threading the dispatcher's ``kanban.failure_limit`` through,
    the guard falls back to DEFAULT_FAILURE_LIMIT and disagrees with the
    breaker — sticking a task prematurely (config limit > default) or
    letting a tripped task escape (config limit < default).
    """
    with kbc.connect() as conn:
        # Config allows MORE retries than the default. A task blocked
        # with failures below the configured limit must still recover.
        t = kb.create_task(conn, title="lenient", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=? "
            "WHERE id=?",
            (kb.DEFAULT_FAILURE_LIMIT, t),
        )
        conn.commit()
        # Default-limit call would stick it (failures >= default).
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"
        # Dispatcher configured a higher limit → recover, preserve counter.
        promoted = kb.recompute_ready(
            conn, failure_limit=kb.DEFAULT_FAILURE_LIMIT + 2
        )
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT

        # Config allows FEWER retries than the default. A task at the
        # stricter limit must stay blocked even though it's below default.
        t2 = kb.create_task(conn, title="strict", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (t2,),
        )
        conn.commit()
        # Default-limit (2) would recover it (1 < 2).
        # Stricter config limit (1) must keep it blocked (1 >= 1).
        assert kb.recompute_ready(conn, failure_limit=1) == 0
        assert kb.get_task(conn, t2).status == "blocked"




# ---------------------------------------------------------------------------
# Parent-completion invariant at the claim gate (RCA t_a6acd07d)
# ---------------------------------------------------------------------------











def test_approve_unblock_task_checks_snapshot_and_comments_atomically(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Approve blocked card",
            body="Keep this body",
            assignee="developer",
            initial_status="blocked",
        )

        task = kb.approve_unblock_task(
            conn,
            tid,
            expected_status="blocked",
            expected_title="Approve blocked card",
            comment_author="agentic-os-cockpit/developer",
            comment_source="Agentic OS Cockpit approve/unblock control",
        )

        assert task is not None
        assert task.status == "ready"
        row = conn.execute(
            "SELECT status, body, assignee, consecutive_failures, last_failure_error "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert tuple(row) == ("ready", "Keep this body", "developer", 0, None)
        comments = kb.list_comments(conn, tid)
        assert len(comments) == 1
        assert comments[0].author == "agentic-os-cockpit/developer"
        assert "Decision: approved_unblock" in comments[0].body
        assert "Resulting status: ready" in comments[0].body
        events = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
            )
        ]
        assert events[-2:] == ["unblocked", "commented"]


def test_approve_unblock_task_rejects_stale_snapshot_without_comment(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Current title", initial_status="blocked")

        with pytest.raises(RuntimeError, match="refresh"):
            kb.approve_unblock_task(
                conn,
                tid,
                expected_status="blocked",
                expected_title="Old title",
                comment_author="agentic-os-cockpit/developer",
                comment_source="Agentic OS Cockpit approve/unblock control",
            )

        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.list_comments(conn, tid) == []
        event_kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
            )
        ]
        assert "unblocked" not in event_kinds
        assert "commented" not in event_kinds


def test_approve_unblock_task_uses_todo_when_parent_is_not_done(kanban_home):
    with kanban_db_connect.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="Blocked child", parents=[parent])
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (child,))
        conn.commit()

        task = kb.approve_unblock_task(
            conn,
            child,
            expected_status="blocked",
            expected_title="Blocked child",
            comment_author="agentic-os-cockpit/developer",
            comment_source="Agentic OS Cockpit approve/unblock control",
        )

        assert task is not None
        assert task.status == "todo"
        comments = kb.list_comments(conn, child)
        assert "Resulting status: todo" in comments[0].body


def test_assign_refuses_while_running(kanban_home):
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, t)
        with pytest.raises(RuntimeError, match="currently running"):
            kb.assign_task(conn, t, "b")



def test_delete_archived_task_removes_related_rows(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        tid = kb.create_task(conn, title="child", parents=[parent], assignee="worker")
        kb.add_comment(conn, tid, "user", "cleanup me")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="done")
        assert kb.archive_task(conn, tid)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', '123', '', 'u', 0, 0)",
            (tid,),
        )
        conn.commit()

        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (tid, tid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_task_removes_task_and_cascades(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="to-delete", assignee="alice")
        kb.add_comment(conn, t, "user", "comment")
        kb.add_comment(conn, t, "user", "another")
        assert kb.delete_task(conn, t)
        assert kb.get_task(conn, t) is None
        assert len(kb.list_comments(conn, t)) == 0
        assert len(kb.list_events(conn, t)) == 0
        assert len(kb.list_runs(conn, t)) == 0




# ---------------------------------------------------------------------------
# Comments / events / worker context
# ---------------------------------------------------------------------------


def test_worker_context_marks_product_test_as_evidence_only(
    kanban_home, tmp_path, all_assignees_spawnable,
):
    board = "worker-context-evidence-boundary"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _v2_product_board_with_repo(board, repo)

    with kanban_db_connect.connect(board=board) as conn:
        tid, _workspace, _branch = _seed_product_test_worktree(conn, board, repo)
        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *args, **kwargs: 5252,
        )
        assert result.spawned[0][0] == tid
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        run = kb.get_run(conn, task.current_run_id)
        context = kb.build_worker_context(conn, tid)

    assert run is not None and run.metadata is not None
    assert "## Evidence-phase source boundary" in context
    assert "never commit source or fixture changes" in context
    assert "workflow_outcome.verdict=changes_requested" in context
    assert run.metadata["test_head_sha"] in context







# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Respawn guard (check_respawn_guard + dispatch_once integration)
# ---------------------------------------------------------------------------







def test_respawn_guard_blocker_auth_on_authentication_error(kanban_home):
    """Full word 'Authentication' triggers blocker_auth (regex covers auth\\w*)."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="authn-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("Authentication failed: invalid credentials", t),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authorization_error(kanban_home):
    """Full word 'authorization' triggers blocker_auth (regex covers auth\\w*)."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="authz-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("authorization denied for scope repo", t),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_recent_success(kanban_home):
    """A completed run within the guard window triggers recent_success."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="already-done", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "recent_success"


def test_respawn_guard_advanced_outcome_does_not_park_pipeline(kanban_home):
    """A product-workflow step-advance (outcome='advanced') must NOT trip the
    recent_success guard — otherwise every pipeline hop parks the card for the
    full guard window. This is the regression that stalled the Trading Company
    board when a parallel branch stamped step-advances as 'completed'."""
    kb.create_board("prod", preset="product")
    with kanban_db_connect.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="User story: pipeline hop",
            assignee="architect-profile",
            workflow_template_id="product",
            current_step_key="architecture",
        )
        # Advancing the step records a run — it must be outcome='advanced',
        # which the guard ignores, so the next role can spawn immediately.
        assert kb.complete_task(
            conn, tid, summary="architecture done", board="prod",
            product_role_assignees={"developer": "developer-profile"},
        )
        latest = kb.latest_run(conn, tid)
        reason = kbd.check_respawn_guard(conn, tid)
    assert latest.outcome == "advanced", "step-advance must not be 'completed'"
    assert reason is None, f"pipeline card wrongly parked: {reason}"


def test_respawn_guard_recent_success_bypassed_by_requeue(kanban_home):
    """An explicit re-queue after a recent success (operator done->ready,
    promote, unblock, reclaim) is a deliberate re-run and must bypass the
    recent_success guard — otherwise a manual done->ready just sits there
    until the window elapses."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="rerun-me", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        # Baseline: a recent completion defers the respawn.
        assert kbd.check_respawn_guard(conn, t) == "recent_success"
        # Operator drags done -> ready: a 'status' event after completion.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'status', ?)",
            (t, now - 10),
        )
        assert kbd.check_respawn_guard(conn, t) is None


def test_respawn_guard_stale_success_not_guarded(kanban_home):
    """A completed run outside the guard window does not block re-spawn."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="old-done", assignee="alice")
        old_end = int(time.time()) - kbd._RESPAWN_GUARD_SUCCESS_WINDOW - 60
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, old_end - 300, old_end),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_in_comment(kanban_home):
    """A GitHub PR URL in a recent comment triggers active_pr."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_old_pr_comment_not_guarded(kanban_home):
    """A GitHub PR URL in a comment older than the PR window does not block."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="old-pr", assignee="alice")
        old_ts = int(time.time()) - kbd._RESPAWN_GUARD_PR_WINDOW - 60
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'worker', "
            "'PR: https://github.com/totemx-AI/subsidysmart/pull/10', ?)",
            (t, old_ts),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason is None


def test_dispatch_respawn_guard_defers_auth_error_without_auto_block(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose last
    error is a blocker_auth.

    The old behaviour auto-blocked on first occurrence, which was too
    aggressive: a transient 429 rate-limit (which typically clears in
    seconds to minutes) would end up requiring manual unblock. The new
    behaviour defers the spawn this tick; the task stays in ``ready``
    and gets another chance next tick. If the auth error genuinely
    persists, the existing ``consecutive_failures`` circuit breaker
    will auto-block via the normal failure-limit path.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="quota-storm", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("rate limit exceeded: 429 Too Many Requests", t),
        )
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    # Critical: task is NOT auto-blocked on first occurrence.
    assert t not in res.auto_blocked, (
        f"blocker_auth should defer, not auto-block on first occurrence; "
        f"got auto_blocked={res.auto_blocked!r}"
    )
    # It IS recorded as respawn_guarded with the reason.
    assert (t, "blocker_auth") in res.respawn_guarded, (
        f"expected (task_id, 'blocker_auth') in respawn_guarded; "
        f"got {res.respawn_guarded!r}"
    )
    # And it's NOT spawned this tick.
    assert t not in spawned_ids
    # Status stays ``ready`` so a future tick (or operator action) can
    # retry without manual unblock.
    with kanban_db_connect.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_skips_recent_success(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with a recent completed run."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="recent-winner", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "recent_success") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kanban_db_connect.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # not blocked, just skipped


def test_dispatch_respawn_guard_skips_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with an active PR comment."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.add_comment(
            conn, t, "worker",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kanban_db_connect.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_dry_run_no_auto_block(
    kanban_home, all_assignees_spawnable
):
    """In dry_run mode, blocker_auth tasks are recorded in respawn_guarded (not auto-blocked)."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="dry-quota", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("quota exceeded", t),
        )
        res = kbd.dispatch_once(conn, dry_run=True)

    assert (t, "blocker_auth") in res.respawn_guarded
    assert t not in res.auto_blocked
    with kanban_db_connect.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # dry_run: no writes


def test_dispatch_respawn_guard_allows_clean_task(
    kanban_home, all_assignees_spawnable
):
    """A task with no guard triggers is spawned normally."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="clean-task", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids
    assert not res.respawn_guarded
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_emits_event_for_skipped_task(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once emits a respawn_guarded task_event so operators can diagnose stuck-ready tasks."""
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="event-check", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "respawn_guarded" in kinds
    guarded_evt = next(e for e in events if e.kind == "respawn_guarded")
    # Event.payload is already parsed as a dict by list_events.
    assert isinstance(guarded_evt.payload, dict)
    assert guarded_evt.payload.get("reason") == "recent_success"


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------









def test_worktree_workspace_explicit_target_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "custom-task"
    branch = "wt/custom-task"
    with kbc.connect() as conn:
        t = kb.create_task(
            conn,
            title="ship",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=branch,
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kbw.resolve_workspace(task)

    assert ws == target
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {target}" in listed
    assert f"branch refs/heads/{branch}" in listed


# ---------------------------------------------------------------------------
# Epic-branch base ref threading for v2 story worktrees (T4.1)
# ---------------------------------------------------------------------------

def test_ensure_git_worktree_base_param_branches_off_given_branch(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "feat"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "feat"], check=True, capture_output=True, text=True)
    feat_sha = _commit_file(repo, "feat.txt", "feat\n", "feat commit")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)

    target = repo / ".worktrees" / "story1"
    kb._ensure_git_worktree(repo, target, "story1", base="feat")

    assert (target / "feat.txt").exists()
    subprocess.run(
        ["git", "-C", str(target), "merge-base", "--is-ancestor", feat_sha, "HEAD"],
        check=True, capture_output=True, text=True,
    )


def test_ensure_git_worktree_default_base_still_branches_off_head(tmp_path):
    """Legacy call sites (no ``base`` kwarg) keep branching off HEAD, byte-for-byte."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "feat"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "feat"], check=True, capture_output=True, text=True)
    _commit_file(repo, "feat.txt", "feat\n", "feat commit")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    target = repo / ".worktrees" / "story-legacy"
    kb._ensure_git_worktree(repo, target, "story-legacy")

    assert not (target / "feat.txt").exists()
    worktree_head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert worktree_head == head_sha


def test_epic_branch_for_naming():
    assert kb.epic_branch_for("epic-123") == "epic/epic-123"


def test_ensure_epic_branch_creates_off_head_idempotently(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    epic_branch = kb.epic_branch_for("epic-1")

    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    assert kb._git_branch_exists(repo, epic_branch)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    branch_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", epic_branch], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert branch_sha == head_sha

    # Idempotent: calling again does not error or move the branch.
    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    branch_sha_again = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", epic_branch], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert branch_sha_again == head_sha


def test_story_base_branch_v2_story_with_epic_parent_returns_epic_branch(kanban_home):
    board = "v2-story-base-branch"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        result = kb._story_base_branch(conn, story, board=board)
    assert result == kb.epic_branch_for(epic)


def test_story_base_branch_no_parent_returns_none(kanban_home):
    board = "v2-story-base-branch-no-parent"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        story = kb.create_task(conn, title="Story", board=board)
        result = kb._story_base_branch(conn, story, board=board)
    assert result is None


def test_story_base_branch_non_v2_board_returns_none(kanban_home):
    board = "legacy-story-base-branch"
    kb.create_board(board, name="Legacy Board", preset="product")
    with kanban_db_connect.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        result = kb._story_base_branch(conn, story, board=board)
    assert result is None


def test_resolve_worktree_workspace_default_base_branch_none_uses_head(kanban_home, tmp_path):
    """Legacy call sites (no ``base_branch`` kwarg) keep branching off HEAD."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    with kanban_db_connect.connect() as conn:
        t = kb.create_task(conn, title="ship", workspace_kind="worktree", workspace_path=str(repo))
        task = kb.get_task(conn, t)
        assert task is not None
        ws, _branch = kb._resolve_worktree_workspace(task)
    ws_head = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert ws_head == head_sha


def test_story_worktree_branches_off_epic_branch_contains_upstream_commit(kanban_home, tmp_path):
    """The plan's key test: a downstream story's worktree must contain the
    upstream story's committed code, proven by branching off the epic branch."""
    board = "v2-epic-branching"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    with kanban_db_connect.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        epic_branch = kb.epic_branch_for(epic)

    # Simulate an upstream story's integrated commit landing on the epic branch.
    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    subprocess.run(["git", "-C", str(repo), "checkout", epic_branch], check=True, capture_output=True, text=True)
    upstream_sha = _commit_file(repo, "upstream.txt", "upstream story code\n", "upstream story")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)

    with kanban_db_connect.connect(board=board) as conn:
        story = kb.create_task(
            conn, title="Story", board=board,
            workspace_kind="worktree", workspace_path=str(repo),
        )
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        task = kb.get_task(conn, story)
        assert task is not None
        ws, _branch = kb._resolve_worktree_workspace(
            task, board=board, base_branch=epic_branch,
        )

    assert (ws / "upstream.txt").exists()
    subprocess.run(
        ["git", "-C", str(ws), "merge-base", "--is-ancestor", upstream_sha, "HEAD"],
        check=True, capture_output=True, text=True,
    )


def test_spawn_one_v2_wires_story_base_branch_to_epic(kanban_home, tmp_path, monkeypatch):
    """_spawn_one_v2 (the v2 spawn path) computes _story_base_branch and threads
    it into _resolve_worktree_workspace, so a v2 story's worktree lands on top
    of its epic branch -- without touching the live dispatch loop."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-epic-branch"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)

    with kanban_db_connect.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        epic_branch = kb.epic_branch_for(epic)

    kb._ensure_epic_branch(repo, epic_branch, start_point=kb._git_head_sha(repo))
    subprocess.run(["git", "-C", str(repo), "checkout", epic_branch], check=True, capture_output=True, text=True)
    upstream_sha = _commit_file(repo, "upstream.txt", "upstream story code\n", "upstream story")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True, text=True)

    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        story = kb.create_task(
            conn, title="Story", board=board,
            assignee="developer", workspace_kind="worktree", workspace_path=str(repo),
        )
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        task = kb.get_task(conn, story)
        assert task is not None and task.status == "ready", (
            "story with a done epic parent should be immediately ready"
        )
        pid = kb._spawn_one_v2(conn, story, board=board, spawn_fn=fake_spawn)

    assert pid == 4242
    assert len(spawns) == 1
    ws = Path(spawns[0][1])
    assert (ws / "upstream.txt").exists()
    subprocess.run(
        ["git", "-C", str(ws), "merge-base", "--is-ancestor", upstream_sha, "HEAD"],
        check=True, capture_output=True, text=True,
    )


def test_spawn_one_v2_success_sets_running_flag(kanban_home, tmp_path, monkeypatch):
    """A successful _spawn_one_v2 spawn ends with the v2 running flag set.
    R1 update: the flag is now set by claim_task (the seam _spawn_one_v2
    calls internally), not by a separate set_running() call in this
    function -- see test_claim_task_v2_board_sets_running_flag_and_consistent_status
    for direct coverage of that seam."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-sets-running"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def fake_spawn(task, workspace, board=None):
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        pid = kb._spawn_one_v2(conn, tid, board=board, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        row = conn.execute(
            "SELECT running, status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert pid == 4242
    assert task is not None and task.worker_pid == 4242
    assert row["running"] == 1
    assert row["status"] == "running"


def test_spawn_one_v2_resolver_preflight_from_test_skips_test_target_pinning(
    kanban_home, tmp_path, monkeypatch
):
    board = "v2-spawn-test-resolver-preflight"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawned: list[tuple[str, str]] = []

    monkeypatch.setattr(kb, "_stamp_run_executor_identity", lambda *_args: None)

    def fake_spawn(task, workspace, board=None):
        spawned.append((task.assignee, workspace))
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Test finding needs Resolver",
            board=board,
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        tester = kb.claim_task(conn, task_id, board=board)
        assert tester is not None and tester.current_run_id is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Required smoke is not exposed through the trusted runner",
            kind="capability",
            attempted_resolutions=["Ran the allowed repository test runner"],
            expected_run_id=tester.current_run_id,
            board=board,
            human_escalation_assignee="resolver",
        )
        assert kb.has_unresolved_product_preflight(conn, task_id)

        pid = kb._spawn_one_v2(
            conn, task_id, board=board, spawn_fn=fake_spawn
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert pid == 4242
    assert spawned == [("resolver", str(repo))]
    assert task is not None
    assert task.status == "running"
    assert task.blocked is False
    assert not [
        event
        for event in events
        if event.kind == "spawn_failed"
        and "test target preparation" in str(event.payload)
    ]


def test_spawn_one_v2_stamps_runtime_identity_before_spawn(
    kanban_home, tmp_path, monkeypatch
):
    """The event-driven v2 path stamps the same executor facts as polling."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-stamps-runtime"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    stamped: list[str] = []

    def stamp(_conn, task):
        stamped.append(task.id)
        return {
            "profile": "developer",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
        }

    monkeypatch.setattr(kb, "_stamp_run_executor_identity", stamp)

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        pid = kb._spawn_one_v2(
            conn,
            tid,
            board=board,
            spawn_fn=lambda _task, _workspace, board=None: 4242,
        )

    assert pid == 4242
    assert stamped == [tid]


def test_spawn_one_v2_failure_clears_running_flag(kanban_home, tmp_path, monkeypatch):
    """R3 fix: claim_task sets running=1 at claim time (R1), and a failed
    spawn now goes through _record_task_failure (via _record_spawn_failure),
    which clears ``running`` back to 0 alongside the legacy ``status`` revert
    to 'ready' -- closing the status/flag gap R1's reviewer flagged. This
    test used to pin the pre-R3 gap (running stuck at 1); it now asserts the
    fixed behavior."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-failure-no-running"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def boom(task, workspace, board=None):
        raise RuntimeError("spawn failed")

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        pid = kb._spawn_one_v2(conn, tid, board=board, spawn_fn=boom)
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert pid is None
    # R3: _record_task_failure now clears running (and blocked) on the
    # failure path, so flags and status agree again.
    assert row["running"] == 0
    assert row["blocked"] == 0
    assert row["status"] != "running"


def test_spawn_then_handoff_running_flag_round_trip(kanban_home, tmp_path, monkeypatch):
    """W2 lifecycle: spawn sets running=1 (via _spawn_one_v2), then handoff
    clears it back to running=0 on the same card -- the full set/clear
    round-trip the running flag is meant to support."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-handoff-roundtrip"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def fake_spawn(task, workspace, board=None):
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        kb._spawn_one_v2(conn, tid, board=board, spawn_fn=fake_spawn)
        after_spawn = conn.execute(
            "SELECT running FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        spawned_workspace = Path(kb.get_task(conn, tid).workspace_path)
        (spawned_workspace / "src.py").write_text("print('hi')\n", encoding="utf-8")
        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        after_handoff = conn.execute(
            "SELECT running FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

    assert after_spawn["running"] == 1
    assert result is True
    assert after_handoff["running"] == 0


def test_handoff_releases_worker_claim_so_next_agent_can_spawn(kanban_home, tmp_path, monkeypatch):
    """Regression: handoff must release the completing worker's claim
    (claim_lock / claim_expires / worker_pid), not just clear ``running``.

    Otherwise the handed-off card stays ready+claimed, and
    ``spawn_after_handoff`` (``WHERE claim_lock IS NULL``) skips it -- the
    event-driven chain stalls at every handoff until a manual reclaim.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-releases-claim"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    def fake_spawn(task, workspace, board=None):
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        kb._spawn_one_v2(conn, tid, board=board, spawn_fn=fake_spawn)
        after_spawn = conn.execute(
            "SELECT claim_lock, worker_pid FROM tasks WHERE id = ?", (tid,)
        ).fetchone()

        spawned_workspace = Path(kb.get_task(conn, tid).workspace_path)
        (spawned_workspace / "src.py").write_text("print('hi')\n", encoding="utf-8")
        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        after_handoff = conn.execute(
            "SELECT status, claim_lock, claim_expires, worker_pid FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    # spawn claimed the card
    assert after_spawn["claim_lock"] is not None
    # handoff advanced AND released the claim -> card is ready + unclaimed,
    # which is exactly what spawn_after_handoff requires to fire the next agent.
    assert result is True
    assert after_handoff["status"] == "ready"
    assert after_handoff["claim_lock"] is None
    assert after_handoff["claim_expires"] is None
    assert after_handoff["worker_pid"] is None


# ---------------------------------------------------------------------------
# R1: claim_task maintains the v2 running flag (state-model integrity)
#
# The v2 running flag used to be set only by _spawn_one_v2 -- a path the
# LIVE gateway never calls (it spawns via dispatch_once -> claim_task
# directly). So a gateway-spawned v2 card ended up status='running',
# running=0: flags and status disagreeing, the exact defect the v2 state
# model exists to prevent. _apply_v2_flags is the single in-txn seam that
# fixes this; claim_task is its first (and, after this task, only) caller
# for the running flag on the claim path.
# ---------------------------------------------------------------------------

def test_apply_v2_flags_sets_flag_and_syncs_status(kanban_home, monkeypatch):
    """Direct unit coverage of the seam helper: sets the requested flag(s)
    and re-derives legacy status via _sync_legacy_status."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-apply-flags"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kanban_db_connect.connect(board=board) as conn:
        with kb.write_txn(conn):
            kb._apply_v2_flags(conn, tid, meta, running=True, blocked=False)
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert row["running"] == 1
    assert row["blocked"] == 0
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta)


def test_apply_v2_flags_legacy_board_is_noop(kanban_home):
    """meta=None (legacy board) -- flags and status must be untouched."""
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task")
        before = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())
        with kb.write_txn(conn):
            kb._apply_v2_flags(conn, tid, None, running=True, blocked=True)
        after = dict(conn.execute(
            "SELECT current_step_key, status, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert after == before


def test_apply_v2_flags_noop_when_not_handoff_v2_enabled(kanban_home, monkeypatch):
    """A product-preset board that hasn't opted into handoff_v2 also no-ops."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-no-v2-apply-flags"
    kb.create_board(board, name="Product No V2", preset="product")
    meta = kb.read_board_metadata(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="Story", workflow_template_id="product", current_step_key="development",
        )
        before = dict(conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone())
        with kb.write_txn(conn):
            kb._apply_v2_flags(conn, tid, meta, running=True, blocked=True)
        after = dict(conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert after == before


def test_claim_task_v2_board_sets_running_flag_and_consistent_status(kanban_home, monkeypatch):
    """claim_task itself (not _spawn_one_v2) must set running=1 on a v2
    board's card -- this is the fix for the gateway-bypasses-the-flag gap."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-claim-sets-running"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")
    meta = kb.read_board_metadata(board)

    with kanban_db_connect.connect(board=board) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        claimed = kb.claim_task(conn, tid, claimer="host:1")
        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert claimed is not None
    assert row["running"] == 1
    assert row["blocked"] == 0
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta)


def test_claim_task_legacy_board_does_not_touch_flags(kanban_home):
    """Legacy (non-v2) boards: claim_task must remain byte-for-byte
    unchanged -- neither running nor blocked is touched by the claim."""
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="Legacy task", assignee="alice")
        claimed = kb.claim_task(conn, tid, claimer="host:1")
        row = conn.execute(
            "SELECT running, blocked, status FROM tasks WHERE id = ?", (tid,),
        ).fetchone()

    assert claimed is not None
    assert row["status"] == "running"
    assert row["running"] == 0
    assert row["blocked"] == 0


def test_dispatch_once_gateway_spawn_sets_running_flag(kanban_home, tmp_path, monkeypatch):
    """THE key integration test: drive the REAL dispatch_once -> claim_task
    live-gateway path (not _spawn_one_v2, not set_running directly) on a v2
    board and prove the claimed card's running flag and legacy status agree.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    board = "v2-dispatch-sets-running"
    _v2_product_board(board)
    meta = kb.read_board_metadata(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            board=board,
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        result = kbd.dispatch_once(conn, spawn_fn=fake_spawn, board=board)

        row = conn.execute(
            "SELECT current_step_key, running, blocked, status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()

    assert len(spawns) == 1
    assert spawns[0][0] == tid
    assert {s[0] for s in result.spawned} == {tid}
    assert row["running"] == 1
    assert row["blocked"] == 0
    assert row["status"] == "running"
    assert row["status"] == kb._legacy_status(row, meta), (
        "flags and status must not disagree on a live v2 board"
    )


def test_task_from_row_exposes_running_and_blocked(kanban_home, monkeypatch):
    """get_task/Task.from_row surfaces running/blocked reflecting the row."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-task-exposes-flags"
    _v2_product_board(board)
    tid = _seed_v2_card(board, step="development")

    with kanban_db_connect.connect(board=board) as conn:
        conn.execute(
            "UPDATE tasks SET running = 1, blocked = 0 WHERE id = ?", (tid,)
        )
        task = kb.get_task(conn, tid)

    assert task.running is True
    assert task.blocked is False


def test_task_from_row_defaults_flags_false_when_columns_absent():
    """Defensive mapping: a row lacking running/blocked columns (e.g. an
    older schema snapshot) defaults both to False rather than raising."""
    row = {
        "id": "t1",
        "title": "T",
        "body": None,
        "assignee": None,
        "status": "ready",
        "priority": 0,
        "created_by": None,
        "created_at": 0,
        "started_at": None,
        "completed_at": None,
        "workspace_kind": "inline",
        "workspace_path": None,
        "claim_lock": None,
        "claim_expires": None,
    }
    task = kb.Task.from_row(row)
    assert task.running is False
    assert task.blocked is False


def test_dependency_source_base_selects_required_parent_receipt(kanban_home, tmp_path):
    repo = tmp_path / "dependency-source-base"
    _init_git_repo(repo)


    with kanban_db_connect.connect() as conn:
        parent_id = kb.create_task(
            conn, title="Parent", workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.claim_task(conn, parent_id)
        assert parent is not None and parent.current_run_id is not None
        (repo / "parent.txt").write_text("parent\n", encoding="utf-8")
        assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
        child_id = kb.create_task(conn, title="Child", parents=[parent_id])
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == _head_sha(repo)


def test_dependency_source_base_uses_latest_completed_receipt(kanban_home, tmp_path):
    repo = tmp_path / "completed-receipt-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect() as conn:
        parent_id = kb.create_task(
            conn, title="Parent", workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.claim_task(conn, parent_id)
        assert parent is not None and parent.current_run_id is not None
        (repo / "first.txt").write_text("first\n", encoding="utf-8")
        assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
        first_sha = _head_sha(repo)
        conn.execute(
            "UPDATE task_runs SET ended_at = ended_at + 100 WHERE id = ?",
            (parent.current_run_id,),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, metadata) "
            "VALUES (?, 'crashed', 'crashed', 200, 300, ?)",
            (parent_id, json.dumps({"source_completion_receipt": {"commit_sha": first_sha}})),
        )
        child_id = kb.create_task(conn, title="Child", parents=[parent_id])
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == first_sha


def test_dependency_source_base_deduplicates_identical_receipts(kanban_home, tmp_path):
    repo = tmp_path / "duplicate-receipt-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect() as conn:
        parents = []
        shared_sha = _head_sha(repo)
        for title in ("One", "Two"):
            parent_id = kb.create_task(
                conn, title=title, workspace_kind="worktree", workspace_path=str(repo),
                source_commit_required=True,
            )
            parent = kb.claim_task(conn, parent_id)
            assert parent is not None and parent.current_run_id is not None
            (repo / f"{title.lower()}.txt").write_text(f"{title}\n", encoding="utf-8")
            assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps({"source_completion_receipt": {"commit_sha": shared_sha}}), parent.current_run_id),
            )
            parents.append(parent_id)
        child_id = kb.create_task(conn, title="Child", parents=parents)
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == shared_sha


def test_resolve_worktree_workspace_rejects_divergent_receipts_before_materializing(
    kanban_home, tmp_path, monkeypatch
):
    board = "dependency-source-divergence"
    kb.create_board(board, name="Dependency Source", preset="generic")
    repo = tmp_path / "divergent-receipts-repo"
    _init_git_repo(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "side"], check=True,
                   capture_output=True, text=True)
    second_sha = _commit_file(repo, "second.txt", "second\n", "second")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True,
                   capture_output=True, text=True)
    first_sha = _commit_file(repo, "first.txt", "first\n", "first")
    target = repo / ".worktrees" / "child"
    monkeypatch.setattr(kb, "_story_base_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(kb, "_handoff_v2_enabled", lambda _meta: False)
    with kanban_db_connect.connect(board=board) as conn:
        parents = []
        for title, sha in (("One", first_sha), ("Two", second_sha)):
            parent_id = kb.create_task(
                conn, title=title, board=board, workspace_kind="worktree", workspace_path=str(repo),
                source_commit_required=True,
            )
            parent = kb.claim_task(conn, parent_id)
            assert parent is not None and parent.current_run_id is not None
            (repo / f"{title.lower()}.txt").write_text(f"{title}\n", encoding="utf-8")
            assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps({"source_completion_receipt": {"commit_sha": sha}}),
                 parent.current_run_id),
            )
            parents.append(parent_id)
        child_id = kb.create_task(
            conn, title="Child", board=board, parents=parents, workspace_kind="worktree",
            workspace_path=str(repo),
        )
        child = kb.get_task(conn, child_id)
        assert child is not None
        with pytest.raises(RuntimeError, match="diverge"):
            kb._resolve_worktree_workspace(child, board=board, conn=conn)
    assert not target.exists()
    assert not kb._git_branch_exists(repo, f"wt/{child_id}")


def test_dependency_source_base_linear_multi_parent_uses_descendant(kanban_home, tmp_path):
    repo = tmp_path / "linear-multi-parent-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect() as conn:
        parent_ids = []
        for title in ("Ancestor", "Descendant"):
            parent_id = kb.create_task(conn, title=title, workspace_kind="worktree", workspace_path=str(repo), source_commit_required=True)
            parent = kb.claim_task(conn, parent_id)
            assert parent is not None and parent.current_run_id is not None
            (repo / f"{title.lower()}.txt").write_text(title, encoding="utf-8")
            assert kb.complete_task(conn, parent_id, expected_run_id=parent.current_run_id)
            parent_ids.append(parent_id)
        child_id = kb.create_task(conn, title="Child", parents=parent_ids)
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert kb._dependency_source_base(conn, child, repo) == _head_sha(repo)


def test_dependency_source_flow_resolves_parent_and_child_worktrees(kanban_home, tmp_path):
    board = "dependency-source-e2e"
    kb.create_board(board, name="Dependency Source E2E", preset="generic")
    repo = tmp_path / "dependency-source-e2e-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        parent_id = kb.create_task(
            conn, title="Parent", board=board, assignee="developer",
            workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.get_task(conn, parent_id)
        assert parent is not None
        parent_ws, parent_branch = kb._resolve_worktree_workspace(parent, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(parent_ws), parent_id))
        (parent_ws / "parent.txt").write_text("parent content\n", encoding="utf-8")
        claimed = kb.claim_task(conn, parent_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(conn, parent_id, expected_run_id=claimed.current_run_id)
        child_id = kb.create_task(conn, title="Child", board=board, parents=[parent_id], workspace_kind="worktree", workspace_path=str(repo), source_commit_required=True)
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "ready"
        child_ws, child_branch = kb._resolve_worktree_workspace(child, board=board, conn=conn)
    assert parent_ws != child_ws
    assert parent_branch != child_branch
    assert (child_ws / "parent.txt").read_text(encoding="utf-8") == "parent content\n"


def test_dependency_source_flow_resolves_concrete_child_worktree_from_parent(kanban_home, tmp_path):
    board = "dependency-source-concrete-child"
    kb.create_board(board, name="Dependency Source Concrete Child", preset="generic")
    repo = tmp_path / "dependency-source-concrete-child-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        parent_id = kb.create_task(
            conn, title="Parent", board=board, assignee="developer",
            workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True,
        )
        parent = kb.get_task(conn, parent_id)
        assert parent is not None
        parent_ws, parent_branch = kb._resolve_worktree_workspace(parent, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(parent_ws), parent_id))
        (parent_ws / "parent.txt").write_text("parent content\n", encoding="utf-8")
        claimed = kb.claim_task(conn, parent_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(conn, parent_id, expected_run_id=claimed.current_run_id)

        child_id = kb.create_task(
            conn, title="Child", board=board, parents=[parent_id], assignee="developer",
            workspace_kind="worktree", workspace_path=str(repo / ".worktrees" / "child"),
            branch_name=f"child/{parent_id}",
        )
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "ready"
        child_ws, child_branch = kb._resolve_worktree_workspace(child, board=board, conn=conn)

    assert parent_branch != child_branch
    assert child_ws == repo / ".worktrees" / "child"
    assert (child_ws / "parent.txt").read_text(encoding="utf-8") == "parent content\n"


def test_complete_task_required_source_commits_before_terminal_update_and_persists_receipt(
    kanban_home, tmp_path
):
    repo = tmp_path / "completion-repo"
    _init_git_repo(repo)
    base_sha = _head_sha(repo)

    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Commit before done",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_required=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")

        assert kb.complete_task(
            conn,
            tid,
            summary="Implemented feature",
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, tid)
        run = kb.get_run(conn, run_id)

    assert task is not None and task.status == "done"
    assert run is not None and run.metadata is not None
    intent = run.metadata["source_completion_intent"]
    receipt = run.metadata["source_completion_receipt"]
    assert intent["run_id"] == run_id
    assert receipt["intent_id"] == intent["intent_id"]
    assert receipt["run_id"] == run_id
    assert receipt["base_sha"] == base_sha
    assert receipt["commit_sha"] == _head_sha(repo)
    assert receipt["commit_sha"] != base_sha
    assert len(receipt["tree_sha"]) == 40
    assert len(receipt["diff_digest"]) == 64
    assert receipt["paths"] == ["feature.py"]
    assert receipt["created_at"] >= intent["created_at"]
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def test_default_board_forbidden_dependency_chain_forwards_candidate_sha(
    kanban_home, tmp_path
):
    board = "default"
    repo = tmp_path / "default-source-three-card-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        developer_id = kb.create_task(conn, title="Developer", board=board,
            assignee="developer", workspace_kind="worktree", workspace_path=str(repo),
            source_commit_required=True)
        developer = kb.get_task(conn, developer_id)
        assert developer is not None
        developer_ws, _ = kb._resolve_worktree_workspace(developer, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(developer_ws), developer_id))
        (developer_ws / "feature.txt").write_text("candidate\n", encoding="utf-8")
        claimed = kb.claim_task(conn, developer_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(conn, developer_id, expected_run_id=claimed.current_run_id)
        developer_sha = _head_sha(developer_ws)
        tester_id = kb.create_task(conn, title="Tester", board=board, assignee="tester",
            parents=[developer_id], workspace_kind="worktree", workspace_path=str(repo),
            source_commit_forbidden=True)
        tester = kb.get_task(conn, tester_id)
        assert tester is not None and tester.status == "ready"
        tester_ws, _ = kb._resolve_worktree_workspace(tester, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(tester_ws), tester_id))
        assert _head_sha(tester_ws) == developer_sha
        tester_claimed = kb.claim_task(conn, tester_id)
        assert tester_claimed is not None and tester_claimed.current_run_id is not None
        assert kb.complete_task(conn, tester_id, metadata={"candidate_sha": "caller-value"},
            expected_run_id=tester_claimed.current_run_id)
        tester_run = kb.get_run(conn, tester_claimed.current_run_id)
        assert tester_run is not None and tester_run.metadata["candidate_sha"] == developer_sha
        reviewer_id = kb.create_task(conn, title="Reviewer", board=board, assignee="reviewer",
            parents=[tester_id], workspace_kind="worktree", workspace_path=str(repo),
            source_commit_forbidden=True)
        reviewer = kb.get_task(conn, reviewer_id)
        assert reviewer is not None and reviewer.status == "ready"
        reviewer_ws, _ = kb._resolve_worktree_workspace(reviewer, board=board, conn=conn)
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(reviewer_ws), reviewer_id))
        assert _head_sha(reviewer_ws) == developer_sha
        assert (reviewer_ws / "feature.txt").read_text(encoding="utf-8") == "candidate\n"


def test_complete_task_adopts_the_one_exact_commit_after_crash_before_receipt(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "adoption-repo"
    _init_git_repo(repo)

    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Adopt exact commit",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_required=True,
        )
        first = kb.claim_task(conn, tid)
        assert first is not None and first.current_run_id is not None
        first_run_id = first.current_run_id
        (repo / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        original_persist = kb._persist_source_completion_metadata
        persist_calls = 0

        def crash_before_receipt(*args, **kwargs):
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 2:
                raise RuntimeError("simulated crash after git commit")
            return original_persist(*args, **kwargs)

        monkeypatch.setattr(kb, "_persist_source_completion_metadata", crash_before_receipt)
        with pytest.raises(RuntimeError, match="simulated crash"):
            kb.complete_task(conn, tid, expected_run_id=first_run_id)
        committed_sha = _head_sha(repo)
        assert kb.get_task(conn, tid).status == "running"

        monkeypatch.setattr(kb, "_persist_source_completion_metadata", original_persist)
        assert kb.reclaim_task(conn, tid, reason="retry source finalization")
        second = kb.claim_task(conn, tid)
        assert second is not None and second.current_run_id is not None
        second_run_id = second.current_run_id

        assert kb.complete_task(conn, tid, expected_run_id=second_run_id)
        receipt = kb.get_run(conn, second_run_id).metadata["source_completion_receipt"]

    assert receipt["adopted"] is True
    assert receipt["commit_sha"] == committed_sha == _head_sha(repo)
    assert receipt["intent_run_id"] == first_run_id
    assert receipt["run_id"] == second_run_id
    log_count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log_count == "2"


def test_complete_task_forbidden_source_does_not_commit_worker_diff(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "forbidden-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Evidence only",
            assignee="reviewer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        monkeypatch.setattr(
            kb,
            "_commit_worker_diff",
            lambda *args, **kwargs: pytest.fail("forbidden completion authored source"),
        )

        assert kb.complete_task(
            conn, tid, expected_run_id=claimed.current_run_id
        )

        task = kb.get_task(conn, tid)

    assert task is not None and task.status == "done"
    assert task.source_commit_forbidden is True
    assert _head_sha(repo) == subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_complete_task_forbidden_source_rejects_dirty_git_before_terminal_mutation(
    kanban_home, tmp_path
):
    repo = tmp_path / "dirty-forbidden-repo"
    _init_git_repo(repo)
    before_sha = _head_sha(repo)
    with kanban_db_connect.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="Evidence-only parent",
            assignee="reviewer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_forbidden=True,
        )
        child_id = kb.create_task(conn, title="Dependent")
        kb.link_tasks(conn, parent_id, child_id)
        claimed = kb.claim_task(conn, parent_id)
        assert claimed is not None and claimed.current_run_id is not None
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("diagnosis\n", encoding="utf-8")

        with pytest.raises(kb._SourceCommitError) as raised:
            kb.complete_task(conn, parent_id, expected_run_id=claimed.current_run_id)

        parent = kb.get_task(conn, parent_id)
        child = kb.get_task(conn, child_id)
        run = kb.get_run(conn, claimed.current_run_id)

    assert raised.value.code == "source_forbidden_dirty"
    assert parent is not None and parent.status == "running"
    assert child is not None and child.status == "todo"
    assert run is not None and run.ended_at is None
    assert _head_sha(repo) == before_sha
    assert (repo / "README.md").read_text(encoding="utf-8") == "dirty\n"
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "diagnosis\n"


def test_complete_task_forbidden_source_allows_non_git_report_only_workspace(
    kanban_home, tmp_path
):
    workspace = tmp_path / "report-only"
    workspace.mkdir()
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Report-only evidence",
            assignee="reviewer",
            workspace_kind="dir",
            workspace_path=str(workspace),
            source_commit_forbidden=True,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None

        assert kb.complete_task(conn, task_id, expected_run_id=claimed.current_run_id)

        task = kb.get_task(conn, task_id)

    assert task is not None and task.status == "done"


def test_complete_task_required_source_raises_typed_failure_without_commit(
    kanban_home, tmp_path
):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Missing source",
            assignee="developer",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            source_commit_required=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None

        with pytest.raises(kb._SourceCommitError) as raised:
            kb.complete_task(conn, tid, expected_run_id=claimed.current_run_id)

        task = kb.get_task(conn, tid)

    assert raised.value.code == "not_a_git_repository"
    assert task is not None and task.status == "running"


def test_complete_task_required_source_rechecks_run_ownership_before_done(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "cas-repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn,
            title="CAS before done",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            source_commit_required=True,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        (repo / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
        original_persist = kb._persist_source_completion_metadata

        def lose_ownership_after_receipt(*args, **kwargs):
            original_persist(*args, **kwargs)
            if kwargs.get("receipt") is not None:
                conn.execute(
                    "UPDATE tasks SET current_run_id = NULL WHERE id = ?",
                    (tid,),
                )
                conn.commit()

        monkeypatch.setattr(
            kb, "_persist_source_completion_metadata", lose_ownership_after_receipt
        )

        with pytest.raises(kb._SourceCommitError) as raised:
            kb.complete_task(conn, tid, expected_run_id=run_id)

        task = kb.get_task(conn, tid)

    assert raised.value.code == "run_changed"
    assert task is not None and task.status == "running"
    assert _head_sha(repo) != subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_commit_worker_diff_dirty_worktree_returns_sha_and_cleans_tree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="worktree", workspace_path=str(repo)
        )
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")
        sha = kb._commit_worker_diff(conn, tid)

    assert sha is not None
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""
    assert _head_sha(repo) == sha


def test_commit_worker_diff_nothing_to_commit_returns_none(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="worktree", workspace_path=str(repo)
        )
        before = _head_sha(repo)
        result = kb._commit_worker_diff(conn, tid)

    assert result is None
    assert _head_sha(repo) == before


def test_commit_worker_diff_no_repo_returns_none(kanban_home, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="dir", workspace_path=str(not_a_repo)
        )
        result = kb._commit_worker_diff(conn, tid)

    assert result is None


def test_commit_worker_diff_missing_workspace_path_returns_none(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="no workspace")
        result = kb._commit_worker_diff(conn, tid)

    assert result is None


def test_commit_worker_diff_respects_gitignore(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add gitignore"], check=True, capture_output=True, text=True)

    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(
            conn, title="ship it", workspace_kind="worktree", workspace_path=str(repo)
        )
        state_dir = repo / "state"
        state_dir.mkdir()
        (state_dir / "runtime.json").write_text("{}\n", encoding="utf-8")
        (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
        sha = kb._commit_worker_diff(conn, tid)

    assert sha is not None
    show = subprocess.run(
        ["git", "-C", str(repo), "show", "--stat", "--name-only", sha],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "feature.py" in show
    assert "state/runtime.json" not in show
    ls_files = subprocess.run(
        ["git", "-C", str(repo), "ls-files"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "state/runtime.json" not in ls_files


def test_handoff_commit_first_gate_blocks_advance_on_clean_tree(kanban_home, tmp_path, monkeypatch):
    """T2.2: no committed diff (clean tree) -> False, card untouched, no event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-gate"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        before = _card_snapshot(conn, tid)

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert after == before
    assert after["current_step_key"] == "development"
    assert not any(event.kind == "handoff" for event in events)


def test_handoff_happy_path_commits_advances_and_emits_one_event(kanban_home, tmp_path, monkeypatch):
    """T2.3: dirty worktree + provenance -> commits, advances, retags, syncs status, one event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-happy"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET running = 1 WHERE id = ?", (tid,))
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)
        meta = kb.read_board_metadata(board)

    assert result is True
    assert card["current_step_key"] == "test"
    assert card["assignee"] == "tester"
    assert card["running"] == 0
    assert card["result"] == "Implemented checkout"
    assert card["status"] == kb._legacy_status(card, meta)

    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    payload = handoff_events[0].payload
    assert payload["from_step"] == "development"
    assert payload["to_step"] == "test"
    assert payload["assignee"] == "tester"
    assert payload["summary"] == "Implemented checkout"
    sha = payload["sha"]
    assert sha and len(sha) == 40

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == sha
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


def test_handoff_terminal_review_advances_with_no_next_assignee(kanban_home, tmp_path, monkeypatch):
    """T2.4a: review -> release_measure advances with assignee None, one event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-review"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        (repo / "notes.md").write_text("looks good\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board,
            metadata={
                "ai_provenance": {
                    "writer": {"agent": "hermes"},
                    "reviewer": {"agent": "codex"},
                }
            },
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is True
    assert card["current_step_key"] == "release_measure"
    assert card["assignee"] is None
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["to_step"] == "release_measure"
    assert handoff_events[0].payload["assignee"] is None


def test_handoff_terminal_release_measure_does_not_auto_advance(kanban_home, tmp_path, monkeypatch):
    """T2.4b: release_measure has no transition -> False, nothing committed, no event."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-terminal"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="release_measure",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        # Dirty tree present: if handoff mistakenly committed first, this
        # would prove the bug (HEAD would move even without a transition).
        (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        before_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        before = _card_snapshot(conn, tid)

        result = kb.handoff(conn, tid, board=board)

        after = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert after == before
    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert after_head == before_head
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() != ""  # still dirty -- never staged/committed
    assert not any(event.kind == "handoff" for event in events)


def test_handoff_non_v2_board_is_noop(kanban_home, monkeypatch):
    """Non-v2 (legacy) boards never use handoff() -- False, no mutation (T2.5 guarantee)."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-no-v2-handoff"
    kb.create_board(board, name="Product No V2", preset="product")
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="development",
        )
        before = _card_snapshot(conn, tid)

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after = _card_snapshot(conn, tid)

    assert result is False
    assert after == before


def test_handoff_noop_then_legacy_complete_task_advances_card(kanban_home, monkeypatch):
    """Coexistence guard (T2.5): on a non-v2 product board, ``handoff()``
    no-ops (returns ``False``, mutates nothing, emits no ``handoff`` event)
    and legacy ``complete_task`` still advances the same card exactly as
    ``test_product_completion_advances_card_to_next_role`` asserts.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "product-legacy-coexist"
    kb.create_board(board, name="Product Legacy Coexist", preset="product")
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="architect-profile",
            workflow_template_id="product",
            current_step_key="architecture",
        )
        before = _card_snapshot(conn, tid)

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after_handoff = _card_snapshot(conn, tid)
        events_after_handoff = kb.list_events(conn, tid)

        assert result is False
        assert after_handoff == before
        assert not any(event.kind == "handoff" for event in events_after_handoff)

        # Legacy completion must still advance the card exactly as before.
        assert kb.complete_task(
            conn,
            tid,
            summary="Architecture settled",
            board=board,
            product_role_assignees={"developer": "developer-profile"},
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest_run = kb.latest_run(conn, tid)

    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "developer-profile"
    assert latest_run.outcome == "advanced"
    advanced = [event for event in events if event.kind == "workflow_advanced"]
    assert advanced
    assert advanced[-1].payload["from_step"] == "architecture"
    assert advanced[-1].payload["to_step"] == "development"


def test_handoff_provenance_failure_raises_and_leaves_card_untouched(kanban_home, tmp_path, monkeypatch):
    """Provenance gate runs before commit-first: raises, nothing committed or mutated."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-provenance"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")
        before = _card_snapshot(conn, tid)

        with pytest.raises(kb.ProductProvenanceError, match="Development completion"):
            kb.handoff(conn, tid, board=board)

        after = _card_snapshot(conn, tid)

    assert after == before
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() != ""  # never staged/committed


# ---------------------------------------------------------------------------
# complete_task -> handoff() routing on handoff_v2 boards (W1)
# ---------------------------------------------------------------------------

def test_complete_task_v2_non_terminal_routes_to_commit_first_handoff(kanban_home, tmp_path, monkeypatch):
    """A real v2 worker's non-terminal completion routes through ``handoff()``:
    dirty worktree -> committed, card advances, exactly one ``handoff`` event,
    ``running`` cleared, and the run is ended cleanly (no dangling open run).
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-happy"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id_before = claimed.current_run_id
        assert run_id_before is not None
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        closed_run = kb.get_run(conn, run_id_before)

    assert result is True
    assert card["current_step_key"] == "test"
    assert card["assignee"] == "tester"
    assert card["running"] == 0

    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["from_step"] == "development"
    assert handoff_events[0].payload["to_step"] == "test"
    sha = handoff_events[0].payload["sha"]
    assert sha and len(sha) == 40
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == sha

    # Run bookkeeping: no dangling open run.
    assert task.current_run_id is None
    assert closed_run is not None
    assert closed_run.ended_at is not None
    assert closed_run.outcome == "advanced"
    assert closed_run.status == "completed"


def test_complete_task_v2_no_diff_does_not_complete(kanban_home, tmp_path, monkeypatch):
    """A v2 completion with a clean worktree (no committed diff) returns
    False and does NOT complete or advance the card -- the commit-first
    gate reaches real workers via ``complete_task``.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-no-diff"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        before = _card_snapshot(conn, tid)

        result = kb.complete_task(
            conn,
            tid,
            summary="Nothing changed",
            board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        after = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert after == before
    assert after["current_step_key"] == "development"
    assert task.status != "done"
    assert not any(event.kind == "handoff" for event in events)
    assert not any(event.kind == "workflow_advanced" for event in events)


def test_complete_task_v2_clean_test_evidence_advances_without_commit(kanban_home, tmp_path, monkeypatch):
    """A test-step handoff records evidence and advances even when the
    worktree is clean: testers verify the existing development commit and
    usually have no source diff of their own to commit.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-clean-test"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    before_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="tester",
            workflow_template_id="product",
            current_step_key="test",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )

        result = kb.complete_task(
            conn,
            tid,
            summary="Tests passed",
            board=board,
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {"tester": {"agent": "hermes", "result": "passed"}},
            },
        )

        card = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert result is True
    assert card["current_step_key"] == "review"
    assert card["assignee"] == "reviewer"
    assert card["running"] == 0
    assert task.status == "review"
    assert before_head == after_head
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["from_step"] == "test"
    assert handoff_events[0].payload["to_step"] == "review"
    assert handoff_events[0].payload["sha"] is None


def test_standalone_release_measure_review_evidence_advances_without_commit(
    kanban_home, tmp_path, monkeypatch
):
    """A review-step handoff can be evidence-only: independent review should
    move the card to Release / Measure without requiring a new code commit.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-clean-review"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    before_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )

        result = kb.complete_task(
            conn,
            tid,
            summary="Independent review passed",
            board=board,
            metadata={
                "workflow_outcome": {"verdict": "approved"},
                "ai_provenance": {
                    "writer": {"agent": "claude-code"},
                    "reviewer": {"agent": "codex"},
                }
            },
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    assert result is True
    assert card["current_step_key"] == "release_measure"
    assert card["assignee"] is None
    assert before_head == after_head
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1
    assert handoff_events[0].payload["from_step"] == "review"
    assert handoff_events[0].payload["to_step"] == "release_measure"
    assert handoff_events[0].payload["sha"] is None


def test_complete_task_v2_with_unresolved_preflight_resumes_instead_of_handoff(
    kanban_home, tmp_path, monkeypatch,
):
    """A v2 card with an unresolved product preflight (the T3.3 obstacle chain
    routed it to the ``default`` resolver via ``block_task``) must RESUME
    to its original assignee/step when that
    resolver's turn ends via ``complete_task`` -- NOT be treated as real work
    and routed through the commit-first ``handoff()``, even though a diff is
    sitting uncommitted in the worktree.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-complete-task-preflight"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer-profile",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        assert kb.block_task(
            conn,
            tid,
            reason="Need API credentials",
            kind="needs_input",
            attempted_resolutions=["checked env"],
            board=board,
            human_escalation_assignee="default",
        )
        blocked_card = _card_snapshot(conn, tid)
        assert blocked_card["assignee"] == "default"
        assert blocked_card["current_step_key"] == "development"
        resolver_run = kb.claim_task(conn, tid)
        assert resolver_run is not None and resolver_run.current_run_id is not None

        # A stray uncommitted diff is present in the worktree -- if the
        # buggy v2 branch fired here, it would wrongly commit it and
        # advance the card, mistaking obstacle-resolution for real work.
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = _resolve_preflight(
            conn,
            tid,
            resolver_run.current_run_id,
            board,
            reason="Found internal test token path",
        )

        card = _card_snapshot(conn, tid)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        head = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    assert result is True
    assert card["current_step_key"] == "development"
    assert card["assignee"] == "developer-profile"
    assert task.status == "ready"
    assert not any(event.kind == "handoff" for event in events)
    assert [event.kind for event in events].count("human_input_preflight_resolved") == 1
    # The legacy resume path never touches git -- the stray diff is still
    # sitting there uncommitted (it was NOT swept into a handoff commit).
    assert head != ""


def test_complete_task_v2_terminal_release_measure_requires_release_evidence(kanban_home):
    board = "v2-complete-task-terminal"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="release_measure",
        )

        with pytest.raises(kb.ReleaseEvidenceError):
            kb.complete_task(
                conn,
                tid,
                summary="Released and measured",
                board=board,
            )

        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "ready"
    assert not any(event.kind == "handoff" for event in events)


def test_complete_task_legacy_board_unchanged(kanban_home):
    """Non-v2 product boards keep using the legacy advance path unchanged
    (mirrors ``test_product_completion_advances_card_to_next_role``).
    """
    kb.create_board("prod-w1-legacy", preset="product")
    with kanban_db_connect.connect(board="prod-w1-legacy") as conn:
        tid = kb.create_task(
            conn,
            title="User story: checkout",
            assignee="architect-profile",
            workflow_template_id="product",
            current_step_key="architecture",
        )
        assert kb.complete_task(
            conn,
            tid,
            summary="Architecture settled",
            board="prod-w1-legacy",
            product_role_assignees={"developer": "developer-profile"},
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        latest_run = kb.latest_run(conn, tid)
    assert task.status == "ready"
    assert task.current_step_key == "development"
    assert task.assignee == "developer-profile"
    assert latest_run.outcome == "advanced"
    advanced = [event for event in events if event.kind == "workflow_advanced"]
    assert advanced
    assert advanced[-1].payload["from_step"] == "architecture"
    assert advanced[-1].payload["to_step"] == "development"
    assert not any(event.kind == "handoff" for event in events)


# ---------------------------------------------------------------------------
# handoff() honors expected_run_id — stale reclaimed worker cannot advance
# (Codex P1: complete_task's v2 routing used to call handoff() without the
# worker's run id, so a RECLAIMED worker could still commit + advance.)
# ---------------------------------------------------------------------------

def test_complete_task_v2_stale_reclaimed_worker_cannot_advance(kanban_home, tmp_path, monkeypatch):
    """Real path: claim a v2 card, write a dirty diff, RECLAIM the claim
    (operator-driven, same as the dashboard recovery flow), then the OLD
    run's worker calls ``complete_task(..., expected_run_id=<old run id>)``.

    Must return False: no advance (current_step_key unchanged), no commit
    (HEAD unmoved, worktree still dirty), and no ``handoff`` event -- the
    ownership was revoked out from under it.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-stale-reclaim"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        stale_run_id = claimed.current_run_id
        assert stale_run_id is not None
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        before_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Operator (or crash detector) reclaims the claim -- ownership is
        # revoked, current_run_id cleared, card back to ready.
        assert kb.reclaim_task(conn, tid, reason="test reclaim") is True
        reclaimed_card = kb.get_task(conn, tid)
        assert reclaimed_card.current_run_id is None
        assert reclaimed_card.status == "ready"

        # The stale worker (still holding the OLD run id) tries to complete.
        result = kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            board=board,
            expected_run_id=stale_run_id,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert card["current_step_key"] == "development"
    assert not any(event.kind == "handoff" for event in events)
    after_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert after_head == before_head
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() != ""  # still dirty -- never staged/committed


def test_end_run_expected_id_cannot_close_new_owner(kanban_home):
    with kanban_db_connect.connect() as conn:
        tid = kb.create_task(conn, title="owned run", assignee="developer")
        first = kb.claim_task(conn, tid, claimer="old")
        assert first is not None and first.current_run_id is not None
        old_run_id = first.current_run_id
        assert kb.reclaim_task(conn, tid, reason="new owner") is True
        second = kb.claim_task(conn, tid, claimer="new")
        assert second is not None and second.current_run_id is not None
        new_run_id = second.current_run_id

        with kb.write_txn(conn):
            ended = kb._end_run(
                conn,
                tid,
                outcome="advanced",
                expected_run_id=old_run_id,
            )
        task = kb.get_task(conn, tid)
        old_run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id=?", (old_run_id,)
        ).fetchone()
        new_run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id=?", (new_run_id,)
        ).fetchone()

    assert ended is None
    assert task is not None and task.current_run_id == new_run_id
    assert old_run["ended_at"] is not None and old_run["outcome"] == "reclaimed"
    assert new_run["ended_at"] is None and new_run["outcome"] is None

def test_complete_task_v2_owning_worker_still_advances_with_expected_run_id(
    kanban_home, tmp_path, monkeypatch,
):
    """The current run's worker (expected_run_id == current_run_id, status
    running) must still commit + advance exactly as before -- passing
    expected_run_id must not regress the happy path.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-owning-worker"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.complete_task(
            conn,
            tid,
            summary="Implemented checkout",
            board=board,
            expected_run_id=run_id,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is True
    assert card["current_step_key"] == "test"
    assert card["assignee"] == "tester"
    handoff_events = [event for event in events if event.kind == "handoff"]
    assert len(handoff_events) == 1


def test_handoff_cas_race_loses_ownership_between_commit_and_advance(
    kanban_home, tmp_path, monkeypatch,
):
    """CAS on the advance UPDATE: even when the precheck passed, if
    ownership changes in the gap between the commit-first gate and the
    final advance (a competing reclaim races in), the advance must refuse
    (rowcount != 1) and emit no event. The diff is already committed at
    the git level by this point (can't be undone), but the DB/card must
    not advance nor record a handoff.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-handoff-cas-race"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
        kb.set_running(conn, tid, True, board=board)
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        real_commit = kb._commit_worker_diff

        def _racing_commit(conn_, task_id_, *args, **kwargs):
            sha = real_commit(conn_, task_id_, *args, **kwargs)
            # Simulate a competing reclaim landing in the window between
            # the commit-first gate and the advance UPDATE below.
            conn_.execute(
                "UPDATE tasks SET current_run_id = NULL, status = 'ready' "
                "WHERE id = ?",
                (task_id_,),
            )
            return sha

        monkeypatch.setattr(kb, "_commit_worker_diff", _racing_commit)

        result = kb.handoff(
            conn, tid, board=board, expected_run_id=run_id,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )

        card = _card_snapshot(conn, tid)
        events = kb.list_events(conn, tid)

    assert result is False
    assert card["current_step_key"] == "development"
    assert not any(event.kind == "handoff" for event in events)

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""  # the commit itself DID happen -- git can't undo it


# ---------------------------------------------------------------------------
# spawn_after_handoff — event-driven fire-once spawn consumer (T3.1)
# ---------------------------------------------------------------------------

def test_spawn_after_handoff_fire_once_spawns_the_handed_off_card(kanban_home, tmp_path, monkeypatch):
    """One handoff -> spawn_after_handoff spawns the next-role agent exactly once."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-fire-once"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET running = 1 WHERE id = ?", (tid,))
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board, summary="Implemented checkout",
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        assert result is True

        spawned_ids = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)

        task = kb.get_task(conn, tid)

    assert spawned_ids == [tid]
    assert len(spawns) == 1
    assert spawns[0][0] == tid
    assert task is not None
    assert task.status == "running"
    assert task.worker_pid == 4242
    assert task.assignee == "tester"


def test_spawn_after_handoff_second_pass_spawns_nothing(kanban_home, tmp_path, monkeypatch):
    """Regression guard: a second pass over an already-claimed card is a no-op (claim-CAS)."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-no-respawn"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute("UPDATE tasks SET running = 1 WHERE id = ?", (tid,))
        (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board,
            metadata={"ai_provenance": {"writer": {"agent": "hermes"}}},
        )
        assert result is True

        first = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)
        second = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)

    assert first == [tid]
    assert second == []
    assert len(spawns) == 1  # spawn count stays <= 1 across both passes


def test_spawn_after_handoff_terminal_review_handoff_spawns_nothing(kanban_home, tmp_path, monkeypatch):
    """A review -> release_measure handoff leaves assignee=NULL: not a candidate, no spawn."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-spawn-terminal"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        (repo / "notes.md").write_text("looks good\n", encoding="utf-8")

        result = kb.handoff(
            conn, tid, board=board,
            metadata={
                "ai_provenance": {
                    "writer": {"agent": "hermes"},
                    "reviewer": {"agent": "codex"},
                }
            },
        )
        assert result is True

        card = _card_snapshot(conn, tid)
        assert card["assignee"] is None

        spawned_ids = kb.spawn_after_handoff(conn, board=board, spawn_fn=fake_spawn)

    assert spawned_ids == []
    assert spawns == []


def test_spawn_after_handoff_legacy_board_is_noop(kanban_home, monkeypatch):
    """Non-v2 (legacy) boards never use spawn_after_handoff -- returns [], spawn_fn never called."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    spawns: list[tuple[str, str]] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append((task.id, workspace))
        return 4242

    with kanban_db_connect.connect() as conn:
        kb.create_task(conn, title="legacy card", assignee="developer")

        spawned_ids = kb.spawn_after_handoff(conn, spawn_fn=fake_spawn)

    assert spawned_ids == []
    assert spawns == []


# ---------------------------------------------------------------------------
# reconcile() -- bounded safety-net poller for handoff_v2 boards (T3.2)
# ---------------------------------------------------------------------------

def test_reconcile_recovers_dead_pid_then_spawns_next_pass_bounded(
    kanban_home, tmp_path, monkeypatch,
):
    """Dead-PID running card: pass 1 reclaims only (0 spawns); pass 2 spawns
    only (having become ready+idle). Never more than one action per pass --
    the direct regression guard against the multi-spawn storm."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-dead-pid"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    host = kb._claimer_id().split(":", 1)[0]
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, f"{host}:w0", stale_started_at, tid),
        )
        conn.commit()

        pass1 = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert pass1.reclaimed == [tid]
        assert pass1.spawned == []
        assert spawns == []  # zero spawns this pass -- the anti-storm guard

        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.claim_lock is None
        assert card.worker_pid is None

        pass2 = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert pass2.reclaimed == []
        assert pass2.spawned == [tid]
        assert spawns == [tid]  # exactly one spawn total, on pass 2

        card = kb.get_task(conn, tid)
        assert card.status == "running"


def test_reconcile_no_thrash_on_healthy_running_card(kanban_home, tmp_path, monkeypatch):
    """A running card with a LIVE pid gets no action -- repeatedly."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-healthy"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    host = kb._claimer_id().split(":", 1)[0]
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (os.getpid(), f"{host}:w0", stale_started_at, tid),
        )
        conn.commit()

        first = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        second = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)

    assert first.reclaimed == [] and first.spawned == []
    assert second.reclaimed == [] and second.spawned == []
    assert spawns == []


def test_reconcile_honors_crash_grace_period(kanban_home, tmp_path, monkeypatch):
    """A dead-reading PID whose worker just started (within the crash grace
    window) must NOT be reclaimed -- mirrors detect_crashed_workers' grace
    logic (#T3.2 review finding). Without this, reconcile's poll cadence can
    misclassify a freshly-spawned healthy worker as dead before its PID is
    visible on /proc, reintroducing the exact respawn churn reconcile exists
    to prevent."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-grace"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.delenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", raising=False)
    host = kb._claimer_id().split(":", 1)[0]

    now = 5_000_000.0
    monkeypatch.setattr(kb.time, "time", lambda: now)

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, f"{host}:w0", int(now), tid),
        )
        conn.commit()

        # Just started (started_at == now): inside the grace window, so no
        # reclaim despite the dead-reading pid.
        within_grace = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert within_grace.reclaimed == []
        assert within_grace.spawned == []
        assert spawns == []
        card = kb.get_task(conn, tid)
        assert card.status == "running"

        # Past the default 30s grace window: now reclaim proceeds as before.
        monkeypatch.setattr(kb.time, "time", lambda: now + 60)
        past_grace = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        assert past_grace.reclaimed == [tid]
        assert past_grace.spawned == []
        card = kb.get_task(conn, tid)
        assert card.status == "ready"


def test_reconcile_skips_liveness_check_for_other_host_claim(
    kanban_home, tmp_path, monkeypatch,
):
    """A running card claimed by a different host is never reclaimed --
    ``_pid_alive`` checks the LOCAL process table, so a remote host's pid is
    meaningless (mirrors detect_crashed_workers' host-ownership guard)."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-other-host"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, "some-other-host:w0", stale_started_at, tid),
        )
        conn.commit()

        result = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)

    assert result.reclaimed == []
    assert result.spawned == []
    assert spawns == []


def test_reconcile_spawns_stranded_ready_card_idempotently(kanban_home, tmp_path, monkeypatch):
    """A ready+idle+spawnable card gets spawned once; a second pass (now
    running) is a no-op via the claim CAS."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "v2-reconcile-stranded-ready"
    _v2_product_board(board)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return os.getpid()  # a real, live pid so the second pass's own
        # dead-worker-recovery step doesn't reclaim it out from under us

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="Story",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        assert kb.get_task(conn, tid).status == "ready"

        first = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)
        second = kb.reconcile(conn, board=board, spawn_fn=fake_spawn)

    assert first.reclaimed == []
    assert first.spawned == [tid]
    assert second.reclaimed == []
    assert second.spawned == []
    assert spawns == [tid]  # spawn count stays <= 1 across both passes




def test_reconcile_legacy_board_is_noop(kanban_home, monkeypatch):
    """Non-v2 (legacy) boards never use reconcile -- empty result, spawn_fn never called."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kanban_db_connect.connect() as conn:
        kb.create_task(conn, title="legacy card", assignee="developer")

        result = kb.reconcile(conn, spawn_fn=fake_spawn)

    assert result.reclaimed == []
    assert result.spawned == []
    assert spawns == []


def test_reconcile_spawn_ready_false_recovers_but_skips_spawn(
    kanban_home, tmp_path, monkeypatch,
):
    """``spawn_ready=False`` skips ONLY step 2 (the stranded-ready spawn
    loop) while step 1 (dead-worker recovery) still runs. The gateway tick uses
    (Codex re-review P1): dispatch_once is already the tick's sole capped
    spawn owner, so reconcile in the tick must recover but not duplicate spawn,
    never spawn an arbitrary ready card."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-reconcile-spawn-ready-false"
    _v2_product_board_with_repo(board, repo)

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    host = kb._claimer_id().split(":", 1)[0]
    stale_started_at = int(time.time()) - 3600  # past the crash grace window

    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kanban_db_connect.connect(board=board) as conn:
        dead_tid = kb.create_task(conn, title="Dead worker story", assignee="developer")
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "started_at=? WHERE id=?",
            (99999, f"{host}:w0", stale_started_at, dead_tid),
        )
        conn.commit()
        ready_tid = kb.create_task(conn, title="Stranded ready story", assignee="developer")

        result = kb.reconcile(conn, board=board, spawn_fn=fake_spawn, spawn_ready=False)

        dead_card = kb.get_task(conn, dead_tid)
        ready_card = kb.get_task(conn, ready_tid)

    assert result.reclaimed == [dead_tid], "recovery (step 1) still runs"
    assert result.spawned == [], "the ready-spawn step (step 2) is skipped"
    assert spawns == []
    assert result.integrated == []

    assert dead_card.status == "ready", "dead-pid card was still re-idled"
    assert ready_card.status == "ready", "stranded ready card was NOT spawned"


# ---------------------------------------------------------------------------
# Scratch cleanup containment (#28818)
# ---------------------------------------------------------------------------



def test_complete_task_persists_scratch_artifacts_before_cleanup(kanban_home):
    """Completion artifacts from scratch workspaces survive workspace cleanup."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="render chart")
        task = kb.get_task(conn, t)
        ws = kbw.resolve_workspace(task)
        kbw.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"png-bytes")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert persisted.exists(), "artifact copy should survive scratch cleanup"
    assert persisted.parent == kb.task_attachments_dir(t)
    assert persisted.name == "chart.png"
    assert persisted.read_bytes() == b"png-bytes"
    assert str(persisted) != str(artifact)
    assert run is not None
    assert run.metadata["artifacts"] == [str(persisted)]
    with kbc.connect() as conn:
        attachments = kb.list_attachments(conn, t)
    assert [(a.filename, a.stored_path) for a in attachments] == [
        ("chart.png", str(persisted.resolve()))
    ]

