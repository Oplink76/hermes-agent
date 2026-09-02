"""Real-repository acceptance coverage for dispatcher-owned story refresh."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "refresh@example.com")
    _git(repo, "config", "user.name", "Refresh Fixture")
    (repo / "README.md").write_text("refresh fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "fixture: initial")
    return repo


def _board(board: str, repo: Path) -> None:
    kb.ensure_product_board_defaults(
        board,
        name="Story Refresh Fixture",
        default_workdir=str(repo),
    )
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["product_workflow"]["handoff_v2"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def _story_card(board: str, repo: Path, step: str = "architecture") -> tuple[str, str, Path, str]:
    epic_branch: str
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(
            conn,
            title="Epic: refresh fixture",
            board=board,
            work_item_kind="epic",
        )
        epic_branch = kb.epic_branch_for(epic_id)
        story_branch = "story/refresh-fixture"
        story_worktree = repo / ".worktrees" / "refresh-fixture"
        _git(repo, "branch", epic_branch)
        _git(repo, "worktree", "add", "-b", story_branch, str(story_worktree), "main")
        story_id = kb.create_task(
            conn,
            title="Story: refresh fixture",
            assignee="developer",
            board=board,
            workspace_kind="worktree",
            workspace_path=str(story_worktree),
            branch_name=story_branch,
            workflow_template_id="product",
            current_step_key=step,
        )
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    return epic_id, story_id, story_worktree, epic_branch


def _story_fixture(
    tmp_path: Path,
    board: str,
    *,
    step: str = "development",
    advance_epic: bool = False,
) -> tuple[Path, str, Path, str]:
    repo = _repository(tmp_path)
    _board(board, repo)
    _epic_id, story_id, story_worktree, epic_branch = _story_card(
        board, repo, step=step
    )
    if advance_epic:
        _git(repo, "checkout", epic_branch)
        _commit(repo, "epic.txt", "from epic\n", "fixture: advance epic")
        _git(repo, "checkout", "main")
    return repo, story_id, story_worktree, epic_branch


def _route_story_to_resolver(conn, story_id: str, board: str) -> kb.Task:
    ordinary = kb.claim_task(conn, story_id)
    assert ordinary is not None and ordinary.current_run_id is not None
    assert kb.block_task(
        conn,
        story_id,
        reason="The governed worker needs recovery",
        kind="needs_input",
        attempted_resolutions=["inspected the dirty worktree"],
        expected_run_id=ordinary.current_run_id,
        board=board,
        human_escalation_assignee="resolver",
    )
    resolver = kb.claim_task(conn, story_id)
    assert resolver is not None and resolver.current_run_id is not None
    return resolver


def _resolve_story_preflight(
    conn,
    story_id: str,
    board: str,
    *,
    decision: str = "resume",
    repair: dict | None = None,
) -> None:
    resolver = _route_story_to_resolver(conn, story_id, board)
    expected = kb.resolver_expected_snapshot(conn, story_id)
    assert expected is not None
    request = {
        "decision": decision,
        "fault_domain": "task_state" if decision != "escalate" else "framework",
        "diagnosis": "The preserved worktree is understood and recoverable",
        "reason": "Resolve the governed preflight",
        "expected": expected,
    }
    if repair is not None:
        request["repair"] = repair
    assert kb.resolve_product_preflight(
        conn,
        story_id,
        board=board,
        request=request,
        resolver_profile="resolver",
        resolver_model="test-model",
    )


def _stamp_executor_event(conn, task: kb.Task) -> dict[str, str | int]:
    assert task.current_run_id is not None
    identity = {
        "profile": task.assignee or "",
        "provider": "openai-codex",
        "model": "test-model",
        "effort": "medium",
        "surface": "hermes-primary",
        "source": "dispatcher",
        "version": 1,
    }
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            task.id,
            "executor_stamped",
            identity,
            run_id=task.current_run_id,
        )
    return identity


_REFRESH_EVENT_KINDS = {
    "story_refresh_checked",
    "story_refreshed",
    "story_refresh_conflict",
    "story_refresh_attention_required",
}


def test_dispatch_refreshes_clean_story_before_claiming(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = "story-refresh-clean"
    repo = _repository(tmp_path)
    _board(board, repo)
    _epic_id, story_id, story_worktree, epic_branch = _story_card(board, repo)

    _git(repo, "checkout", epic_branch)
    _commit(repo, "epic.txt", "from epic\n", "fixture: advance epic")
    _git(repo, "checkout", "main")
    monkeypatch.setattr(kb, "_stamp_run_executor_identity", lambda *_args, **_kwargs: None)

    with kb.connect(board=board) as conn:
        spawned = kb._spawn_one_v2(
            conn,
            story_id,
            board=board,
            spawn_fn=lambda task, workspace: 4242,
        )
        task = kb.get_task(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert spawned == 4242
    assert task is not None and task.status == "running"
    assert task.workspace_path == str(story_worktree)
    assert (story_worktree / "epic.txt").read_text(encoding="utf-8") == "from epic\n"
    refreshed = next(event for event in events if event.kind == "story_refreshed")
    assert refreshed.payload["authority_invalidated"] is True
    assert refreshed.payload["story_branch"] == "story/refresh-fixture"


def test_dispatch_holds_dirty_story_without_claiming(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = "story-refresh-dirty"
    repo = _repository(tmp_path)
    _board(board, repo)
    _epic_id, story_id, story_worktree, epic_branch = _story_card(board, repo)

    _git(repo, "checkout", epic_branch)
    _commit(repo, "epic.txt", "from epic\n", "fixture: advance epic")
    _git(repo, "checkout", "main")
    (story_worktree / "operator-note.txt").write_text("preserve\n", encoding="utf-8")
    monkeypatch.setattr(kb, "_stamp_run_executor_identity", lambda *_args, **_kwargs: None)

    with kb.connect(board=board) as conn:
        spawned = kb._spawn_one_v2(
            conn,
            story_id,
            board=board,
            spawn_fn=lambda task, workspace: 4242,
        )
        task = kb.get_task(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert spawned is None
    assert task is not None and task.status == "ready"
    assert task.claim_lock is None and task.current_run_id is None
    assert (story_worktree / "operator-note.txt").read_text(encoding="utf-8") == "preserve\n"
    attention = next(
        event for event in events if event.kind == "story_refresh_attention_required"
    )
    assert attention.payload["kind"] == "dirty"
    assert attention.payload["dirty_paths"] == ["operator-note.txt"]


@pytest.mark.parametrize(
    "advance_epic", [False, True], ids=["epic-ancestor", "epic-advanced"]
)
def test_dispatch_prioritizes_recorded_recovery_assignee_before_story_refresh(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    advance_epic: bool,
) -> None:
    board = f"story-refresh-resolver-{advance_epic}"
    _repo, story_id, story_worktree, _branch = _story_fixture(
        tmp_path, board, advance_epic=advance_epic
    )
    spawned: list[tuple[str | None, str]] = []

    def fake_spawn(task: kb.Task, workspace: str) -> int:
        spawned.append((task.assignee, workspace))
        return 4242

    monkeypatch.setattr(
        kb, "_stamp_run_executor_identity", _stamp_executor_event
    )
    with kb.connect(board=board) as conn:
        ordinary = kb.claim_task(conn, story_id)
        assert ordinary is not None and ordinary.current_run_id is not None
        (story_worktree / "worker-change.txt").write_text(
            "preserved implementation\n", encoding="utf-8"
        )
        assert kb.block_task(
            conn,
            story_id,
            reason="The governed worker needs recovery",
            kind="needs_input",
            attempted_resolutions=["ran permitted verification"],
            expected_run_id=ordinary.current_run_id,
            board=board,
            human_escalation_assignee="resolver",
        )
        pid = kb._spawn_one_v2(conn, story_id, board=board, spawn_fn=fake_spawn)
        task = kb.get_task(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert pid == 4242
    assert spawned == [("resolver", str(story_worktree))]
    assert task is not None and task.status == "running" and task.assignee == "resolver"
    assert (story_worktree / "worker-change.txt").read_text(
        encoding="utf-8"
    ) == "preserved implementation\n"
    assert not _REFRESH_EVENT_KINDS.intersection(event.kind for event in events)


@pytest.mark.parametrize(
    "advance_epic", [False, True], ids=["epic-ancestor", "epic-advanced"]
)
def test_dispatch_claims_original_worker_after_fresh_resolver_resume(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    advance_epic: bool,
) -> None:
    board = f"story-refresh-resolver-resume-{advance_epic}"
    _repo, story_id, story_worktree, _branch = _story_fixture(
        tmp_path, board, advance_epic=advance_epic
    )
    (story_worktree / "worker-change.txt").write_text(
        "preserved implementation\n", encoding="utf-8"
    )
    spawned: list[tuple[str | None, str]] = []

    def fake_spawn(task: kb.Task, workspace: str) -> int:
        spawned.append((task.assignee, workspace))
        return 4242

    monkeypatch.setattr(
        kb, "_stamp_run_executor_identity", _stamp_executor_event
    )
    with kb.connect(board=board) as conn:
        _resolve_story_preflight(conn, story_id, board)
        kb.add_comment(conn, story_id, "resolver", "The dirty evidence was reviewed.")
        pid = kb._spawn_one_v2(conn, story_id, board=board, spawn_fn=fake_spawn)
        task = kb.get_task(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert pid == 4242
    assert spawned == [("developer", str(story_worktree))]
    assert task is not None and task.status == "running" and task.assignee == "developer"
    assert (story_worktree / "worker-change.txt").read_text(
        encoding="utf-8"
    ) == "preserved implementation\n"
    resolved_event = [
        event for event in events if event.kind == "human_input_preflight_resolved"
    ][-1]
    assert resolved_event.payload["action"] == "resume"
    assert resolved_event.payload["step_key"] == "development"
    assert not _REFRESH_EVENT_KINDS.intersection(event.kind for event in events)


def test_dispatch_retries_resumed_dirty_story_after_post_claim_spawn_failure(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = "story-refresh-resolver-retry"
    _repo, story_id, story_worktree, _branch = _story_fixture(tmp_path, board)
    (story_worktree / "worker-change.txt").write_text(
        "preserved implementation\n", encoding="utf-8"
    )
    attempts = 0

    def flaky_spawn(task: kb.Task, workspace: str) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("post-claim spawn failed")
        return 4242

    monkeypatch.setattr(
        kb, "_stamp_run_executor_identity", _stamp_executor_event
    )
    with kb.connect(board=board) as conn:
        _resolve_story_preflight(conn, story_id, board)
        first = kb._spawn_one_v2(conn, story_id, board=board, spawn_fn=flaky_spawn)
        after_failure = kb.get_task(conn, story_id)
        second = kb._spawn_one_v2(conn, story_id, board=board, spawn_fn=flaky_spawn)
        after_retry = kb.get_task(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert first is None
    assert second == 4242
    assert attempts == 2
    assert after_failure is not None
    assert after_failure.status == "ready"
    assert after_failure.current_run_id is None
    assert after_failure.claim_lock is None
    assert after_retry is not None
    assert after_retry.status == "running"
    assert after_retry.assignee == "developer"
    assert after_retry.worker_pid == 4242
    assert (story_worktree / "worker-change.txt").read_text(
        encoding="utf-8"
    ) == "preserved implementation\n"
    assert any(event.kind == "spawn_failed" for event in events)
    assert not _REFRESH_EVENT_KINDS.intersection(event.kind for event in events)


@pytest.mark.parametrize(
    "mutation",
    [
        "newer_preflight",
        "lifecycle_mutation",
        "repair",
        "wrong_resolver_profile",
        "wrong_resolver_outcome",
        "resolver_status",
        "resolver_claim_lock",
        "resolver_claim_expires",
        "resolver_worker_pid",
        "step_mismatch",
        "status_mismatch",
        "preflight_assignee_mismatch",
        "assignee_mismatch",
        "escalate",
        "retry_worker_pid",
        "retry_started_work",
        "retry_second_failure",
    ],
)
def test_dispatch_refreshes_when_resolver_resume_provenance_is_not_current(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    board = f"story-refresh-resolver-guard-{mutation}"
    _repo, story_id, story_worktree, _branch = _story_fixture(tmp_path, board)
    (story_worktree / "worker-change.txt").write_text("preserve\n", encoding="utf-8")
    spawned: list[str] = []
    monkeypatch.setattr(
        kb, "_stamp_run_executor_identity", _stamp_executor_event
    )

    with kb.connect(board=board) as conn:
        decision = (
            "escalate" if mutation == "escalate"
            else "repair" if mutation == "repair"
            else "resume"
        )
        _resolve_story_preflight(
            conn,
            story_id,
            board,
            decision=decision,
            repair=(
                {"workflow": {"phase": "development", "assignee": "developer"}}
                if mutation == "repair" else None
            ),
        )
        events = kb.list_events(conn, story_id)
        resolved = [
            event for event in events if event.kind == "human_input_preflight_resolved"
        ][-1]

        if mutation == "newer_preflight":
            with kb.write_txn(conn):
                kb._append_event(
                    conn,
                    story_id,
                    kb.PRODUCT_WORKFLOW_PRECHECK_EVENT,
                    {
                        "reason": "newer evidence",
                        "kind": "needs_input",
                        "original_assignee": "developer",
                        "hermes_assignee": "resolver",
                        "step_key": "development",
                        "resume_status": "ready",
                    },
                )
        elif mutation == "lifecycle_mutation":
            assert kb.assign_task(conn, story_id, "developer")
        elif mutation == "wrong_resolver_outcome":
            conn.execute(
                "UPDATE task_runs SET outcome='preflight_repaired' WHERE id=?",
                (resolved.run_id,),
            )
            conn.commit()
        elif mutation == "resolver_status":
            conn.execute(
                "UPDATE task_runs SET status='blocked' WHERE id=?",
                (resolved.run_id,),
            )
            conn.commit()
        elif mutation == "resolver_claim_lock":
            conn.execute(
                "UPDATE task_runs SET claim_lock='stale-lock' WHERE id=?",
                (resolved.run_id,),
            )
            conn.commit()
        elif mutation == "resolver_claim_expires":
            conn.execute(
                "UPDATE task_runs SET claim_expires=1 WHERE id=?",
                (resolved.run_id,),
            )
            conn.commit()
        elif mutation == "resolver_worker_pid":
            conn.execute(
                "UPDATE task_runs SET worker_pid=9876 WHERE id=?",
                (resolved.run_id,),
            )
            conn.commit()
        elif mutation == "preflight_assignee_mismatch":
            preflight = next(
                event for event in events
                if event.id == resolved.payload["preflight_event_id"]
            )
            payload = dict(preflight.payload or {})
            payload["hermes_assignee"] = "not-resolver"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(payload), preflight.id),
            )
            conn.commit()
        elif mutation in {
            "wrong_resolver_profile",
            "step_mismatch",
            "status_mismatch",
            "assignee_mismatch",
        }:
            payload = dict(resolved.payload or {})
            updates = {
                "wrong_resolver_profile": ("resolver_profile", "not-resolver"),
                "step_mismatch": ("step_key", "architecture"),
                "status_mismatch": ("status", "todo"),
                "assignee_mismatch": ("assignee", "not-developer"),
            }
            field, value = updates[mutation]
            payload[field] = value
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(payload), resolved.id),
            )
            conn.commit()

        if mutation.startswith("retry_"):
            attempts = 0

            def retry_spawn(task: kb.Task, workspace: str) -> int:
                nonlocal attempts
                attempts += 1
                if mutation == "retry_second_failure" or attempts == 1:
                    raise RuntimeError("spawn failed")
                spawned.append(task.id)
                return 4242

            assert kb._spawn_one_v2(
                conn,
                story_id,
                board=board,
                spawn_fn=retry_spawn,
                failure_limit=3,
            ) is None
            failed_run = conn.execute(
                "SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (story_id,),
            ).fetchone()
            assert failed_run is not None
            if mutation == "retry_worker_pid":
                conn.execute(
                    "UPDATE task_runs SET worker_pid=9876 WHERE id=?",
                    (failed_run["id"],),
                )
                conn.commit()
            elif mutation == "retry_started_work":
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, step_key, status, claim_lock, "
                    "claim_expires, started_at) VALUES (?, ?, ?, 'running', ?, ?, ?)"
                    ,
                    (story_id, "developer", "development", "started-lock", 1, 1),
                )
                conn.commit()
            if mutation == "retry_second_failure":
                assert kb._spawn_one_v2(
                    conn,
                    story_id,
                    board=board,
                    spawn_fn=retry_spawn,
                    failure_limit=3,
                ) is None
            pid = kb._spawn_one_v2(
                conn,
                story_id,
                board=board,
                spawn_fn=retry_spawn,
                failure_limit=3,
            )
            assert attempts == (2 if mutation == "retry_second_failure" else 1)
        else:
            pid = kb._spawn_one_v2(
                conn,
                story_id,
                board=board,
                spawn_fn=lambda task, workspace: spawned.append(task.id) or 4242,
            )
        task = kb.get_task(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert pid is None
    assert spawned == []
    assert task is not None
    assert (story_worktree / "worker-change.txt").read_text(encoding="utf-8") == "preserve\n"
    attention = [
        event for event in events if event.kind == "story_refresh_attention_required"
    ]
    assert len(attention) == 1
    assert attention[0].payload["kind"] == "dirty"


def test_dispatch_does_not_bypass_refresh_after_recovery_card_is_reassigned(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = "story-refresh-resolver-reassigned"
    _repo, story_id, story_worktree, _branch = _story_fixture(tmp_path, board)
    spawned: list[str] = []
    monkeypatch.setattr(
        kb, "_stamp_run_executor_identity", _stamp_executor_event
    )

    with kb.connect(board=board) as conn:
        ordinary = kb.claim_task(conn, story_id)
        assert ordinary is not None and ordinary.current_run_id is not None
        assert kb.block_task(
            conn,
            story_id,
            reason="The governed worker needs recovery",
            kind="needs_input",
            expected_run_id=ordinary.current_run_id,
            board=board,
            human_escalation_assignee="resolver",
        )
        assert kb.assign_task(conn, story_id, "developer")
        (story_worktree / "worker-change.txt").write_text(
            "preserve\n", encoding="utf-8"
        )

        pid = kb._spawn_one_v2(
            conn,
            story_id,
            board=board,
            spawn_fn=lambda task, workspace: spawned.append(task.id) or 4242,
        )
        task = kb.get_task(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert pid is None
    assert spawned == []
    assert task is not None and task.status == "ready" and task.assignee == "developer"
    attention = [
        event for event in events if event.kind == "story_refresh_attention_required"
    ]
    assert len(attention) == 1
    assert attention[0].payload["kind"] == "dirty"


def test_dispatch_routes_isolated_conflict_to_development_rework(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = "story-refresh-conflict"
    repo = _repository(tmp_path)
    _board(board, repo)
    _epic_id, story_id, story_worktree, epic_branch = _story_card(board, repo)

    _commit(story_worktree, "shared.txt", "story\n", "fixture: story change")
    _git(repo, "checkout", epic_branch)
    _commit(repo, "shared.txt", "epic\n", "fixture: epic change")
    _git(repo, "checkout", "main")
    monkeypatch.setattr(kb, "_stamp_run_executor_identity", lambda *_args, **_kwargs: None)

    with kb.connect(board=board) as conn:
        spawned = kb._spawn_one_v2(
            conn,
            story_id,
            board=board,
            spawn_fn=lambda task, workspace: 4242,
        )
        task = kb.get_task(conn, story_id)
        directive = kb.active_rework_directive(conn, story_id)
        events = kb.list_events(conn, story_id)

    assert spawned is None
    assert task is not None
    assert task.status == "ready" and task.current_step_key == "development"
    assert task.assignee == "developer"
    assert task.claim_lock is None and task.current_run_id is None
    assert directive is not None
    assert directive.origin_kind == "refresh"
    assert directive.origin_phase == "architecture"
    assert directive.target_phase == "development"
    assert directive.rejected_branch == "story/refresh-fixture"
    assert directive.epic_tip_sha
    assert "shared.txt" in directive.findings[0]
    conflict = next(event for event in events if event.kind == "story_refresh_conflict")
    retained = Path(conflict.payload["conflict_worktree"])
    assert retained.is_dir()
    assert "shared.txt" in conflict.payload["conflict_paths"]
    routed = next(event for event in events if event.kind == "story_refresh_rework_routed")
    assert routed.payload["directive_id"] == directive.id
