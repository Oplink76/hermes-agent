"""End-to-end proof of the governed member-to-Epic release lifecycle.

Covers the full public path: member stories through the product workflow,
dispatcher-owned story integration into the Epic branch, immutable Epic
release snapshot preparation, read-only CI observation, drift invalidation,
crash recovery, CAS refusal, the checked-out Epic-branch migration blocker,
pruned-event recovery, legacy-writer grandfathering, and the structural
no-push boundary.

Every engine Git invocation flows through a fake ``git`` executable that
logs its argv and fails on any push.  Only the test harness (through
:meth:`FakeGit.real`) may mutate the temporary bare remote.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_v2_migration as migration
from hermes_cli.kanban_repository import _prepared_ref_sha

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fixture exercises the required POSIX scripts/run_tests.sh project contract",
)


# ---------------------------------------------------------------------------
# Fake git harness
# ---------------------------------------------------------------------------

class FakeGit:
    """A PATH-resident fake ``git`` that logs every engine invocation and
    refuses any push.  The test harness calls :meth:`real` for setup and
    remote-pushing -- it bypasses the fake entirely."""

    def __init__(self, base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._real_git = shutil.which("git")
        assert self._real_git is not None, "real git not found in PATH"
        self._bin_dir = base / "fake-bin"
        self._bin_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._bin_dir / "git-args.log"

        # Python-based fake git: logs argv and refuses push.
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import os, sys
            args = sys.argv[1:]
            with open({self._log_path.as_posix()!r}, "a", encoding="utf-8") as f:
                f.write("\\t".join(a.replace("\\t", "\\\\t") for a in args) + "\\n")
            if "push" in args:
                sys.stderr.write("fake-git: push refused by test boundary\\n")
                sys.exit(128)
            os.execv({self._real_git!r}, [{self._real_git!r}] + args)
        """)
        (self._bin_dir / "git").write_text(script, encoding="utf-8")
        (self._bin_dir / "git").chmod(0o755)

        old_path = os.environ.get("PATH", os.defpath)
        monkeypatch.setenv("PATH", f"{self._bin_dir}{os.pathsep}{old_path}")

    def real(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        """Run the REAL git from ``cwd`` -- harness side. Bypasses the fake."""
        assert self._real_git is not None
        real_git: str = self._real_git
        result = subprocess.run(
            [real_git, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise AssertionError(f"git {' '.join(args)} failed: {detail}")
        return result

    @property
    def invocations(self) -> list[str]:
        """Every logged line (tab-joined argv)."""
        if not self._log_path.exists():
            return []
        return self._log_path.read_text(encoding="utf-8").rstrip("\n").splitlines()

    @property
    def push_invocations(self) -> list[str]:
        """Invocations whose argv contained ``push``."""
        return [line for line in self.invocations if "\tpush\t" in f"\t{line}\t"]

    def reset_log(self) -> None:
        if self._log_path.exists():
            self._log_path.unlink()


# ---------------------------------------------------------------------------
# Board metadata helpers
# ---------------------------------------------------------------------------

def _default_product_board_metadata(board: str, repo: Path) -> dict:
    """Set up a governed product board with a repository contract."""
    kb.ensure_product_board_defaults(
        board,
        name=board.replace("-", " ").title(),
        default_workdir=str(repo),
    )
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    pw = meta.setdefault("product_workflow", {})
    pw["handoff_v2"] = True
    meta["repository"] = {
        "base_ref": "refs/remotes/origin/main",
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": {
                "commands": [
                    {
                        "argv": ["bash", "tests/e2e_scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 60,
                    },
                ],
            },
            "epic_release": {
                "commands": [
                    {
                        "argv": ["bash", "tests/e2e_scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 60,
                    },
                ],
            },
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
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def governed_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with kanban-governance enabled."""
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


@pytest.fixture
def fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    """Fake ``git`` on PATH that refuses push and logs every invocation."""
    return FakeGit(tmp_path / "fake-git", monkeypatch)


@dataclass
class _ProductFixture:
    board: str
    repo: Path       # local clone (primary checkout)
    remote: Path     # bare remote
    fake_git: FakeGit
    initial_sha: str


@pytest.fixture
def product_fixture(
    governed_profile: Path,
    fake_git: FakeGit,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> _ProductFixture:
    """Bare remote + local clone with governed repository contract."""
    slug = (
        request.node.name
        .replace("[", "-").replace("]", "").replace("/", "-").replace(" ", "_")
    )
    board = f"e2e-epic-{slug}"[:80]
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    remote.mkdir()
    clone.mkdir(parents=True)

    # Bare remote & clone (harness uses real git)
    fake_git.real(tmp_path, "init", "--bare", "-b", "main", str(remote))
    fake_git.real(tmp_path, "clone", str(remote), str(clone))
    fake_git.real(clone, "config", "user.email", "fixture@e2e.test")
    fake_git.real(clone, "config", "user.name", "Epic E2E Fixture")

    # Write a passing verification script in a non-standard dir so it does
    # not collide with the main repo's scripts/run_tests.sh.
    script_dir = clone / "tests" / "e2e_scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "run_tests.sh").write_text(
        "#!/bin/sh\nset -eu\necho ok\nexit 0\n", encoding="utf-8",
    )
    (script_dir / "run_tests.sh").chmod(0o755)
    # Seed .gitignore BEFORE the board setup below, which appends the
    # engine-owned ``.worktrees/`` guard entry.
    (clone / ".gitignore").write_text("*.pyc\n__pycache__/\n")

    # Board metadata (also appends ``.worktrees/`` to the clone's .gitignore)
    _default_product_board_metadata(board, clone)

    fake_git.real(clone, "add", ".gitignore", "tests/e2e_scripts/run_tests.sh")
    fake_git.real(clone, "commit", "-m", "initial")
    initial_sha = fake_git.real(clone, "rev-parse", "HEAD").stdout.strip()
    fake_git.real(clone, "push", "origin", "main")

    # Log reset: all engine git invocations should come AFTER fixture setup
    fake_git.reset_log()

    return _ProductFixture(
        board=board, repo=clone, remote=remote,
        fake_git=fake_git, initial_sha=initial_sha,
    )


# ---------------------------------------------------------------------------
# Story lifecycle helpers
# ---------------------------------------------------------------------------

def _claim_and_complete(
    conn, task_id: str, claimer: str, board: str,
    *, summary: str, metadata: dict | None = None,
) -> None:
    """Claim a task and complete it with the given summary/metadata."""
    claimed = kb.claim_task(conn, task_id, board=board, claimer=claimer)
    assert claimed is not None and claimed.current_run_id is not None
    assert kb.complete_task(
        conn, task_id, summary=summary, metadata=metadata,
        expected_run_id=claimed.current_run_id, board=board,
    ), f"complete_task returned False for {task_id} ({summary})"


def _complete_development(
    conn, task_id: str, worktree: Path, branch: str, board: str,
) -> str:
    """Write content and complete the development step; return the handoff SHA."""
    (worktree / "story.txt").write_text("delivered\n", encoding="utf-8")
    dev = kb.claim_task(conn, task_id, board=board, claimer="dev")
    assert dev is not None and dev.current_run_id is not None
    assert kb.complete_task(
        conn, task_id,
        summary="Implementation done",
        metadata={
            "ai_provenance": {
                "writer": {"agent": "e2e-codex", "branch": branch},
            },
        },
        expected_run_id=dev.current_run_id,
        board=board,
    )
    dev_handoffs = [
        e for e in kb.list_events(conn, task_id)
        if e.kind == "handoff" and e.payload.get("from_step") == "development"
    ]
    assert dev_handoffs, "no development handoff event"
    dev_sha = dev_handoffs[-1].payload["sha"]
    assert len(dev_sha) == 40, dev_sha
    return dev_sha


def _complete_test(
    conn, task_id: str, worktree: Path, board: str,
) -> dict[str, str]:
    """Complete the test step with passed outcome + pins."""
    test = kb.claim_task(conn, task_id, board=board, claimer="tester")
    assert test is not None and test.current_run_id is not None
    test_pin = kb._prepare_test_target(
        conn, task_id, worktree, board=board,
    )
    assert isinstance(test_pin, dict)
    assert kb.complete_task(
        conn, task_id,
        summary="Tests passed",
        metadata={
            "workflow_outcome": {"verdict": "passed"},
            "ai_provenance": {
                "writer": {"agent": "e2e-codex"},
                "tester": {"agent": "e2e-hermes", "result": "passed"},
            },
            **test_pin,
        },
        expected_run_id=test.current_run_id,
        board=board,
    )
    return test_pin


def _complete_review(
    conn, task_id: str, worktree: Path, board: str,
) -> dict[str, str]:
    """Complete the review step with approved outcome + pins (enqueues intent)."""
    reviewer = kb.claim_review_task(conn, task_id, claimer="e2e-reviewer")
    assert reviewer is not None and reviewer.current_run_id is not None
    review_pin = kb._prepare_review_target(
        conn, task_id, worktree, board=board,
    )
    assert isinstance(review_pin, dict)
    assert kb.complete_task(
        conn, task_id,
        summary="Review approved",
        metadata={
            "workflow_outcome": {"verdict": "approved"},
            "ai_provenance": {
                "writer": {"agent": "e2e-codex"},
                "reviewer": {"agent": "e2e-codex-reviewer"},
            },
            **review_pin,
        },
        expected_run_id=reviewer.current_run_id,
        board=board,
    )
    return review_pin


def _drive_story_to_review(
    conn, task_id: str, worktree: Path, branch: str, board: str,
) -> str:
    """Drive one story through backlog→architecture→development→test→review.

    Returns the development handoff SHA.
    """
    _claim_and_complete(conn, task_id, "po", board, summary="Backlog accepted")
    _claim_and_complete(
        conn, task_id, "architect", board, summary="Architecture accepted",
    )
    dev_sha = _complete_development(conn, task_id, worktree, branch, board)
    _complete_test(conn, task_id, worktree, board)
    _complete_review(conn, task_id, worktree, board)
    return dev_sha


def _create_epic(conn, title: str) -> str:
    """Create an Epic task in the governed collecting state."""
    epic_id = kb.create_task(conn, title=title, work_item_kind="epic")
    with kb.authorized_governance_write(), kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product_epic', "
            "current_step_key='collecting_members', status='todo', "
            "assignee=NULL, running=0, blocked=0, current_run_id=NULL "
            "WHERE id=?", (epic_id,),
        )
    return epic_id


def _create_epic_member(
    conn, product: _ProductFixture, board: str, epic_id: str,
) -> tuple[str, Path, str]:
    """Create an epic-member story and materialize its worktree the same
    way the dispatcher does.  Returns ``(story_id, worktree, branch)``.
    """
    story_id = kb.create_task(
        conn,
        title="Story: epic member",
        assignee="po",
        board=board,
        workspace_kind="worktree",
        workspace_path=None,
        branch_name=None,
        workflow_template_id="product",
        current_step_key="backlog",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)

    # Dispatcher-owned materialization: resolve the worktree (creates the
    # Epic base branch when missing) and persist path + branch.
    task = kb.get_task(conn, story_id)
    assert task is not None
    workspace, branch = kb._resolve_worktree_workspace(
        task, board=board, conn=conn,
    )
    kb.set_workspace_path(conn, story_id, str(workspace))
    kb.set_branch_name(conn, story_id, branch)
    return story_id, workspace, branch


def _create_named_epic_branch(
    product: _ProductFixture, epic_id: str, start_sha: str,
) -> str:
    """Harness-side: create the named Epic branch at ``start_sha``."""
    epic_branch = kb.epic_branch_for(epic_id)
    product.fake_git.real(product.repo, "switch", "-c", epic_branch, start_sha)
    product.fake_git.real(product.repo, "switch", "main")
    return epic_branch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_full_member_to_epic_lifecycle_release(
    product_fixture: _ProductFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One story through the full lifecycle to Epic release + CI + done."""
    board = product_fixture.board
    repo = product_fixture.repo
    fg = product_fixture.fake_git

    from hermes_cli import kanban_repository as repo_module

    with kb.connect(board=board) as conn:
        epic_id = _create_epic(conn, "Epic: lifecycle proof")
        story_id, worktree, branch = _create_epic_member(
            conn, product_fixture, board, epic_id,
        )

        dev_sha = _drive_story_to_review(
            conn, story_id, worktree, branch, board,
        )
        assert kb._git_head_sha(worktree) == dev_sha

        # --- Story is now integration_pending; reconcile integrates ---
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert story_id in result.integrated, (
            f"Expected story integrated, got {result.integrated}"
        )

        story = kb.get_task(conn, story_id)
        assert story is not None and story.status == "done"
        assert story.current_step_key == "done"

        fact = conn.execute(
            "SELECT * FROM epic_story_integrations "
            "WHERE epic_id=? AND story_id=?",
            (epic_id, story_id),
        ).fetchone()
        assert fact is not None

        readiness = kb.epic_readiness(conn, epic_id, board=board)
        assert readiness.ready, f"blockers: {readiness.blockers}"
        assert len(readiness.members) == 1
        assert readiness.members[0].story_id == story_id

        # --- Epic release snapshot ---
        snapshot = kb.prepare_epic_release_snapshot(
            conn, epic_id, board=board,
        )
        assert snapshot.status == "awaiting_push"
        assert snapshot.epic_id == epic_id
        assert snapshot.release_candidate_sha is not None
        assert len(snapshot.release_candidate_sha) == 40
        assert snapshot.candidate_ref.startswith(
            "refs/hermes/release-candidates/",
        )
        assert _prepared_ref_sha(repo, snapshot.candidate_ref) == (
            snapshot.release_candidate_sha
        )

        # --- Read-only CI observation (not yet pushed) ---
        ci = kb.observe_epic_release_ci(conn, epic_id, board=board)
        assert ci.kind == "ci_pending", (
            f"Expected ci_pending, got {ci.kind} evidence={ci.evidence}"
        )
        assert ci.snapshot is not None
        assert ci.snapshot.status == "awaiting_push"

        # --- Release handoff (read-only, prose action only) ---
        handoff = kb.build_epic_release_handoff(conn, epic_id, board=board)
        assert isinstance(handoff.action, str) and handoff.action
        assert "push" not in handoff.action.lower()

        # --- CLI release-state path ---
        from hermes_cli import kanban as kanban_cli
        state = kanban_cli._task_release_state(conn, epic_id, board=board)
        assert state["kind"] == "epic"

        # --- Dashboard API path ---
        dashboard = _load_api_module()
        app = FastAPI()
        app.include_router(dashboard.router, prefix="/api/plugins/kanban")
        api = TestClient(app)

        resp = api.get(
            f"/api/plugins/kanban/tasks/{epic_id}?board={board}",
        )
        assert resp.status_code == 200, resp.text
        epic_body = resp.json()
        assert epic_body["epic_detail"]["release_state"] is not None

        # Dedicated read-only release-state API route
        resp_rs = api.get(
            f"/api/plugins/kanban/tasks/{epic_id}/release-state?board={board}",
        )
        assert resp_rs.status_code == 200, resp_rs.text
        assert resp_rs.json()["state"] is not None

        resp2 = api.get(
            f"/api/plugins/kanban/tasks/{story_id}?board={board}",
        )
        assert resp2.status_code == 200, resp2.text
        assert "member_release_state" in resp2.json()

        # --- Harness pushes the exact candidate to the remote target ---
        # (the ONLY remote mutation, and it is the harness, not the engine)
        candidate_sha = snapshot.release_candidate_sha
        fg.real(
            repo, "push", "origin",
            f"{candidate_sha}:refs/heads/main",
        )

        # Fake CI transport: GET-only observation seam
        real_remote_observe = repo_module._remote_observe_git

        class _FakeRemoteGetUrl:
            returncode = 0
            stdout = "https://github.com/e2e-fixture/kanban-e2e.git"
            stderr = ""

        def fake_remote_observe(repo_root: Path, *args: str):
            if tuple(args[:2]) == ("remote", "get-url"):
                return _FakeRemoteGetUrl()
            return real_remote_observe(repo_root, *args)

        monkeypatch.setattr(
            repo_module, "_remote_observe_git", fake_remote_observe,
        )
        monkeypatch.setattr(
            repo_module, "_http_observe_get",
            lambda url: {"workflow_runs": [
                {"name": "CI", "conclusion": "success"},
            ]},
        )

        released = kb.observe_epic_release_ci(conn, epic_id, board=board)
        assert released.kind == "released", (
            f"Expected released, got {released.kind} evidence={released.evidence}"
        )
        assert released.snapshot is not None
        assert released.snapshot.status == "released"
        # Exact-SHA candidate ref cleanup
        assert released.candidate_ref_deleted is True
        assert _prepared_ref_sha(repo, snapshot.candidate_ref) is None

        # Final lifecycle state via CLI read model: done
        final_state = kanban_cli._task_release_state(conn, epic_id, board=board)
        assert final_state["state"] == "done", final_state

    assert fg.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fg.push_invocations}"
    )


def test_epic_integration_conflict_refusal(
    product_fixture: _ProductFixture,
) -> None:
    """A conflicting Epic-branch commit routes integration back to rework
    without merge, approval, or push."""
    board = product_fixture.board
    repo = product_fixture.repo
    fg = product_fixture.fake_git

    with kb.connect(board=board) as conn:
        epic_id = _create_epic(conn, "Epic: conflict proof")
        story_id, worktree, branch = _create_epic_member(
            conn, product_fixture, board, epic_id,
        )

        # Drive through development + test, then diverge the Epic branch
        # with a conflicting commit before Review pins its base.
        _claim_and_complete(conn, story_id, "po", board, summary="Backlog accepted")
        _claim_and_complete(
            conn, story_id, "architect", board, summary="Architecture accepted",
        )
        _complete_development(conn, story_id, worktree, branch, board)
        _complete_test(conn, story_id, worktree, board)

        epic_branch = kb.epic_branch_for(epic_id)
        fg.real(repo, "switch", epic_branch)
        (repo / "story.txt").write_text("epic says no\n", encoding="utf-8")
        fg.real(repo, "add", "story.txt")
        fg.real(repo, "commit", "-m", "epic: conflicting version")
        fg.real(repo, "switch", "main")

        # Review pins the diverged Epic tip as its base.
        _complete_review(conn, story_id, worktree, board)

        before_events = len(kb.list_events(conn, story_id))
        before_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks",).fetchone()[0]
        before_links = conn.execute(
            "SELECT COUNT(*) FROM task_links",).fetchone()[0]

        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert story_id not in result.integrated

        story = kb.get_task(conn, story_id)
        assert story is not None
        assert story.current_step_key == "development", (
            f"Expected development after conflict, got "
            f"step={story.current_step_key} status={story.status}"
        )
        directive = kb.active_rework_directive(conn, story_id)
        assert directive is not None, "Expected a rework directive"
        assert directive.target_phase == "development"

        assert conn.execute(
            "SELECT COUNT(*) FROM tasks",).fetchone()[0] == before_tasks
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links",).fetchone()[0] == before_links

        event_kinds = {
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? AND id > ?",
                (story_id, before_events),
            ).fetchall()
        }
        assert not event_kinds.intersection(
            {"approval_requested", "release_requested",
             "release_approved", "story_merged_to_main"},
        )

    assert fg.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fg.push_invocations}"
    )


def test_epic_release_cas_and_invalidation(
    product_fixture: _ProductFixture,
) -> None:
    """Stale completion is refused; target drift invalidates the snapshot;
    the release candidate ref is cleaned up only when it still pins the
    recorded SHA."""
    board = product_fixture.board
    repo = product_fixture.repo
    fg = product_fixture.fake_git

    with kb.connect(board=board) as conn:
        epic_id = _create_epic(conn, "Epic: CAS proof")
        story_id, worktree, branch = _create_epic_member(
            conn, product_fixture, board, epic_id,
        )
        _drive_story_to_review(conn, story_id, worktree, branch, board)

        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert story_id in result.integrated

        snap1 = kb.prepare_epic_release_snapshot(conn, epic_id, board=board)
        assert snap1.status == "awaiting_push"

        # --- Stale completion CAS refusal (already-done story) ---
        story = kb.get_task(conn, story_id)
        assert story is not None
        assert not kb.complete_task(
            conn, story_id, summary="stale completion",
            expected_run_id=999_999_999, board=board,
        )

        # --- Advance local main (concurrent deployment simulation) ---
        fg.real(repo, "switch", "main")
        (repo / "marker.txt").write_text("advance\n", encoding="utf-8")
        fg.real(repo, "add", "marker.txt")
        fg.real(repo, "commit", "-m", "main advance")

        inv = kb.invalidate_epic_release_snapshot(
            conn, epic_id, board=board,
        )
        assert inv.kind == "invalidated", (
            f"Expected invalidated, got {inv.kind} evidence={inv.evidence}"
        )
        assert inv.snapshot.status == "invalidated"
        assert inv.candidate_ref_deleted is True
        assert _prepared_ref_sha(repo, snap1.candidate_ref) is None

        # No active snapshot remains → missing
        inv2 = kb.invalidate_epic_release_snapshot(
            conn, epic_id, board=board,
        )
        assert inv2.kind in {"exact", "missing"}

        # Re-prepare with the new target pre-sha
        snap2 = kb.prepare_epic_release_snapshot(conn, epic_id, board=board)
        assert snap2.status == "awaiting_push"
        assert snap2.target_pre_sha != snap1.target_pre_sha

    assert fg.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fg.push_invocations}"
    )


_INTEGRATION_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
    assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,
    completed_at INTEGER,
    workflow_template_id TEXT, current_step_key TEXT,
    work_item_kind TEXT NOT NULL DEFAULT 'card',
    running INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE epic_memberships (
    epic_id TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL, PRIMARY KEY (epic_id, task_id)
);
CREATE TABLE epic_story_integrations (
    epic_id TEXT NOT NULL, story_id TEXT NOT NULL,
    source_sha TEXT NOT NULL, candidate_sha TEXT,
    integrated_at INTEGER NOT NULL,
    PRIMARY KEY (epic_id, story_id, source_sha)
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL, step_key TEXT, status TEXT NOT NULL,
    started_at INTEGER NOT NULL, ended_at INTEGER,
    outcome TEXT, metadata TEXT
);
"""


def _build_integration_scratch_db(
    db: Path,
    *,
    epic_id: str,
    member_id: str,
    source_sha: str,
    candidate_sha: str,
    dev_receipt_sha: str | None,
    membership: bool = True,
    member_status: str = "done",
) -> None:
    """Build a scratch DB with one epic + one member carrying integration data."""
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(_INTEGRATION_SCHEMA)
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, work_item_kind) "
            "VALUES (?, 'Epic', 'ready', 1, 'epic')",
            (epic_id,),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, "
            "work_item_kind, completed_at) "
            "VALUES (?, 'Done member', ?, 2, 'card', 3)",
            (member_id, member_status),
        )
        if membership:
            conn.execute(
                "INSERT INTO epic_memberships (epic_id, task_id, created_at) "
                "VALUES (?, ?, 4)",
                (epic_id, member_id),
            )
        conn.execute(
            "INSERT INTO epic_story_integrations "
            "(epic_id, story_id, source_sha, candidate_sha, integrated_at) "
            "VALUES (?, ?, ?, ?, 5)",
            (epic_id, member_id, source_sha, candidate_sha),
        )
        if dev_receipt_sha is not None:
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, step_key, status, started_at, ended_at, "
                "outcome, metadata) "
                "VALUES (?, 'development', 'completed', 6, 7, 'completed', ?)",
                (member_id, json.dumps(
                    {"source_completion_receipt": {"commit_sha": dev_receipt_sha}},
                )),
            )


def test_epic_release_checked_out_migration_blocker(
    product_fixture: _ProductFixture,
    tmp_path: Path,
) -> None:
    """v2-migrate refuses when an affected Epic branch is checked out."""
    fg = product_fixture.fake_git
    repo = product_fixture.repo
    initial_sha = product_fixture.initial_sha

    epic_id = "t_e1"
    epic_branch = _create_named_epic_branch(
        product_fixture, epic_id, initial_sha,
    )
    # Check out the epic branch in a worktree
    blocker_worktree = repo / ".worktrees" / "epic-checkout"
    fg.real(repo, "worktree", "add", str(blocker_worktree), epic_branch)

    scratch_db = tmp_path / "scratch-blocker.db"
    _build_integration_scratch_db(
        scratch_db,
        epic_id=epic_id,
        member_id="t_m1",
        source_sha=initial_sha,
        candidate_sha=initial_sha,
        dev_receipt_sha=initial_sha,
    )

    with pytest.raises(migration.MigrationBlocked, match="checked.out"):
        migration.audit_db(str(scratch_db), repo_root=str(repo))

    assert fg.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fg.push_invocations}"
    )


def test_epic_release_legacy_writer_grandfathering(
    product_fixture: _ProductFixture,
    tmp_path: Path,
) -> None:
    """A legacy-written done member with exact durable evidence is
    grandfathered as an integration fact during v2-migrate."""
    fg = product_fixture.fake_git
    repo = product_fixture.repo
    initial_sha = product_fixture.initial_sha

    epic_id = "t_e1"
    member_id = "t_m1"
    _create_named_epic_branch(product_fixture, epic_id, initial_sha)

    scratch_db = tmp_path / "scratch-legacy.db"
    _build_integration_scratch_db(
        scratch_db,
        epic_id=epic_id,
        member_id=member_id,
        source_sha=initial_sha,
        candidate_sha=initial_sha,
        dev_receipt_sha=initial_sha,
    )

    audit = migration.audit_db(str(scratch_db), repo_root=str(repo))
    assert any(
        g.get("task_id") == member_id and g.get("grandfathered") is True
        for g in audit.get("grandfathered", [])
    ), f"No grandfathered entry for {member_id} in {audit.get('grandfathered')}"

    applied = migration.apply_db(str(scratch_db), repo_root=str(repo))
    assert "verification" in applied

    verify = migration.verify_db(str(scratch_db))
    assert verify.get("counts", {}).get("needs_migration", 1) == 0, verify

    assert fg.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fg.push_invocations}"
    )


def test_epic_release_pruned_event_recovery(
    product_fixture: _ProductFixture,
) -> None:
    """After an event row is pruned, reconcile must not crash or
    double-integrate, and the release handoff invalidates the snapshot
    with drift evidence."""
    board = product_fixture.board
    fg = product_fixture.fake_git

    with kb.connect(board=board) as conn:
        epic_id = _create_epic(conn, "Epic: prune proof")
        story_id, worktree, branch = _create_epic_member(
            conn, product_fixture, board, epic_id,
        )
        _drive_story_to_review(conn, story_id, worktree, branch, board)
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert story_id in result.integrated

        snap = kb.prepare_epic_release_snapshot(conn, epic_id, board=board)
        assert snap.status == "awaiting_push"

        # --- Prune the aggregate verification event ---
        conn.execute(
            "DELETE FROM task_events WHERE id=?",
            (snap.aggregate_verification_event_id,),
        )

        # Reconcile again: no crash, no re-integration
        result2 = kb.reconcile(conn, board=board, spawn_ready=False)
        assert result2.integrated == []
        story = kb.get_task(conn, story_id)
        assert story is not None and story.status == "done"

        # Release handoff detects the pruned-event drift and invalidates
        with pytest.raises(kb.EpicReleaseHandoffError, match="snapshot_drifted"):
            kb.build_epic_release_handoff(conn, epic_id, board=board)

    assert fg.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fg.push_invocations}"
    )


def test_epic_integration_crash_recovery(
    product_fixture: _ProductFixture,
) -> None:
    """A dead-PID running card is reclaimed by reconcile; a recovered card
    is spawnable on the next pass (one action per pass, no storm)."""
    board = product_fixture.board
    fg = product_fixture.fake_git

    # A PID that is certainly dead.
    probe = subprocess.Popen(["true"])
    probe.wait(timeout=30)
    dead_pid = probe.pid

    with kb.connect(board=board) as conn:
        card_id = kb.create_task(
            conn, title="Crash recovery card", assignee="dev", board=board,
        )
        host_prefix = f"{kb._claimer_id().split(':', 1)[0]}:"
        with kb.authorized_governance_write(), kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', running=1, "
                "worker_pid=?, claim_lock=?, started_at=NULL "
                "WHERE id=?",
                (dead_pid, f"{host_prefix}dead", card_id),
            )

    with kb.connect(board=board) as conn:
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        assert card_id in result.reclaimed, (
            f"Expected dead-PID card reclaimed, got {result}"
        )
        task = kb.get_task(conn, card_id)
        assert task is not None and task.status == "ready"

        # Second pass (spawn_ready=True) picks the recovered card up.
        result2 = kb.reconcile(conn, board=board, spawn_ready=True)
        task2 = kb.get_task(conn, card_id)
        assert task2 is not None
        assert card_id in result2.spawned or task2.status == "running", (
            f"Recovered card not dispatched: {result2}"
        )

    assert fg.push_invocations == [], (
        f"ENGINE ISSUED GIT PUSH: {fg.push_invocations}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_api_module():
    """Load the kanban dashboard plugin API module for TestClient use."""
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "hermes_kanban_epic_api",
        repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_kanban_epic_api"] = module
    spec.loader.exec_module(module)
    return module
