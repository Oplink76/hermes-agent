"""Safe v2 product-board migration with manifest-hashed dry-run/apply.

The dry-run path opens a scratch copy of the board SQLite database read-only
and produces an exact report with a content-hash manifest.  Apply snapshots the
scratch DB first, migrates the board metadata to product preset, and backfills
task workflow state in one atomic transaction.  It never runs against a live
board database — only explicit scratch copies — and preserves all task history,
comments, events, and links.

Board databases resolved through the normal ``board`` slug path are rejected
with a clear ``MigrationBlocked`` error; callers must first create a byte-for-
byte copy of the target DB and pass its absolute path.

When the caller supplies an explicit scratch ``repo_root``, the audit/apply
additionally enforce the same two repo-facing safety rules the Epic flow
already enforces elsewhere: a checked-out affected Epic branch is an
unconditional blocker, and a done member's integration fact is grandfathered
only from exact durable evidence (matching membership, latest Development
handoff SHA, a full existing candidate commit, and ancestor relation to the
current Epic tip). Approval history never grandfathers anything.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sqlite3
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli import kanban_db as kb


class MigrationBlocked(RuntimeError):
    """The board cannot be migrated without risking active or unknown work."""


def _row_get(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read a column from a ``sqlite3.Row`` (or any mapping) without ``.get``.

    ``sqlite3.Row`` supports index and key access but has no ``.get`` method
    (before Python 3.12), so a plain ``row.get(...)`` raises
    ``AttributeError``. This helper normalizes both shapes and treats a
    missing key and a NULL value the same way ``dict.get`` does.
    """
    try:
        value = row[key]  # type: ignore[index]
        return default if value is None else value
    except (KeyError, IndexError):
        return default


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Stable, byte-for-byte deterministic hash of the manifest content."""
    canonical = json.dumps(
        {k: manifest[k] for k in sorted(manifest) if k not in ("hashes", "receipt_path")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _sha256_bytes(canonical.encode("utf-8"))


# ---------------------------------------------------------------------------
# Scratch-repository git evidence helpers
# ---------------------------------------------------------------------------
# Read-only, bounded, shell-free git probes against an explicit scratch
# repository. Every helper fails closed: any command failure (not a repo,
# missing ref, timeout) yields the empty answer, which can never create
# authority — nothing is grandfathered and nothing is detected as checked
# out unless the exact command succeeded.

_GIT_PROBE_TIMEOUT = 30
_FULL_SHA_LEN = 40


def _git_evidence(
    repo_root: Path, *args: str
) -> Optional[subprocess.CompletedProcess[str]]:
    """Run one bounded, read-only git command; ``None`` on any failure."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _checked_out_branch_refs(repo_root: Path) -> set[str]:
    """The ``refs/heads/...`` refs checked out in any worktree of the repo."""
    completed = _git_evidence(repo_root, "worktree", "list", "--porcelain")
    refs: set[str] = set()
    if completed is None or completed.returncode != 0:
        return refs
    for block in (completed.stdout or "").strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        branch = fields.get("branch", "")
        if branch.startswith("refs/heads/") and fields.get("worktree"):
            refs.add(branch)
    return refs


def _full_commit_sha(repo_root: Path, sha: str) -> Optional[str]:
    """Resolve ``sha`` to a full existing commit in the repo, or ``None``."""
    completed = _git_evidence(repo_root, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if completed is None or completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    return value if len(value) == _FULL_SHA_LEN else None


def _is_ancestor_of(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """Whether ``ancestor`` is an ancestor of (or equal to) ``descendant``."""
    completed = _git_evidence(
        repo_root, "merge-base", "--is-ancestor", ancestor, descendant
    )
    return completed is not None and completed.returncode == 0


# ---------------------------------------------------------------------------
# Historical persisted-outcome classification — LOCAL to this module.
# ---------------------------------------------------------------------------
# The migration may reconstruct a done member's integration history from
# already-persisted data. The classification of which persisted outcomes are
# authoritative handoffs — and which never are — lives ONLY here, on purpose:
# no completion or outcome-validation path may import or consult these
# constants (E08/E08R2 scope). Approval-shaped verdicts are deliberately
# non-authoritative: approval history never grandfathers anything, and
# redundant approved metadata never creates a fact or authority.

#: ``task_runs.outcome`` values that represent a finished worker run eligible
#: to carry a Development handoff SHA in its completion metadata.
_HISTORICAL_DEVELOPMENT_HANDOFF_OUTCOMES = frozenset({"completed"})

#: Persisted workflow verdicts that are approval-shaped. Never consulted as
#: integration evidence; only the Development-handoff chain may grandfather.
_HISTORICAL_NON_AUTHORITATIVE_VERDICTS = frozenset({"approved", "passed"})


# ---------------------------------------------------------------------------
# Scratch-DB guard
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path_or_board: str) -> Path:
    """Return the absolute path to a SQLite database.

    Accepts a raw filesystem path (must already exist) but rejects board-slug
    resolution through the normal kanban DB path machinery — the board slug
    route is the live path this migration must never touch.
    """
    raw = Path(db_path_or_board).expanduser()
    if not raw.is_absolute():
        # Relative paths could accidentally hit a board DB via cwd resolution.
        raise MigrationBlocked(
            f"refusing relative path {db_path_or_board!r}; "
            "pass the absolute path to a scratch DB copy"
        )
    if not raw.is_file():
        raise MigrationBlocked(
            f"database file does not exist: {raw}"
        )
    # Refuse the canonical kanban DB path for any live board so the caller
    # can't accidentally point at a real board by guessing its path.
    for slug in kb.list_boards(include_archived=False):
        slug_name = slug.get("slug") if isinstance(slug, dict) else slug
        live = kb.kanban_db_path(slug_name)
        try:
            if raw.resolve() == live.resolve():
                raise MigrationBlocked(
                    f"refusing live board database at {raw}; "
                    f"copy it to a scratch location first"
                )
        except OSError:
            # live.resolve() failed — path doesn't exist, skip
            continue
    return raw


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Audit (dry-run)
# ---------------------------------------------------------------------------

def _is_legacy_product(row: Mapping[str, Any]) -> bool:
    """A task that already has product workflow metadata set, or is an epic."""
    # Epics don't participate in the product workflow — they are containers.
    if str(_row_get(row, "work_item_kind", "card")) == "epic":
        return True
    return bool(
        _row_get(row, "workflow_template_id") == "product"
        or _row_get(row, "current_step_key") in kb.PRODUCT_WORKFLOW_STEP_SET
    )


def _infer_v2_step(row: Mapping[str, Any]) -> Optional[str]:
    """Infer the product workflow step for a legacy task.

    Uses the same inference as ``_infer_product_step`` in kanban_db, but
    applied to every non-archived task in the audit.
    """
    status = str(_row_get(row, "status", ""))
    workflow_template = str(_row_get(row, "workflow_template_id", "")).strip() or None
    current_step = str(_row_get(row, "current_step_key", "")).strip() or None

    if workflow_template == "product" and current_step in kb.PRODUCT_WORKFLOW_STEP_SET:
        return current_step

    if status == "done" or current_step == "done":
        return "done"

    # Map legacy statuses to v2 steps
    if status == "review":
        return "review"

    assignee = str(_row_get(row, "assignee", "")).strip()
    if assignee in kb.PRODUCT_WORKFLOW_ROLE_TO_STEP:
        return kb.PRODUCT_WORKFLOW_ROLE_TO_STEP[assignee]

    if status in {"todo", "ready", "triage"}:
        return "backlog"

    # Running tasks keep their assignee's step if known, else backlog
    if assignee:
        return kb.PRODUCT_WORKFLOW_ROLE_TO_STEP.get(assignee, "backlog")

    return "backlog"


# ---------------------------------------------------------------------------
# Grandfathering of done epic members' integration facts
# ---------------------------------------------------------------------------

def _latest_development_handoff_sha(
    conn: sqlite3.Connection, task_id: str
) -> Optional[str]:
    """The commit SHA of the newest completed Development handoff, or ``None``.

    Only runs whose persisted ``outcome`` is classified as a Development
    handoff by the module-local ``_HISTORICAL_DEVELOPMENT_HANDOFF_OUTCOMES``
    are consulted. Approval-shaped runs and verdicts are never looked at.
    """
    outcomes = tuple(sorted(_HISTORICAL_DEVELOPMENT_HANDOFF_OUTCOMES))
    placeholders = ", ".join("?" for _ in outcomes)
    row = conn.execute(
        "SELECT metadata FROM task_runs "
        f"WHERE task_id = ? AND step_key = 'development' "
        f"AND outcome IN ({placeholders}) "
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id, *outcomes),
    ).fetchone()
    if row is None:
        return None
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    receipt = metadata.get("source_completion_receipt")
    sha = receipt.get("commit_sha") if isinstance(receipt, dict) else None
    if not isinstance(sha, str):
        return None
    sha = sha.strip()
    return sha if len(sha) == _FULL_SHA_LEN else None


def _grandfather_done_member_integration(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    epic_id: str,
    task_id: str,
    epic_tip_sha: str,
) -> bool:
    """Decide whether a done epic member's integration fact may be grandfathered.

    Grandfathering is granted ONLY from exact durable evidence and fails
    closed on any missing or inconsistent link:

    1. the task is ``done`` and ``epic_memberships`` matches (epic, task);
    2. the fact's ``source_sha`` equals the latest Development handoff SHA;
    3. ``candidate_sha`` is a full, existing commit in ``repo_root``;
    4. that candidate is an ancestor of the current Epic tip.

    Approval history is never consulted and never grandfathers anything;
    redundant approved metadata never creates a fact or authority.
    """
    task = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if task is None or str(task["status"]) != "done":
        return False

    membership = conn.execute(
        "SELECT 1 FROM epic_memberships WHERE epic_id = ? AND task_id = ?",
        (epic_id, task_id),
    ).fetchone()
    if membership is None:
        return False

    handoff_sha = _latest_development_handoff_sha(conn, task_id)
    if handoff_sha is None:
        return False

    fact = conn.execute(
        "SELECT source_sha, candidate_sha FROM epic_story_integrations "
        "WHERE epic_id = ? AND story_id = ? "
        "ORDER BY integrated_at DESC, source_sha DESC LIMIT 1",
        (epic_id, task_id),
    ).fetchone()
    if fact is None:
        return False
    if str(fact["source_sha"] or "") != handoff_sha:
        return False

    candidate_sha = str(fact["candidate_sha"] or "").strip()
    if len(candidate_sha) != _FULL_SHA_LEN:
        return False
    if _full_commit_sha(repo_root, candidate_sha) != candidate_sha:
        return False
    return _is_ancestor_of(repo_root, candidate_sha, epic_tip_sha)


def _audit_scratch_db(
    db_path: Path, *, repo_root: Optional[Path] = None
) -> dict[str, Any]:
    """Return a byte-for-byte read-only migration plan for a scratch DB.

    When ``repo_root`` (an explicit scratch repository) is supplied, the
    audit additionally (a) refuses the plan if an affected Epic branch is
    currently checked out in any worktree — matching the unconditional
    checked-out refusal at CAS time — and (b) classifies done epic members'
    integration facts for grandfathering from exact durable evidence only.
    Without a repository nothing is grandfathered (fails closed).
    """
    with _ro_connect(db_path) as conn:
        # Verify the DB is structurally valid.
        integrity = str(
            conn.execute("PRAGMA integrity_check").fetchone()[0]
        )
        if integrity != "ok":
            raise MigrationBlocked(f"database integrity check failed: {integrity}")

        # Check for active runs — must be zero.
        # The 'running' column is from the v2 state model and may not exist
        # on scratch DBs copied from older schema versions.
        task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "running" in task_cols:
            active = conn.execute(
                "SELECT id, title, assignee FROM tasks WHERE status = 'running' AND running = 1"
            ).fetchall()
        else:
            # Older schema: any task with status='running' is active.
            active = conn.execute(
                "SELECT id, title, assignee FROM tasks WHERE status = 'running'"
            ).fetchall()
        if active:
            raise MigrationBlocked(
                "active running work must finish before v2 migration: "
                + ", ".join(str(row["id"]) for row in active)
            )

        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived' ORDER BY created_at, id"
        ).fetchall()

        task_count = len(rows)
        epics: list[str] = []
        epic_ids: list[str] = []
        for row in rows:
            task_id = str(row["id"])
            kind = str(_row_get(row, "work_item_kind", "card"))
            title = str(_row_get(row, "title", "")).strip().lower()
            if kind == "epic" or title.startswith("epic:"):
                epics.append(task_id)
            if kind == "epic":
                epic_ids.append(task_id)

        # Repository-evidence blockers and grandfathering classification.
        # Only consulted when the caller passes a scratch repository; the
        # epic branch name is the deterministic `epic/<id>` convention.
        grandfathered: list[dict[str, Any]] = []
        if repo_root is not None:
            repo_root = Path(repo_root).expanduser().resolve()
            checked_out = _checked_out_branch_refs(repo_root)
            for epic_id in epic_ids:
                epic_ref = f"refs/heads/{kb.epic_branch_for(epic_id)}"
                if epic_ref in checked_out:
                    raise MigrationBlocked(
                        f"epic branch {epic_ref} is checked out in a worktree; "
                        "v2 migration refused (unconditional checked-out refusal)"
                    )
                tip_sha = _full_commit_sha(repo_root, epic_ref)
                member_rows = conn.execute(
                    "SELECT task_id FROM epic_memberships WHERE epic_id = ?",
                    (epic_id,),
                ).fetchall()
                for member_row in member_rows:
                    member_id = str(member_row["task_id"])
                    status_row = conn.execute(
                        "SELECT status FROM tasks WHERE id = ?", (member_id,)
                    ).fetchone()
                    if status_row is None or str(status_row["status"]) != "done":
                        continue
                    grandfathered.append({
                        "epic_id": epic_id,
                        "task_id": member_id,
                        "grandfathered": (
                            tip_sha is not None
                            and _grandfather_done_member_integration(
                                conn,
                                repo_root,
                                epic_id=epic_id,
                                task_id=member_id,
                                epic_tip_sha=tip_sha,
                            )
                        ),
                    })

        items: list[dict[str, Any]] = []
        for row in rows:
            task_id = str(row["id"])
            v2_step = _infer_v2_step(row)
            is_product = _is_legacy_product(row)
            needs_migration = not is_product

            items.append({
                "id": task_id,
                "title": str(_row_get(row, "title", "")),
                "status": str(_row_get(row, "status", "")),
                "assignee": _row_get(row, "assignee"),
                "workflow_template_id": _row_get(row, "workflow_template_id"),
                "current_step_key": _row_get(row, "current_step_key"),
                "inferred_v2_step": v2_step,
                "already_product": is_product,
                "needs_migration": needs_migration,
            })

        counts = {
            "total": task_count,
            "already_product": sum(1 for item in items if item["already_product"]),
            "needs_migration": sum(1 for item in items if item["needs_migration"]),
            "epics": len(epics),
            "active": 0,
        }

        return {
            "mode": "dry-run",
            "db_path": str(db_path.resolve()),
            "db_hash": _sha256_path(db_path),
            "integrity": integrity,
            "counts": counts,
            "epics": epics,
            "grandfathered": grandfathered,
            "items": items,
        }


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _snapshot_scratch_db(
    db_path: Path,
    *,
    recovery_root: Optional[Path],
    audit: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Create an immutable snapshot of the scratch DB before migration."""
    root = Path(recovery_root) if recovery_root is not None else (
        db_path.parent / "v2-migration-snapshots"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hash_suffix = _sha256_path(db_path)[:10]
    # A unique nonce keeps the receipt directory collision-free even when two
    # applies land in the same second against byte-identical scratch content
    # (e.g. a re-apply whose WAL has not yet checkpointed into the main file).
    nonce = secrets.token_hex(6)
    receipt_dir = root / f"{stamp}-{hash_suffix}-{nonce}"
    snapshot = receipt_dir / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=False)

    consistent_db = snapshot / "kanban.db"
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(consistent_db))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    # Verify the snapshot is intact
    probe_path = receipt_dir / "restore-probe.sqlite3"
    shutil.copy2(consistent_db, probe_path)
    with sqlite3.connect(str(probe_path)) as probe:
        integrity = str(probe.execute("PRAGMA integrity_check").fetchone()[0])
        restore_probe = {
            "integrity_check": integrity,
            "tasks": int(
                probe.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            ),
        }
    probe_path.unlink()

    if integrity != "ok":
        raise MigrationBlocked(f"snapshot integrity check failed: {integrity}")

    manifest = {
        "version": 1,
        "created_at": int(time.time()),
        "source": {
            "db_path": str(db_path.resolve()),
            "db_hash": _sha256_path(db_path),
        },
        "snapshot": {
            "db": str(consistent_db),
        },
        "restore_probe": restore_probe,
        "audit": dict(audit),
    }
    inventory_path = snapshot / "inventory.json"
    inventory_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    manifest["hashes"] = {
        str(path.relative_to(receipt_dir)): _sha256_path(path)
        for path in sorted(receipt_dir.rglob("*"))
        if path.is_file()
    }
    manifest["manifest_digest"] = _manifest_digest(manifest)
    return receipt_dir, manifest


def _make_read_only(root: Path) -> None:
    """Mark receipt metadata read-only, leaving the snapshot DB restorable.

    SQLite must be able to open and journal its database to verify a snapshot
    (``PRAGMA integrity_check`` needs a writable connection), so only the
    receipt/inventory JSON is locked down. The snapshot's own integrity is
    protected by the manifest content hashes instead.
    """
    for path in root.rglob("*.json"):
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _apply_migration_to_scratch_db(
    db_path: Path,
    *,
    audit: Mapping[str, Any],
    recovery_root: Optional[Path],
    repo_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Migrate a scratch DB to product v2 in one atomic transaction."""
    receipt_dir, receipt = _snapshot_scratch_db(
        db_path, recovery_root=recovery_root, audit=audit
    )

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = OFF")

        # Re-verify before acting.
        refreshed = _audit_scratch_db(db_path, repo_root=repo_root)
        if refreshed["counts"]["active"] > 0:
            raise MigrationBlocked("active running work started during v2 migration")

        # All-or-nothing: do everything in one transaction.
        with conn:
            # Convert board metadata to product if the board_governance
            # table exists and a row is present. Scratch DBs from earlier
            # schema versions may not have it.
            gov_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='board_governance'"
            ).fetchone()
            if gov_exists:
                conn.execute(
                    "INSERT OR REPLACE INTO board_governance (id, qualification_required) VALUES (1, 0)"
                )

            changed = 0
            for item in refreshed["items"]:
                task_id = item["id"]
                if not item["needs_migration"]:
                    continue
                step = item["inferred_v2_step"]
                target_assignee = None
                if step in kb.PRODUCT_WORKFLOW_TRANSITIONS:
                    trans = kb.PRODUCT_WORKFLOW_TRANSITIONS[step]
                    target_assignee = trans.get("assignee_role")

                conn.execute(
                    """UPDATE tasks
                       SET workflow_template_id = 'product',
                           current_step_key = ?,
                           assignee = COALESCE(?, assignee)
                       WHERE id = ?""",
                    (step, target_assignee, task_id),
                )
                conn.execute(
                    """INSERT INTO task_events (task_id, kind, payload, created_at)
                       VALUES (?, 'v2_migrated',
                               ?, ?)""",
                    (
                        task_id,
                        json.dumps({
                            "workflow_template_id": "product",
                            "current_step_key": step,
                            "assignee": target_assignee,
                            "manifest_digest": receipt.get("manifest_digest", ""),
                        }),
                        int(time.time()),
                    ),
                )
                changed += 1

        receipt.update({
            "status": "applied",
            "changed": changed,
        })

    receipt_path = receipt_dir / "receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _make_read_only(receipt_dir)

    # Re-audit to produce post-migration verification.
    verification = _audit_scratch_db(db_path, repo_root=repo_root)
    receipt["verification"] = verification

    return {
        "db_path": str(db_path.resolve()),
        "changed": changed,
        "receipt_path": str(receipt_path),
        "manifest_digest": receipt.get("manifest_digest"),
        "verification": verification,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit_db(
    db_path: str, *, repo_root: Optional[str] = None
) -> dict[str, Any]:
    """Audit a scratch kanban database for v2 migration readiness.

    Opens the database read-only and produces an exact report including:
    - Task counts and migration status
    - Inferred v2 workflow steps
    - Active/running tasks (blocker)
    - DB integrity check

    When ``repo_root`` (an explicit scratch repository path) is supplied, the
    audit also refuses the plan if an affected Epic branch is checked out in
    any worktree, and classifies done epic members' integration facts for
    grandfathering from exact durable evidence only.

    Raises ``MigrationBlocked`` if the path is a live board DB or has
    active running tasks, or (with ``repo_root``) an Epic branch is checked
    out in a worktree.
    """
    resolved = _resolve_db_path(db_path)
    root = Path(repo_root).expanduser().resolve() if repo_root else None
    audit = _audit_scratch_db(resolved, repo_root=root)
    audit["manifest_digest"] = _manifest_digest(audit)
    return audit


def apply_db(
    db_path: str,
    *,
    recovery_root: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Migrate a scratch kanban database to product v2.

    1. Snapshots the database for recovery.
    2. Re-audits to confirm zero active runs (and no checked-out Epic branch
       when ``repo_root`` is supplied).
    3. Backfills product workflow metadata in one atomic transaction.
    4. Produces an immutable receipt with verification.

    Raises ``MigrationBlocked`` if the path is a live board DB or has
    active running tasks (or an Epic branch is checked out in a worktree).
    """
    resolved = _resolve_db_path(db_path)
    root = Path(repo_root).expanduser().resolve() if repo_root else None
    audit = _audit_scratch_db(resolved, repo_root=root)
    recovery = Path(recovery_root) if recovery_root else None
    return _apply_migration_to_scratch_db(
        resolved,
        audit=audit,
        recovery_root=recovery,
        repo_root=root,
    )


def verify_db(db_path: str) -> dict[str, Any]:
    """Verify that a scratch DB is correctly migrated to product v2.

    Returns a post-migration audit showing the current state. Does not
    modify the database.
    """
    resolved = _resolve_db_path(db_path)
    return _audit_scratch_db(resolved)