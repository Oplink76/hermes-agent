"""Safe legacy-board migration into strict qualification governance.

The dry-run path opens SQLite read-only.  Apply snapshots the board first,
backfills only evidence-derived contracts in one board-local transaction, then
atomically enables the existing qualification policy.  It never recreates a
task, so task ids and operational history remain intact.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_default_hermes_root
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_intake


class MigrationBlocked(RuntimeError):
    """The board cannot be migrated without risking active or unknown work."""


_TERMINAL_STATUSES = {"done", "archived"}
_NEXT = {
    "backlog": ("architecture", "architect"),
    "architecture": ("development", "developer"),
    "development": ("test", "tester"),
    "test": ("review", "reviewer"),
    "review": ("release_measure", None),
    "release_measure": ("done", None),
}


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _explicit_epic(row: Mapping[str, Any]) -> bool:
    return str(row["work_item_kind"] or "card") == "epic" or str(
        row["title"] or ""
    ).strip().lower().startswith("epic:")


def _work_type(title: str, body: Optional[str]) -> str:
    evidence = f"{title}\n{body or ''}".lower()
    if any(token in evidence for token in (" bug", "bug:", " fix ", "defect")):
        return "bug"
    if any(
        token in evidence
        for token in ("maintenance", "refactor", "upgrade", "update ", "docs", "documentation")
    ):
        return "maintenance"
    if any(
        token in evidence
        for token in ("operations", " ops ", "runbook", "gateway", "cron", "backup", "restore", "migration", "sync")
    ):
        return "ops"
    if any(token in evidence for token in ("spike", "investigate", "research", "explore")):
        return "spike"
    return "story"


def _first_outcome(title: str, body: Optional[str]) -> str:
    for raw in str(body or "").splitlines():
        line = raw.strip().lstrip("#-* ").strip()
        if line and not line.lower().endswith(("criteria", "scope", "context")):
            return line[:1000]
    return title.strip()


def _phase_and_assignee(
    row: Mapping[str, Any], phase_assignees: Mapping[str, Any]
) -> tuple[Optional[str], Optional[str]]:
    if _explicit_epic(row):
        return None, None
    status = str(row["status"] or "")
    phase = str(row["current_step_key"] or "").strip()
    assignee = str(row["assignee"] or "").strip()
    if status == "done" or phase == "done":
        return "release_measure", None
    if phase in phase_assignees:
        return phase, phase_assignees[phase]
    for candidate, expected in phase_assignees.items():
        if expected and assignee == str(expected):
            return str(candidate), str(expected)
    if status == "review":
        return "review", phase_assignees.get("review")
    # Missing evidence never earns a late entry.  Backlog is the safe path.
    return "backlog", phase_assignees.get("backlog")


def _routing_relations(
    task_id: str,
    *,
    links: set[tuple[str, str]],
    explicit_epics: set[str],
) -> tuple[str, Optional[str], list[str], bool]:
    if task_id in explicit_epics:
        return "explicit_epic", None, [], False
    epic_parents = sorted(
        parent
        for parent, child in links
        if child == task_id and parent in explicit_epics
    )
    dependencies = sorted(
        parent
        for parent, child in links
        if child == task_id and parent not in explicit_epics
    )
    ambiguous = len(epic_parents) > 1
    if ambiguous:
        relation = "ambiguous_epic_membership"
    elif epic_parents:
        relation = "epic_membership"
    elif dependencies or any(parent == task_id for parent, _child in links):
        relation = "dependency"
    else:
        relation = "standalone"
    return relation, epic_parents[0] if len(epic_parents) == 1 else None, dependencies, ambiguous


def _audit_from_connection(
    conn: sqlite3.Connection, *, board: str, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    if str(metadata.get("preset") or "").lower() != "product":
        raise MigrationBlocked(f"board {board!r} is not a product board")
    policy = metadata.get("qualification")
    policy = policy if isinstance(policy, Mapping) else kb.PRODUCT_QUALIFICATION_DEFAULTS
    phase_assignees = policy.get("phase_assignees")
    if not isinstance(phase_assignees, Mapping):
        phase_assignees = kb.PRODUCT_QUALIFICATION_DEFAULTS["phase_assignees"]

    rows = conn.execute(
        "SELECT * FROM tasks WHERE status != 'archived' ORDER BY created_at, id"
    ).fetchall()
    links = {
        (str(row["parent_id"]), str(row["child_id"]))
        for row in conn.execute("SELECT parent_id, child_id FROM task_links")
    }
    explicit_epics = {str(row["id"]) for row in rows if _explicit_epic(row)}
    items: list[dict[str, Any]] = []
    active: list[str] = []
    for row in rows:
        task_id = str(row["id"])
        running = bool(row["running"]) or str(row["status"]) == "running"
        if row["current_run_id"] is not None:
            run = conn.execute(
                "SELECT status FROM task_runs WHERE id = ?", (row["current_run_id"],)
            ).fetchone()
            running = running or bool(run and str(run["status"]) == "running")
        phase, assignee = _phase_and_assignee(row, phase_assignees)
        relation, epic_id, dependencies, ambiguous = _routing_relations(
            task_id, links=links, explicit_epics=explicit_epics
        )
        if running:
            result = "running"
            active.append(task_id)
        elif ambiguous:
            result = "ambiguous"
        elif row["work_contract_id"]:
            result = "already_contracted"
        else:
            result = "safe"
        missing: list[str] = []
        if not str(row["body"] or "").strip():
            missing.append("body/outcome")
        items.append(
            {
                "id": task_id,
                "title": str(row["title"]),
                "result": result,
                "current": {
                    "status": row["status"],
                    "phase": row["current_step_key"],
                    "assignee": row["assignee"],
                    "workflow_template_id": row["workflow_template_id"],
                },
                "proposed": {
                    "qualification_path": "hermes",
                    "work_item_kind": "epic" if task_id in explicit_epics else "card",
                    "work_type": _work_type(str(row["title"]), row["body"]),
                    "entry_phase": phase,
                    "assignee": assignee,
                    "epic_id": epic_id,
                    "dependencies": dependencies,
                },
                "relations": relation,
                "missing_evidence": missing,
            }
        )
    return {
        "board": board,
        "mode": "dry-run",
        "qualification_required": bool(policy.get("required") is True),
        "strict_ready": not active and not any(
            item["result"] == "ambiguous" for item in items
        ),
        "active_running": active,
        "counts": {
            "items": len(items),
            "safe": sum(item["result"] == "safe" for item in items),
            "already_contracted": sum(
                item["result"] == "already_contracted" for item in items
            ),
            "running": len(active),
            "ambiguous": sum(item["result"] == "ambiguous" for item in items),
        },
        "items": items,
    }


def audit_board(board: str) -> dict[str, Any]:
    """Return a byte-for-byte read-only migration plan for one product board."""

    metadata = kb.read_board_metadata(board)
    path = kb.kanban_db_path(board)
    if not path.is_file():
        raise MigrationBlocked(f"board database does not exist: {path}")
    with _ro_connect(path) as conn:
        return _audit_from_connection(conn, board=board, metadata=metadata)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_if_file(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def _snapshot_board(
    board: str,
    *,
    recovery_root: Optional[Path],
    audit: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    db_path = kb.kanban_db_path(board)
    metadata_path = kb.board_metadata_path(board)
    attachments_path = kb.attachments_root(board)
    key_path = get_default_hermes_root() / kanban_intake.SIGNING_KEY_RELATIVE_PATH
    root = Path(recovery_root) if recovery_root is not None else (
        get_default_hermes_root() / "recovery" / "qualification-migrations"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = hashlib.sha256(f"{board}:{time.time_ns()}".encode()).hexdigest()[:10]
    receipt_dir = root / f"{stamp}-{board}-{suffix}"
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

    raw_files: dict[str, bool] = {}
    for suffix_name in ("", "-wal", "-shm"):
        source_path = Path(str(db_path) + suffix_name)
        raw_files[source_path.name] = _copy_if_file(
            source_path, snapshot / "raw" / source_path.name
        )
    metadata_exists = _copy_if_file(metadata_path, snapshot / "board.json")
    key_exists = _copy_if_file(key_path, snapshot / "work_contract_signing.key")
    attachments_exists = attachments_path.is_dir()
    if attachments_exists:
        shutil.copytree(attachments_path, snapshot / "attachments")

    probe_path = receipt_dir / "restore-probe.sqlite3"
    shutil.copy2(consistent_db, probe_path)
    with sqlite3.connect(str(probe_path)) as probe:
        integrity = str(probe.execute("PRAGMA integrity_check").fetchone()[0])
        restore_probe = {
            "integrity_check": integrity,
            "tasks": int(probe.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
            "contracts": int(
                probe.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0]
            ),
            "epics": int(
                probe.execute(
                    "SELECT COUNT(*) FROM tasks WHERE work_item_kind = 'epic'"
                ).fetchone()[0]
            ),
            "project_links": int(
                probe.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id IS NOT NULL"
                ).fetchone()[0]
            ),
        }
    probe_path.unlink()
    if integrity != "ok":
        raise MigrationBlocked(f"snapshot integrity check failed: {integrity}")

    manifest = {
        "version": 1,
        "board": board,
        "created_at": int(time.time()),
        "live": {
            "db": str(db_path),
            "metadata": str(metadata_path),
            "attachments": str(attachments_path),
            "signing_key": str(key_path),
        },
        "snapshot": {
            "db": str(consistent_db),
            "metadata": str(snapshot / "board.json") if metadata_exists else None,
            "attachments": str(snapshot / "attachments") if attachments_exists else None,
            "signing_key": str(snapshot / "work_contract_signing.key") if key_exists else None,
            "raw_files": raw_files,
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
        str(path.relative_to(receipt_dir)): _sha256(path)
        for path in sorted(receipt_dir.rglob("*"))
        if path.is_file()
    }
    return receipt_dir, manifest


def _intake_id(board: str, task_id: str) -> str:
    digest = hashlib.sha256(f"{board}:{task_id}".encode("utf-8")).hexdigest()
    return f"qi_migrate_{digest[:20]}"


def _legacy_contract(
    row: Mapping[str, Any],
    *,
    board: str,
    proposed: Mapping[str, Any],
    intake_id: str,
) -> dict[str, Any]:
    phase = proposed["entry_phase"]
    next_phase, next_role = _NEXT.get(str(phase), (None, None)) if phase else (None, None)
    work_type = str(proposed["work_type"])
    item_kind = str(proposed["work_item_kind"])
    if item_kind == "epic":
        phase = None
        next_phase = None
        next_role = None
    return {
        "version": 1,
        "policy_version": kanban_intake.DEFAULT_POLICY_VERSION,
        "qualification_path": "hermes",
        "request_id": intake_id,
        "work": {
            "item_kind": item_kind,
            "work_type": work_type,
            "title": str(row["title"]),
            "outcome": _first_outcome(str(row["title"]), row["body"]),
            "scope": [str(row["title"])],
            "out_of_scope": [],
        },
        "routing": {
            "entry_phase": phase,
            "assignee": proposed["assignee"] if item_kind == "card" else None,
            "epic_id": proposed["epic_id"] if item_kind == "card" else None,
            "dependencies": proposed["dependencies"] if item_kind == "card" else [],
        },
        "entry_assessment": {
            "reason": "Legacy card predates qualification; existing task evidence and board position were preserved.",
            "skipped_phases": [],
            "evidence": [f"legacy-task:{row['id']}"],
        },
        "handover": {
            "deliverables": [str(row["title"])],
            "required_evidence": ["Existing acceptance criteria and phase evidence"],
            "done_when": ["Acceptance criteria pass and required board gates are recorded"],
            "next_phase": next_phase,
            "next_role": next_role,
        },
        "rules": {
            "allowed": ["Work within the card scope and repository instructions"],
            "forbidden": [
                "Bypass Test, independent Review, or release evidence",
                "Edit phase, assignee, Epic membership, or dependencies outside Hermes",
            ],
        },
        "classification": [
            f"framework:{work_type}", "path:hermes", "source:legacy_migration",
            f"board:{board}",
        ],
        "issuer": {
            "profile": "hermes-migration",
            "run_id": None,
            "issued_at": int(row["created_at"] or time.time()),
        },
    }


def _write_metadata_strict(board: str) -> dict[str, Any]:
    path = kb.board_metadata_path(board)
    raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    existing = raw.get("qualification")
    existing = existing if isinstance(existing, dict) else {}
    qualification = {**kb.PRODUCT_QUALIFICATION_DEFAULTS, **existing}
    phase = existing.get("phase_assignees")
    qualification["phase_assignees"] = {
        **kb.PRODUCT_QUALIFICATION_DEFAULTS["phase_assignees"],
        **(phase if isinstance(phase, dict) else {}),
    }
    qualification["paths"] = ["po", "hermes"]
    qualification["work_types"] = list(kb.PRODUCT_QUALIFICATION_DEFAULTS["work_types"])
    qualification["required"] = True
    raw["qualification"] = qualification
    tmp = path.with_name(path.name + ".qualification-migrate.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return raw


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR)


@contextlib.contextmanager
def _quiescent_board(board: str):
    with kb._dispatch_tick_lock(kb.kanban_db_path(board)) as held:
        if not held:
            raise MigrationBlocked(
                f"board {board!r} has an active dispatch tick; retry migration"
            )
        yield


def apply_board(
    board: str, *, recovery_root: Optional[Path] = None
) -> dict[str, Any]:
    """Backfill one quiescent product board and atomically enable strict mode."""

    with _quiescent_board(board):
        return _apply_board_quiescent(board, recovery_root=recovery_root)


def _apply_board_quiescent(
    board: str, *, recovery_root: Optional[Path] = None
) -> dict[str, Any]:

    audit = audit_board(board)
    if audit["active_running"]:
        raise MigrationBlocked(
            "active running work must finish before qualification migration: "
            + ", ".join(audit["active_running"])
        )
    ambiguous = [item["id"] for item in audit["items"] if item["result"] == "ambiguous"]
    if ambiguous:
        raise MigrationBlocked(
            "ambiguous legacy Epic membership requires Hermes qualification: "
            + ", ".join(ambiguous)
        )
    receipt_dir, receipt = _snapshot_board(
        board, recovery_root=recovery_root, audit=audit
    )
    changed = 0
    with kb.connect(board=board) as conn:
        with kb.write_txn(conn):
            refreshed = _audit_from_connection(
                conn, board=board, metadata=kb.read_board_metadata(board)
            )
            if refreshed["active_running"]:
                raise MigrationBlocked(
                    "active running work started during qualification migration"
                )
            by_id = {item["id"]: item for item in refreshed["items"]}
            rows = {
                str(row["id"]): row
                for row in conn.execute(
                    "SELECT * FROM tasks WHERE status != 'archived' ORDER BY created_at, id"
                )
            }
            explicit_epics = {
                task_id for task_id, item in by_id.items()
                if item["proposed"]["work_item_kind"] == "epic"
            }
            for task_id, item in by_id.items():
                row = rows[task_id]
                if item["result"] == "already_contracted":
                    continue
                intake_id = _intake_id(board, task_id)
                raw_request = json.dumps(
                    {
                        "kind": "legacy_qualification_migration",
                        "board": board,
                        "legacy_task_id": task_id,
                        "request": {"title": row["title"], "body": row["body"]},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                now = int(row["created_at"] or time.time())
                conn.execute(
                    """
                    INSERT OR IGNORE INTO qualification_intake (
                        id, raw_request, source, session_id, attachments_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, 'hermes-migration', NULL, '[]', 'pending', ?, ?)
                    """,
                    (intake_id, raw_request, now, now),
                )
                contract = _legacy_contract(
                    row, board=board, proposed=item["proposed"], intake_id=intake_id
                )
                signed = kanban_intake.sign_work_contract(contract)
                contract_id = kb.store_work_contract(
                    conn, signed, created_at=now
                )
                latest = conn.execute(
                    "SELECT decision, contract_id FROM qualification_intake_decisions "
                    "WHERE intake_id = ? ORDER BY id DESC LIMIT 1",
                    (intake_id,),
                ).fetchone()
                if not latest or latest["decision"] != "qualified" or latest["contract_id"] != contract_id:
                    kb.record_qualification_decision(
                        conn,
                        intake_id=intake_id,
                        decision="qualified",
                        actor_profile="hermes-migration",
                        reason="Evidence-preserving legacy backfill before strict activation",
                        contract_id=contract_id,
                        created_at=now,
                    )
                epic = task_id in explicit_epics
                updates = ["work_contract_id = ?", "work_item_kind = ?"]
                params: list[Any] = [contract_id, "epic" if epic else "card"]
                if epic:
                    updates.extend(
                        ["assignee = NULL", "workflow_template_id = NULL", "current_step_key = NULL"]
                    )
                elif str(row["status"]) not in _TERMINAL_STATUSES:
                    updates.extend(
                        ["assignee = ?", "workflow_template_id = 'product'", "current_step_key = ?"]
                    )
                    params.extend(
                        [item["proposed"]["assignee"], item["proposed"]["entry_phase"]]
                    )
                params.append(task_id)
                with kb.authorized_governance_write():
                    conn.execute(
                        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params
                    )
                kb._append_event(
                    conn,
                    task_id,
                    "qualification_migrated",
                    {
                        "work_contract_id": contract_id,
                        "contract_digest": signed["digest"],
                        "source": "legacy_migration",
                    },
                )
                changed += 1

            # Only an explicitly labelled legacy Epic can turn its old child
            # dependency edges into membership.  All other links remain edges.
            with kb.authorized_governance_write():
                for epic_id in explicit_epics:
                    children = conn.execute(
                        "SELECT child_id FROM task_links WHERE parent_id = ?",
                        (epic_id,),
                    ).fetchall()
                    for child in children:
                        child_id = str(child["child_id"])
                        if child_id not in rows or child_id in explicit_epics:
                            continue
                        conn.execute(
                            "INSERT OR IGNORE INTO epic_memberships (epic_id, task_id, created_at) "
                            "VALUES (?, ?, ?)",
                            (epic_id, child_id, int(rows[child_id]["created_at"] or time.time())),
                        )
                        conn.execute(
                            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
                            (epic_id, child_id),
                        )

            remaining = conn.execute(
                "SELECT id FROM tasks WHERE status != 'archived' AND work_contract_id IS NULL"
            ).fetchall()
            if remaining:
                raise MigrationBlocked(
                    "uncontracted non-archived work remains: "
                    + ", ".join(str(row["id"]) for row in remaining)
                )

        _write_metadata_strict(board)
        with kb.authorized_governance_write(), kb.write_txn(conn):
            conn.execute(
                "UPDATE board_governance SET qualification_required = 1 WHERE id = 1"
            )

    verification = audit_board(board)
    if not verification["qualification_required"]:
        raise MigrationBlocked("strict qualification metadata did not activate")
    receipt.update(
        {
            "status": "applied",
            "changed": changed,
            "strict_enabled": True,
            "verification": verification,
        }
    )
    receipt_path = receipt_dir / "receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    _make_read_only(receipt_dir)
    return {
        "board": board,
        "changed": changed,
        "strict_enabled": True,
        "receipt_path": str(receipt_path),
        "verification": verification,
    }


def _other_boards_have_contracts(excluded_board: str) -> bool:
    for entry in kb.list_boards(include_archived=False):
        slug = str(entry.get("slug") or "")
        if not slug or slug == excluded_board:
            continue
        path = kb.kanban_db_path(slug)
        if not path.is_file():
            continue
        try:
            with _ro_connect(path) as conn:
                if conn.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0]:
                    return True
        except sqlite3.Error:
            continue
    return False


def rollback_receipt(receipt_path: Path) -> dict[str, Any]:
    """Restore the pre-apply snapshot and append a separate rollback receipt."""

    path = Path(receipt_path).expanduser().resolve()
    receipt = json.loads(path.read_text(encoding="utf-8"))
    board = str(receipt["board"])
    with _quiescent_board(board):
        return _rollback_receipt_quiescent(path, receipt)


def _rollback_receipt_quiescent(
    path: Path, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    board = str(receipt["board"])
    live = receipt["live"]
    snapshot = receipt["snapshot"]
    current = audit_board(board)
    if current["active_running"]:
        raise MigrationBlocked("cannot rollback while board work is running")

    snapshot_uri = f"file:{Path(snapshot['db'])}?mode=ro&immutable=1"
    with contextlib.closing(sqlite3.connect(snapshot_uri, uri=True)) as source:
        with contextlib.closing(sqlite3.connect(str(live["db"]))) as target:
            source.backup(target)

    metadata_live = Path(live["metadata"])
    if snapshot.get("metadata"):
        shutil.copy2(snapshot["metadata"], metadata_live)
    else:
        metadata_live.unlink(missing_ok=True)

    attachments_live = Path(live["attachments"])
    if attachments_live.exists():
        shutil.rmtree(attachments_live)
    if snapshot.get("attachments"):
        shutil.copytree(snapshot["attachments"], attachments_live)

    key_live = Path(live["signing_key"])
    if snapshot.get("signing_key"):
        shutil.copy2(snapshot["signing_key"], key_live)
        key_live.chmod(0o600)
    elif not _other_boards_have_contracts(board):
        key_live.unlink(missing_ok=True)

    # Re-open after metadata restoration so the mirrored DB gate matches the
    # restored policy. SQLite's backup API coordinates with any open WAL
    # connections instead of replacing files underneath them.
    with kb.connect_closing(board=board) as restored:
        integrity = str(restored.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise MigrationBlocked(f"rollback integrity check failed: {integrity}")

    rollback = {
        "version": 1,
        "board": board,
        "restored": True,
        "source_receipt": str(path),
        "rolled_back_at": int(time.time()),
    }
    rollback_path = path.parent.parent / (
        path.parent.name
        + ".rollback."
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + ".json"
    )
    rollback_path.write_text(
        json.dumps(rollback, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    rollback_path.chmod(0o444)
    return {**rollback, "rollback_receipt": str(rollback_path)}
