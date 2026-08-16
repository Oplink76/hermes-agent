"""Tests for hermes_cli.kanban_v2_migration — guarded scratch migration."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_v2_migration as migration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PRE_INTEGRATION_SQL = (
    Path(__file__).resolve().parent.parent
    / "fixtures" / "kanban" / "v2_migration" / "pre_integration.sql"
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same as test_kanban_db)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb.init_db()
    return home


@pytest.fixture
def pre_integration_db(tmp_path: Path) -> Path:
    """Create a scratch SQLite DB from the pre-integration fixture."""
    src = tmp_path / "kanban.db"
    with sqlite3.connect(str(src)) as conn:
        conn.executescript(_PRE_INTEGRATION_SQL.read_text(encoding="utf-8"))
    return src


@pytest.fixture
def empty_scratch_db(tmp_path: Path) -> Path:
    """Create an empty scratch SQLite DB with the kanban schema."""
    db = tmp_path / "empty.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,
                workflow_template_id TEXT, current_step_key TEXT,
                work_item_kind TEXT NOT NULL DEFAULT 'card',
                running INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                source_commit_required INTEGER NOT NULL DEFAULT 0,
                source_commit_forbidden INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE board_governance (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                qualification_required INTEGER NOT NULL DEFAULT 0
                                   CHECK (qualification_required IN (0, 1))
            )"""
        )
        conn.execute(
            "INSERT INTO board_governance (id, qualification_required) VALUES (1, 0)"
        )
    return db


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_rejects_relative_path() -> None:
    with pytest.raises(migration.MigrationBlocked, match="refusing relative path"):
        migration.audit_db("relative/path/to/db.sqlite")


def test_rejects_nonexistent_path() -> None:
    with pytest.raises(migration.MigrationBlocked, match="database file does not exist"):
        migration.audit_db("/nonexistent/path/to/kanban.db")


def test_rejects_live_board_db(kanban_home) -> None:
    """A path that matches a live board DB must be rejected."""
    from hermes_cli import kanban_db as kb
    # The kanban_home fixture creates a DB at the live path.
    live_path = kb.kanban_db_path(board="default")
    with pytest.raises(migration.MigrationBlocked, match="refusing live board"):
        migration.audit_db(str(live_path))


# ---------------------------------------------------------------------------
# Audit (dry-run)
# ---------------------------------------------------------------------------


def test_audit_pre_integration(pre_integration_db: Path) -> None:
    result = migration.audit_db(str(pre_integration_db))

    assert result["mode"] == "dry-run"
    assert "manifest_digest" in result
    assert len(result["manifest_digest"]) == 64  # SHA-256 hex

    # Integrity must pass
    assert result["integrity"] == "ok"

    counts = result["counts"]
    assert counts["total"] == 8  # 7 tasks + 1 epic
    assert counts["already_product"] == 2  # t_004 + epic t_e8
    assert counts["needs_migration"] == 6
    assert counts["epics"] == 1  # t_e8

    # Verify specific items
    items = {item["id"]: item for item in result["items"]}

    # t_004 is already a product task
    assert items["t_004"]["already_product"] is True
    assert not items["t_004"]["needs_migration"]

    # Legacy tasks need migration
    assert items["t_001"]["needs_migration"]
    assert items["t_001"]["inferred_v2_step"] == "development"  # assignee=developer

    assert items["t_002"]["inferred_v2_step"] == "architecture"  # assignee=architect
    assert items["t_003"]["inferred_v2_step"] == "review"  # status=review
    assert items["t_007"]["inferred_v2_step"] == "test"  # assignee=tester

    # t_005 has no assignee, status todo → backlog
    assert items["t_005"]["inferred_v2_step"] == "backlog"
    assert items["t_005"]["needs_migration"]

    # t_006 is done → done
    assert items["t_006"]["inferred_v2_step"] == "done"

    # Epic should be detected
    assert "t_e8" in result["epics"]


def test_audit_empty_db(empty_scratch_db: Path) -> None:
    result = migration.audit_db(str(empty_scratch_db))

    assert result["counts"]["total"] == 0
    assert result["counts"]["needs_migration"] == 0
    assert result["integrity"] == "ok"
    assert len(result["items"]) == 0


def test_audit_produces_stable_manifest(pre_integration_db: Path) -> None:
    """Repeated audits of the same DB must produce identical manifest digests."""
    first = migration.audit_db(str(pre_integration_db))
    second = migration.audit_db(str(pre_integration_db))

    assert first["manifest_digest"] == second["manifest_digest"]
    assert first["counts"] == second["counts"]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_pre_integration(pre_integration_db: Path, tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    result = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )

    assert result["changed"] == 6  # 6 non-product non-epic tasks migrated
    assert result["receipt_path"]
    assert result["manifest_digest"]

    # Receipt must exist and be readable
    receipt_path = Path(result["receipt_path"])
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "applied"
    assert receipt["changed"] == 6

    # Post-migration audit: all tasks should now be product
    post = migration.audit_db(str(pre_integration_db))
    assert post["counts"]["already_product"] == 8  # All 8 now
    assert post["counts"]["needs_migration"] == 0


def test_apply_idempotent(pre_integration_db: Path, tmp_path: Path) -> None:
    """Applying twice must be safe — second apply changes nothing."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    first = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )
    assert first["changed"] == 6

    second = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )
    assert second["changed"] == 0  # Idempotent — nothing to change


def test_apply_preserves_history(pre_integration_db: Path, tmp_path: Path) -> None:
    """Comments and events from pre-migration must survive."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))

    with sqlite3.connect(str(pre_integration_db)) as conn:
        conn.row_factory = sqlite3.Row

        # Comments survive
        comments = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", ("t_001",)
        ).fetchall()
        assert len(comments) == 2
        assert comments[0]["body"] == "I can reproduce this on staging — the timeout is exactly 30s."
        assert comments[1]["author"] == "bob"

        # Events survive
        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", ("t_001",)
        ).fetchall()
        assert len(events) >= 3  # created + assigned + v2_migrated
        kinds = {e["kind"] for e in events}
        assert "created" in kinds
        assert "assigned" in kinds
        assert "v2_migrated" in kinds

        # t_004 (already product) must NOT get a v2_migrated event
        events_t4 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", ("t_004",)
        ).fetchall()
        kinds_t4 = {e["kind"] for e in events_t4}
        assert "v2_migrated" not in kinds_t4


def test_apply_task_workflow_metadata(pre_integration_db: Path, tmp_path: Path) -> None:
    """Migrated tasks must have correct workflow_template_id and step."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))

    with sqlite3.connect(str(pre_integration_db)) as conn:
        conn.row_factory = sqlite3.Row

        # t_001: developer → development
        t1 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_001",)).fetchone()
        assert t1["workflow_template_id"] == "product"
        assert t1["current_step_key"] == "development"

        # t_002: architect → architecture
        t2 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_002",)).fetchone()
        assert t2["workflow_template_id"] == "product"
        assert t2["current_step_key"] == "architecture"

        # t_003: review → review
        t3 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_003",)).fetchone()
        assert t3["workflow_template_id"] == "product"
        assert t3["current_step_key"] == "review"

        # t_004: already product — unchanged
        t4 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_004",)).fetchone()
        assert t4["workflow_template_id"] == "product"
        assert t4["current_step_key"] == "development"

        # t_005: no assignee → backlog
        t5 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_005",)).fetchone()
        assert t5["workflow_template_id"] == "product"
        assert t5["current_step_key"] == "backlog"

        # t_006: done → done (release_measure)
        t6 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_006",)).fetchone()
        assert t6["workflow_template_id"] == "product"
        assert t6["current_step_key"] == "done"

        # t_007: tester → test
        t7 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_007",)).fetchone()
        assert t7["workflow_template_id"] == "product"
        assert t7["current_step_key"] == "test"

        # Epic is untouched — epics are skipped by the migration
        t8 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_e8",)).fetchone()
        assert t8["workflow_template_id"] is None  # Epics not migrated


def test_apply_rejects_active_run(tmp_path: Path) -> None:
    """Apply must fail if the DB has an active running task."""
    db = tmp_path / "active.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,
                workflow_template_id TEXT, current_step_key TEXT,
                work_item_kind TEXT NOT NULL DEFAULT 'card',
                running INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                source_commit_required INTEGER NOT NULL DEFAULT 0,
                source_commit_forbidden INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE board_governance (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                qualification_required INTEGER NOT NULL DEFAULT 0
                                   CHECK (qualification_required IN (0, 1))
            )"""
        )
        conn.execute("INSERT INTO board_governance (id, qualification_required) VALUES (1, 0)")
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, running) "
            "VALUES ('t_run', 'Running task', 'developer', 'running', 1, 1)"
        )

    with pytest.raises(migration.MigrationBlocked, match="active running"):
        migration.apply_db(str(db), recovery_root=str(tmp_path / "recovery"))


def test_apply_zero_change_on_rerun(pre_integration_db: Path, tmp_path: Path) -> None:
    """After apply, re-running the audit must show zero needs_migration."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))

    # Re-audit: nothing left to migrate
    post = migration.audit_db(str(pre_integration_db))
    assert post["counts"]["needs_migration"] == 0

    # Re-apply: zero change
    second = migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))
    assert second["changed"] == 0


def test_verify_pre_integration(pre_integration_db: Path) -> None:
    """verify_db returns an audit without modifying the DB."""
    before = migration.audit_db(str(pre_integration_db))
    verification = migration.verify_db(str(pre_integration_db))

    # verify_db is just an audit alias
    assert verification["counts"] == before["counts"]
    assert verification["integrity"] == "ok"


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def test_snapshot_is_restorable(pre_integration_db: Path, tmp_path: Path) -> None:
    """The snapshot DB created during apply must pass integrity check."""
    recovery = tmp_path / "recovery_snap"
    recovery.mkdir()

    result = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )

    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    snapshot_db = Path(receipt["snapshot"]["db"])
    assert snapshot_db.is_file()

    # Verify the snapshot itself
    with sqlite3.connect(str(snapshot_db)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        assert integrity == "ok"
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 8  # Same number as the original


# ---------------------------------------------------------------------------
# API-level dry-run via the audit function exercised above.
# apply/verify/idempotent/zero-change/snapshot tests cover the full
# lifecycle without subprocess dependency.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# E08R2 spec behaviours: grandfathering, checked-out blocker, and local
# historical persisted-outcome classification. Scratch DB + scratch repo only.
# ---------------------------------------------------------------------------

_INTEGRATION_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
    assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,
    completed_at INTEGER,
    workflow_template_id TEXT, current_step_key TEXT,
    work_item_kind TEXT NOT NULL DEFAULT 'card',
    running INTEGER NOT NULL DEFAULT 0
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


def _init_scratch_repo(repo: Path) -> str:
    """Create a scratch git repo on ``main``; return the init commit SHA."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "migration@example.com"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Migration Test"],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True, text=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    """Write + commit a file on the current branch; return the new SHA."""
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", name],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True, capture_output=True, text=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _build_integration_db(
    db: Path,
    *,
    epic_id: str = "t_e1",
    member_id: str = "t_m1",
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
            "INSERT INTO tasks (id, title, status, created_at, work_item_kind, completed_at) "
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
                "(task_id, step_key, status, started_at, ended_at, outcome, metadata) "
                "VALUES (?, 'development', 'completed', 6, 7, 'completed', ?)",
                (member_id, json.dumps(
                    {"source_completion_receipt": {"commit_sha": dev_receipt_sha}}
                )),
            )


def test_grandfather_done_member_integration_fact(tmp_path: Path) -> None:
    """A done member with exact durable evidence is grandfathered as integrated."""
    repo = tmp_path / "repo"
    base_sha = _init_scratch_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "epic/t_e1"],
        check=True, capture_output=True, text=True,
    )
    _commit_file(repo, "epic.txt", "tip\n", "epic tip")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True, capture_output=True, text=True,
    )

    db = tmp_path / "integration.db"
    _build_integration_db(
        db,
        source_sha=base_sha,
        candidate_sha=base_sha,
        dev_receipt_sha=base_sha,
    )

    result = migration.audit_db(str(db), repo_root=str(repo))
    assert result["grandfathered"] == [
        {"epic_id": "t_e1", "task_id": "t_m1", "grandfathered": True}
    ]


def test_grandfather_requires_exact_durable_evidence(tmp_path: Path) -> None:
    """Grandfathering fails closed on any missing or inconsistent evidence link."""
    repo = tmp_path / "repo"
    base_sha = _init_scratch_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "epic/t_e1"],
        check=True, capture_output=True, text=True,
    )
    _commit_file(repo, "epic.txt", "tip\n", "epic tip")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True, capture_output=True, text=True,
    )
    # A commit on main that is NOT an ancestor of the epic tip.
    foreign_sha = _commit_file(repo, "foreign.txt", "foreign\n", "foreign")

    def is_grandfathered(db: Path) -> bool:
        entries = migration.audit_db(str(db), repo_root=str(repo))["grandfathered"]
        return entries == [{"epic_id": "t_e1", "task_id": "t_m1", "grandfathered": True}]

    # Membership mismatch.
    db = tmp_path / "no_membership.db"
    _build_integration_db(
        db, source_sha=base_sha, candidate_sha=base_sha,
        dev_receipt_sha=base_sha, membership=False,
    )
    assert not is_grandfathered(db)

    # source_sha != latest Development handoff SHA.
    db = tmp_path / "stale_source.db"
    _build_integration_db(
        db, source_sha="0" * 40, candidate_sha=base_sha, dev_receipt_sha=base_sha,
    )
    assert not is_grandfathered(db)

    # candidate_sha is not a full existing commit.
    db = tmp_path / "foreign_candidate.db"
    _build_integration_db(
        db, source_sha=base_sha, candidate_sha="1" * 40, dev_receipt_sha=base_sha,
    )
    assert not is_grandfathered(db)

    # candidate is not an ancestor of the current Epic tip.
    db = tmp_path / "non_ancestor.db"
    _build_integration_db(
        db, source_sha=foreign_sha, candidate_sha=foreign_sha,
        dev_receipt_sha=foreign_sha,
    )
    assert not is_grandfathered(db)


def test_grandfather_never_from_approval_history(tmp_path: Path) -> None:
    """Approval history and redundant approved metadata never grandfather a fact."""
    repo = tmp_path / "repo"
    base_sha = _init_scratch_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "epic/t_e1"],
        check=True, capture_output=True, text=True,
    )
    _commit_file(repo, "epic.txt", "tip\n", "epic tip")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True, capture_output=True, text=True,
    )

    # A matching fact row exists, but there is NO Development handoff — only a
    # review approval carrying candidate metadata. Approval never grandfathers.
    db = tmp_path / "approval_only.db"
    _build_integration_db(
        db, source_sha=base_sha, candidate_sha=base_sha, dev_receipt_sha=None,
    )
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, started_at, ended_at, outcome, metadata) "
            "VALUES ('t_m1', 'review', 'completed', 6, 7, 'completed', ?)",
            (json.dumps({"workflow_outcome": {"verdict": "approved"},
                         "candidate_sha": base_sha}),),
        )
    entries = migration.audit_db(str(db), repo_root=str(repo))["grandfathered"]
    assert entries == [{"epic_id": "t_e1", "task_id": "t_m1", "grandfathered": False}]

    # A Development run exists but carries no handoff receipt, while a review
    # run's approved metadata is present. Redundant approved metadata creates
    # no fact and no authority.
    db2 = tmp_path / "approved_no_receipt.db"
    _build_integration_db(
        db2, source_sha=base_sha, candidate_sha=base_sha, dev_receipt_sha=None,
    )
    with sqlite3.connect(str(db2)) as conn:
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, started_at, ended_at, outcome, metadata) "
            "VALUES ('t_m1', 'development', 'completed', 6, 7, 'completed', '{}')"
        )
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, started_at, ended_at, outcome, metadata) "
            "VALUES ('t_m1', 'review', 'completed', 8, 9, 'completed', ?)",
            (json.dumps({"workflow_outcome": {"verdict": "approved"},
                         "candidate_sha": base_sha}),),
        )
    entries2 = migration.audit_db(str(db2), repo_root=str(repo))["grandfathered"]
    assert entries2 == [{"epic_id": "t_e1", "task_id": "t_m1", "grandfathered": False}]


def test_checked_out_epic_branch_reported_as_blocker(tmp_path: Path) -> None:
    """A checked-out affected Epic branch blocks the dry-run unconditionally."""
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    # Leave the epic branch checked out in the main worktree — clean, but the
    # refusal is unconditional (matching the CAS-time checked-out refusal).
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "epic/t_e1"],
        check=True, capture_output=True, text=True,
    )

    db = tmp_path / "checked_out.db"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(_INTEGRATION_SCHEMA)
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, work_item_kind) "
            "VALUES ('t_e1', 'Epic', 'ready', 1, 'epic')"
        )

    with pytest.raises(migration.MigrationBlocked, match="checked out"):
        migration.audit_db(str(db), repo_root=str(repo))


def test_historical_persisted_outcome_classification_stays_local() -> None:
    """The persisted-outcome classification lives only in the migration module."""
    from hermes_cli import kanban_db as kb

    # The classification contract: only a completed Development handoff is
    # authoritative; approval-shaped verdicts are non-authoritative.
    assert migration._HISTORICAL_DEVELOPMENT_HANDOFF_OUTCOMES == frozenset({"completed"})
    assert "approved" in migration._HISTORICAL_NON_AUTHORITATIVE_VERDICTS
    assert "passed" in migration._HISTORICAL_NON_AUTHORITATIVE_VERDICTS

    # No completion or outcome-validation path may import or consult it.
    for symbol in (
        "_HISTORICAL_DEVELOPMENT_HANDOFF_OUTCOMES",
        "_HISTORICAL_NON_AUTHORITATIVE_VERDICTS",
    ):
        assert not hasattr(kb, symbol), f"kanban_db must not expose {symbol}"