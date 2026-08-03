"""The epic base branch must exist by the time the first story is materialized.

Observed on Agentic OS Cockpit epic ``t_c29de776``: all three qualified story
worktrees and branches materialized, but ``epic/t_c29de776`` did not. Review
runs then failed before reviewer spawn, because ``_story_base_branch`` selects
the epic branch as the review base and ``git merge-base`` could not resolve it.
An operator had to create the ref by hand.

Root cause: only ``_spawn_one_v2`` (the handoff_v2 event consumer) passed
``base_branch=_story_base_branch(...)`` into ``_resolve_worktree_workspace``.
The time-polling ready loop, the review loop, and ``resolve_workspace`` all
called it with ``base_branch=None``, so ``_ensure_epic_branch`` never ran on
those paths and a story could materialize with its required epic base absent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "epic@example.com")
    _git(repo, "config", "user.name", "Epic Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def _v2_board(board: str, repo: Path) -> None:
    kb.ensure_product_board_defaults(board, default_workdir=str(repo))
    path = kb.board_metadata_path(board)
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["handoff_v2"] = True
    path.write_text(json.dumps(meta), encoding="utf-8")
    if _git(repo, "status", "--porcelain"):
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "board bootstrap")


def _epic_with_story(conn, board: str, repo: Path, title: str) -> tuple[str, str]:
    epic_id = kb.create_task(
        conn, title="Epic outcome", board=board, work_item_kind="epic",
    )
    story_id = kb.create_task(
        conn,
        title=title,
        board=board,
        assignee="developer",
        workspace_kind="worktree",
        workspace_path=str(repo),
        workflow_template_id="product",
        current_step_key="development",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    return epic_id, story_id


def _add_story(conn, board: str, repo: Path, epic_id: str, title: str) -> str:
    story_id = kb.create_task(
        conn,
        title=title,
        board=board,
        assignee="developer",
        workspace_kind="worktree",
        workspace_path=str(repo),
        workflow_template_id="product",
        current_step_key="development",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    return story_id


def test_ready_loop_materialization_creates_the_epic_base_branch(
    epic_home, tmp_path, all_assignees_spawnable
):
    repo = _repo(tmp_path)
    board = "epic-ready-loop"
    _v2_board(board, repo)
    base_sha = _git(repo, "rev-parse", "HEAD")
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board=board)
        story = kb.get_task(conn, story_id)

    epic_branch = kb.epic_branch_for(epic_id)
    # The epic base exists...
    assert _git(repo, "rev-parse", "--verify", epic_branch)
    # ...at exactly the commit the first story branched from...
    assert _git(repo, "rev-parse", epic_branch) == base_sha
    assert story is not None and story.branch_name
    assert _git(repo, "merge-base", epic_branch, story.branch_name) == base_sha
    # ...and the story branch really is rooted on it.
    assert (
        _git(repo, "merge-base", "--is-ancestor", epic_branch, story.branch_name)
        == ""
    )


def test_sibling_materialization_does_not_move_the_epic_base_branch(
    epic_home, tmp_path, all_assignees_spawnable
):
    repo = _repo(tmp_path)
    board = "epic-sibling"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id, _first = _epic_with_story(conn, board, repo, "Story one")
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board=board)
    epic_branch = kb.epic_branch_for(epic_id)
    pinned = _git(repo, "rev-parse", epic_branch)

    # main moves on before the sibling is dispatched.
    (repo / "moved.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "moved.txt")
    _git(repo, "commit", "-m", "main moves on")
    assert _git(repo, "rev-parse", "HEAD") != pinned

    with kb.connect(board=board) as conn:
        sibling_id = _add_story(conn, board, repo, epic_id, "Story two")
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board=board)
        sibling = kb.get_task(conn, sibling_id)

    assert _git(repo, "rev-parse", epic_branch) == pinned
    assert sibling is not None and sibling.branch_name
    # The sibling branched off the epic base, not off the moved main.
    assert _git(repo, "merge-base", epic_branch, sibling.branch_name) == pinned


def test_resolve_workspace_derives_the_epic_base_without_an_explicit_argument(
    epic_home, tmp_path
):
    """The generic resolver is the seam every dispatch path shares — deriving
    the base there is what stops a new call site from reintroducing the bug."""
    repo = _repo(tmp_path)
    board = "epic-generic-resolver"
    _v2_board(board, repo)
    base_sha = _git(repo, "rev-parse", "HEAD")
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        kb._resolve_worktree_workspace(story, board=board, conn=conn)

    epic_branch = kb.epic_branch_for(epic_id)
    assert _git(repo, "rev-parse", epic_branch) == base_sha


def test_reusing_a_story_worktree_recovers_the_pinned_epic_base(
    epic_home, tmp_path
):
    """A deleted epic base is restored from the persisted SHA, not from HEAD.

    The base is pinned to the event ledger when it is first created, so branch
    cleanup and re-cloning are both safe: recovery uses the recorded commit
    even after `main` has moved on.
    """
    repo = _repo(tmp_path)
    board = "epic-reuse-recovers"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        workspace, branch = kb._resolve_worktree_workspace(
            story, board=board, conn=conn
        )
        epic_branch = kb.epic_branch_for(epic_id)
        original = _git(repo, "rev-parse", epic_branch)
        assert kb._epic_base_pinned_sha(conn, epic_id) == original

        _git(repo, "branch", "-D", epic_branch)
        (repo / "moved.txt").write_text("later\n", encoding="utf-8")
        _git(repo, "add", "moved.txt")
        _git(repo, "commit", "-m", "main moves on")
        assert _git(repo, "rev-parse", "HEAD") != original

        kb.set_workspace_path(conn, story_id, str(workspace))
        kb.set_branch_name(conn, story_id, branch)
        reused = kb.get_task(conn, story_id)
        assert reused is not None
        kb._resolve_worktree_workspace(reused, board=board, conn=conn)

    assert _git(repo, "rev-parse", epic_branch) == original


def test_mature_epic_recovers_after_every_local_ref_is_removed(
    epic_home, tmp_path
):
    """The re-clone case: local branches are gone, the ledger is not."""
    repo = _repo(tmp_path)
    board = "epic-reclone"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        workspace, branch = kb._resolve_worktree_workspace(
            story, board=board, conn=conn
        )
        epic_branch = kb.epic_branch_for(epic_id)
        original = _git(repo, "rev-parse", epic_branch)

        # Simulate a fresh clone / aggressive cleanup: every ref this epic
        # produced is gone, and main has advanced.
        _git(repo, "worktree", "remove", str(workspace), "--force")
        _git(repo, "branch", "-D", epic_branch)
        _git(repo, "branch", "-D", branch)
        (repo / "moved.txt").write_text("later\n", encoding="utf-8")
        _git(repo, "add", "moved.txt")
        _git(repo, "commit", "-m", "main moves on")

        sibling_id = _add_story(conn, board, repo, epic_id, "Story two")
        sibling = kb.get_task(conn, sibling_id)
        assert sibling is not None
        kb._resolve_worktree_workspace(sibling, board=board, conn=conn)

    # Recovered from the ledger, not from the moved HEAD.
    assert _git(repo, "rev-parse", epic_branch) == original


def test_legacy_epic_without_a_pin_fails_closed(epic_home, tmp_path):
    """No pin, no integration, but prior materialization history: refuse."""
    repo = _repo(tmp_path)
    board = "epic-legacy-no-pin"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id, first_id = _epic_with_story(conn, board, repo, "Story one")
        sibling_id = _add_story(conn, board, repo, epic_id, "Story two")
        # A legacy epic: the first story really ran, but predates base pinning.
        with kb.write_txn(conn):
            kb._synthesize_ended_run(
                conn, first_id, outcome="advanced", step_key="development",
            )
        assert kb._epic_base_pinned_sha(conn, epic_id) is None

        sibling = kb.get_task(conn, sibling_id)
        assert sibling is not None
        with pytest.raises(RuntimeError, match="cannot be established"):
            kb._resolve_worktree_workspace(sibling, board=board, conn=conn)

    assert not kb._git_branch_exists(repo, kb.epic_branch_for(epic_id))


def test_missing_epic_base_fails_materialization_loudly(
    epic_home, tmp_path, monkeypatch
):
    """A story worktree must never be left usable while its required epic base
    is absent — the failure has to be explicit, not silent."""
    repo = _repo(tmp_path)
    board = "epic-loud-failure"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        _epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        monkeypatch.setattr(
            kb, "_git_branch_exists", lambda _repo, _branch: False
        )
        monkeypatch.setattr(
            subprocess, "run", _refuse_branch_creation(subprocess.run)
        )
        with pytest.raises(RuntimeError, match="epic"):
            kb._resolve_worktree_workspace(story, board=board, conn=conn)


def _refuse_branch_creation(real_run):
    def _run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "branch" in cmd:
            class _Result:
                returncode = 1
                stdout = ""
                stderr = "refused by test"
            return _Result()
        return real_run(cmd, *args, **kwargs)

    return _run


def test_integration_moves_the_pin_so_recovery_restores_the_epic_tip(
    epic_home, tmp_path
):
    """Recovery must restore the integrated tip, not the original base.

    Story cards go `done` after integrating, so `gc_events` prunes their
    `story_integrated_to_epic` rows. If the pin stayed at the first base, a
    later sibling would branch off a base missing every integrated story.
    """
    repo = _repo(tmp_path)
    board = "epic-pin-follows-tip"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        kb._resolve_worktree_workspace(story, board=board, conn=conn)
        epic_branch = kb.epic_branch_for(epic_id)
        original = _git(repo, "rev-parse", epic_branch)

        # A story integrates: the epic branch advances to a new tip.
        _git(repo, "checkout", epic_branch)
        (repo / "integrated.txt").write_text("story one\n", encoding="utf-8")
        _git(repo, "add", "integrated.txt")
        _git(repo, "commit", "-m", "integrate story one")
        tip = _git(repo, "rev-parse", epic_branch)
        _git(repo, "checkout", "main")
        assert tip != original
        kb._record_story_integration(
            conn, story_id, epic_id, epic_branch,
            {"target_branch": epic_branch, "candidate_sha": tip},
        )
        assert kb._epic_base_pinned_sha(conn, epic_id) == tip

        # The story's own events are pruned exactly as gc_events would, so the
        # pin is the only surviving evidence.
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
                (story_id, "story_integrated_to_epic"),
            )
        _git(repo, "branch", "-D", epic_branch)
        (repo / "moved.txt").write_text("later\n", encoding="utf-8")
        _git(repo, "add", "moved.txt")
        _git(repo, "commit", "-m", "main moves on")

        sibling_id = _add_story(conn, board, repo, epic_id, "Story two")
        sibling = kb.get_task(conn, sibling_id)
        assert sibling is not None
        kb._resolve_worktree_workspace(sibling, board=board, conn=conn)

    assert _git(repo, "rev-parse", epic_branch) == tip


def test_refused_resolver_routing_runs_before_any_other_claim_mutation(
    epic_home, tmp_path
):
    """The refusal is the first statement in the claim transaction, so the
    metadata-repair preflight and the parents demotion never touch a card that
    can never dispatch."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Ordinary goal", assignee="developer")
        conn.execute("UPDATE tasks SET assignee='resolver' WHERE id=?", (tid,))
        conn.commit()
        before = [event.kind for event in kb.list_events(conn, tid)]

        assert kb.claim_task(conn, tid) is None

        after = [event.kind for event in kb.list_events(conn, tid)]
        task = kb.get_task(conn, tid)
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)
        ).fetchone()[0]

    # Exactly one new event — the refusal. Nothing repaired, nothing demoted.
    assert after == before + ["claim_rejected"]
    assert runs == 0
    assert task is not None and task.status == "blocked"


def test_repeated_integration_of_the_same_tip_writes_one_pin(
    epic_home, tmp_path
):
    """A re-entrant integration pass must not multiply pin events.

    Live boards re-record `already_integrated` for done stories on every
    dispatcher tick — 17,884 such events on one board in 101 hours. A pin per
    tick would double that churn and record nothing new.
    """
    repo = _repo(tmp_path)
    board = "epic-pin-dedupe"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        kb._resolve_worktree_workspace(story, board=board, conn=conn)
        epic_branch = kb.epic_branch_for(epic_id)
        tip = _git(repo, "rev-parse", epic_branch)

        def pins():
            return conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
                (epic_id, kb.EPIC_BASE_PINNED_EVENT),
            ).fetchone()[0]

        baseline = pins()
        for _ in range(5):
            kb._record_story_integration(
                conn, story_id, epic_id, epic_branch,
                {"target_branch": epic_branch, "candidate_sha": tip,
                 "already_integrated": True},
            )
        # The story's own integration events are still recorded every time.
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
            (story_id, "story_integrated_to_epic"),
        ).fetchone()[0] == 5
        assert pins() == baseline, "an unchanged tip must not write a new pin"

        # A real advance still pins exactly once.
        _git(repo, "checkout", epic_branch)
        (repo / "advanced.txt").write_text("more\n", encoding="utf-8")
        _git(repo, "add", "advanced.txt")
        _git(repo, "commit", "-m", "advance the epic")
        moved = _git(repo, "rev-parse", epic_branch)
        _git(repo, "checkout", "main")
        kb._record_story_integration(
            conn, story_id, epic_id, epic_branch,
            {"target_branch": epic_branch, "candidate_sha": moved},
        )
        assert pins() == baseline + 1
        assert kb._epic_base_pinned_sha(conn, epic_id) == moved
