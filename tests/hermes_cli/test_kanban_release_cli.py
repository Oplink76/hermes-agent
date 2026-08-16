"""`hermes kanban release-state` — read-only Epic release / member integration state.

Epics show named lifecycle states (collecting_members, aggregate_verification,
awaiting_final_release, ci_pending, ci_failed, done) with snapshot evidence.
Members show integration state (integrating, integration_failed, integrated).
No route invokes merge or push.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_epic_release import (
    EpicReadiness,
    EpicReadinessMember,
)


@pytest.fixture
def release_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _json_state(task_id: str, board: str | None = None) -> dict:
    extra = f"--board {board} " if board else ""
    out = kc.run_slash(f"{extra}release-state {task_id} --json")
    return json.loads(out)


def _direct_task_seed(conn, task_id: str, title: str,
                       work_item_kind: str = "card",
                       status: str = "done") -> None:
    """Insert a minimal task row (bypassing create_task)."""
    conn.execute(
        "INSERT INTO tasks (id, title, status, work_item_kind, created_at) "
        "VALUES (?, ?, ?, ?, 99)",
        (task_id, title, status, work_item_kind),
    )


# ── Epic lifecycle tests ─────────────────────────────────────────────────


def test_collecting_members(release_home):
    """Fresh epic with no members → collecting_members."""
    with kb.connect() as conn:
        epic_id = kb.create_task(conn, title="Epic: new", work_item_kind="epic")
    state = _json_state(epic_id)
    assert state["kind"] == "epic"
    assert state["state"] == "collecting_members"
    assert state["actionable"] is False


def test_awaiting_final_release(release_home, tmp_path, monkeypatch):
    """Snapshot awaiting_push + successful handoff → awaiting_final_release."""
    board = "e07-afr"
    repo, _, _ = _repo(tmp_path)
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic: AFR", board=board,
                                  work_item_kind="epic")
        _seed_snapshot(conn, epic_id, status="awaiting_push")

    class _H:
        def __init__(self, local_target_head="", remote_target_head="",
                     remote_name="", checked_at=0, action=""):
            self.local_target_head = local_target_head
            self.remote_target_head = remote_target_head
            self.remote_name = remote_name
            self.checked_at = checked_at
            self.action = action

    monkeypatch.setattr(
        kb, "build_epic_release_handoff",
        lambda conn_, eid, **kw: _H(
            local_target_head="2" * 40, remote_target_head="2" * 40,
            remote_name="origin", checked_at=100,
            action="Merge and push the pinned candidate externally.",
        ),
    )

    state = _json_state(epic_id, board)
    assert state["state"] == "awaiting_final_release"
    assert state["actionable"] is True
    assert "Merge and push" in (state.get("action") or "")


def test_ci_pending(release_home, tmp_path, monkeypatch):
    """Snapshot ci_pending → ci_pending non-actionable."""
    board = "e07-cip"
    repo, _, _ = _repo(tmp_path)
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic: CIP", board=board,
                                  work_item_kind="epic")
        _seed_snapshot(conn, epic_id, status="ci_pending", pushed_sha="6" * 40)

    monkeypatch.setattr(
        kb, "epic_readiness",
        lambda conn_, epic_id_, **kw: _fake_readiness(),
    )
    state = _json_state(epic_id, board)
    assert state["state"] == "ci_pending"
    assert state["actionable"] is False


def test_ci_failed(release_home, tmp_path, monkeypatch):
    """Snapshot ci_failed + CI failure event → ci_failed + CI evidence."""
    board = "e07-cif"
    repo, _, _ = _repo(tmp_path)
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic: CIF", board=board,
                                  work_item_kind="epic")
        _seed_snapshot(conn, epic_id, status="ci_failed", pushed_sha="6" * 40)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'epic_release_ci_failed', ?, 1)",
            (epic_id, json.dumps({"conclusions": {"CI": "failure", "Deploy": "cancelled"}})),
        )

    monkeypatch.setattr(
        kb, "epic_readiness",
        lambda conn_, epic_id_, **kw: _fake_readiness(),
    )
    state = _json_state(epic_id, board)
    assert state["state"] == "ci_failed"
    assert state["actionable"] is False
    assert (state.get("evidence") or {}).get("ci_evidence") == {
        "CI": "failure", "Deploy": "cancelled",
    }


def test_aggregate_verification(release_home, tmp_path, monkeypatch):
    """Readiness ready + no snapshot → aggregate_verification."""
    board = "e07-av"
    repo, _, _ = _repo(tmp_path)
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic: AV", board=board,
                                  work_item_kind="epic")

    monkeypatch.setattr(
        kb, "epic_readiness",
        lambda conn_, epic_id_, **kw: _fake_readiness(ready=True),
    )
    state = _json_state(epic_id, board)
    assert state["state"] == "aggregate_verification"
    assert state["actionable"] is False


def test_done(release_home, tmp_path, monkeypatch):
    """Snapshot released → done."""
    board = "e07-done"
    repo, _, _ = _repo(tmp_path)
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic: DONE", board=board,
                                  work_item_kind="epic")
        _seed_snapshot(conn, epic_id, status="released")

    monkeypatch.setattr(
        kb, "epic_readiness",
        lambda conn_, epic_id_, **kw: _fake_readiness(),
    )
    state = _json_state(epic_id, board)
    assert state["state"] == "done"
    assert state["actionable"] is False


# ── Member integration tests ─────────────────────────────────────────────


def test_member_integrating(release_home):
    """Member with active intent → integrating."""
    epic_id = "epic-int-1"
    story_id = "story-int-1"
    with kb.connect() as conn:
        _direct_task_seed(conn, epic_id, "Epic", "epic")
        _direct_task_seed(conn, story_id, "Story")
        conn.execute(
            "INSERT INTO epic_memberships (epic_id, task_id, created_at) "
            "VALUES (?, ?, 1)", (epic_id, story_id),
        )
        conn.execute(
            "INSERT INTO story_integration_intents "
            "(epic_id, story_id, source_sha, source_branch, review_run_id, "
            "review_base_sha, status, attempt_count, last_failure_code, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'b', 1, ?, 'pending', 2, NULL, 1, 1)",
            (epic_id, story_id, "9" * 40, "0" * 40),
        )

    state = _json_state(story_id)
    assert state["kind"] == "member"
    assert state["state"] == "integrating"
    assert state["actionable"] is False
    intent = (state.get("evidence") or {}).get("intent") or {}
    assert intent.get("status") == "pending"
    assert intent.get("attempt_count") == 2


def test_member_integration_failed(release_home):
    """Member whose latest intent carries a safe failure code → integration_failed."""
    epic_id = "epic-fail-1"
    story_id = "story-fail-1"
    with kb.connect() as conn:
        _direct_task_seed(conn, epic_id, "Epic", "epic")
        _direct_task_seed(conn, story_id, "Story")
        conn.execute(
            "INSERT INTO epic_memberships (epic_id, task_id, created_at) "
            "VALUES (?, ?, 1)", (epic_id, story_id),
        )
        conn.execute(
            "INSERT INTO story_integration_intents "
            "(epic_id, story_id, source_sha, source_branch, review_run_id, "
            "review_base_sha, status, attempt_count, last_failure_code, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'b', 1, ?, 'attention_required', 3, 'merge_conflict', 1, 1)",
            (epic_id, story_id, "9" * 40, "0" * 40),
        )

    state = _json_state(story_id)
    assert state["kind"] == "member"
    assert state["state"] == "integration_failed"
    intent = (state.get("evidence") or {}).get("intent") or {}
    assert intent.get("safe_code") == "merge_conflict"


def test_member_integrated(release_home):
    """Member with durable integration fact → integrated."""
    epic_id = "epic-fact-1"
    story_id = "story-fact-1"
    with kb.connect() as conn:
        _direct_task_seed(conn, epic_id, "Epic", "epic")
        _direct_task_seed(conn, story_id, "Story")
        conn.execute(
            "INSERT INTO epic_memberships (epic_id, task_id, created_at) "
            "VALUES (?, ?, 1)", (epic_id, story_id),
        )
        conn.execute(
            "INSERT INTO epic_story_integrations "
            "(epic_id, story_id, source_sha, candidate_sha, integrated_at) "
            "VALUES (?, ?, ?, ?, 100)",
            (epic_id, story_id, "9" * 40, "a" * 40),
        )

    state = _json_state(story_id)
    assert state["kind"] == "member"
    assert state["state"] == "integrated"
    assert (state.get("evidence") or {}).get("fact") is not None


def test_member_not_integrated(release_home):
    """Member with no intent or fact → not_integrated."""
    epic_id = "epic-no-1"
    story_id = "story-no-1"
    with kb.connect() as conn:
        _direct_task_seed(conn, epic_id, "Epic", "epic")
        _direct_task_seed(conn, story_id, "Story")
        conn.execute(
            "INSERT INTO epic_memberships (epic_id, task_id, created_at) "
            "VALUES (?, ?, 1)", (epic_id, story_id),
        )

    state = _json_state(story_id)
    assert state["kind"] == "member"
    assert state["state"] == "not_integrated"


# ── Regression: old `release` subcommand removed ─────────────────────────


def test_old_release_subcommand_removed(release_home):
    """The old `release` command is gone."""
    out = kc.run_slash("release t_nonexistent --note x")
    assert "invalid choice" in out.lower() or "unknown" in out.lower()


# ── Helpers ───────────────────────────────────────────────────────────────


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@e")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run_tests.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _git(repo, "add", "README.md", "scripts/run_tests.sh")
    _git(repo, "commit", "-m", "base")
    branch = "story/t"
    _git(repo, "switch", "-c", branch)
    (repo / "file.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "story")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    return repo, branch, sha


def _release_board(board: str, repo: Path, *, policy: str = "manual") -> None:
    kb.ensure_product_board_defaults(board, default_workdir=str(repo))
    path = kb.board_metadata_path(board)
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["deployment_policy"] = policy
    path.write_text(json.dumps(meta), encoding="utf-8")


def _seed_snapshot(conn, epic_id: str, *, status: str,
                    pushed_sha: str | None = None) -> None:
    conn.execute(
        "INSERT INTO epic_release_snapshots (epic_id, epic_tip_sha, target_branch, "
        "target_pre_sha, release_candidate_sha, candidate_ref, "
        "aggregate_verification_event_id, repository_contract_digest, "
        "status, pushed_sha, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (epic_id, "1" * 40, "main", "2" * 40, "3" * 40,
         "refs/hermes/releases/epic-1", 71, "7" * 64,
         status, pushed_sha, 100, 110),
    )


def _fake_readiness(ready: bool = False):
    return EpicReadiness(
        epic_id="epic-1", epic_tip_sha="1" * 40,
        members=(
            EpicReadinessMember(
                story_id="s-1", source_sha="4" * 40,
                candidate_sha="5" * 40, integrated_at=90,
            ),
        ) if ready else (),
        blockers=() if ready else ("nonterminal_member",),
    )