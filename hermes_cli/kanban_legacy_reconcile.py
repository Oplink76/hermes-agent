"""Exact, read-only audit for the approved legacy board reconciliation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_qualification_migrate as migration


RECONCILIATION_SCOPE = "legacy-board-inbox-reconciliation-2026-07-20"
APPROVED_BOARDS = [
    "agentic-os-cockpit",
    "the-trading-company",
    "llm-memory-wiki-bridge",
    "handoff-lab",
    "ready-console",
    "useful-tool",
]
CARD_DISPOSITIONS = frozenset({
    "verify",
    "legacy_reconciled",
    "keep_open",
    "review",
    "archive",
})
QUALIFICATION_DISPOSITIONS = frozenset({
    "legitimate_or_none",
    "migration_artifact_not_qualification",
})
_INTERNAL_QUALIFICATION_SOURCES = frozenset({"hermes-migration", "hermes-reconcile"})
_TOP_LEVEL_FIELDS = frozenset({"version", "scope", "boards", "cards"})
_CARD_FIELDS = frozenset({
    "board",
    "task_id",
    "expected",
    "card_disposition",
    "qualification_disposition",
    "qualification_lineage",
    "evidence",
})
_EXPECTED_FIELDS = frozenset({
    "status",
    "current_step_key",
    "current_run_id",
    "running",
    "blocked",
    "work_contract_id",
})
_LINEAGE_FIELDS = frozenset({"intake_ids", "contract_ids"})
_APPROVAL_FIELDS = frozenset({
    "version",
    "authority",
    "scope",
    "approved_by",
    "approved_at",
    "manifest_sha256",
    "evidence",
})


class ReconciliationBlocked(RuntimeError):
    """The exact reconciliation manifest does not match live board state."""


def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_exact_fields(
    value: object,
    expected_fields: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReconciliationBlocked(f"{label} must be an object")
    actual_fields = frozenset(value)
    if actual_fields != expected_fields:
        missing = sorted(expected_fields - actual_fields)
        extra = sorted(actual_fields - expected_fields)
        raise ReconciliationBlocked(
            f"{label} schema mismatch: missing={missing!r} extra={extra!r}"
        )
    return value


def _require_optional_string(value: object, label: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ReconciliationBlocked(f"{label} must be a non-empty string or null")


def _validate_string_list(value: object, label: str, *, allow_empty: bool) -> None:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ReconciliationBlocked(
            f"{label} must be a list of distinct non-empty strings"
        )


def _validate_card(card_value: object, index: int) -> dict[str, Any]:
    label = f"cards[{index}]"
    card = _require_exact_fields(card_value, _CARD_FIELDS, label)
    if card["board"] not in APPROVED_BOARDS:
        raise ReconciliationBlocked(f"{label}.board is not an approved board")
    if not isinstance(card["task_id"], str) or not card["task_id"]:
        raise ReconciliationBlocked(f"{label}.task_id must be a non-empty string")

    expected = _require_exact_fields(
        card["expected"], _EXPECTED_FIELDS, f"{label}.expected"
    )
    if not isinstance(expected["status"], str) or not expected["status"]:
        raise ReconciliationBlocked(f"{label}.expected.status must be a string")
    _require_optional_string(
        expected["current_step_key"], f"{label}.expected.current_step_key"
    )
    if expected["current_run_id"] is not None and (
        type(expected["current_run_id"]) is not int or expected["current_run_id"] < 1
    ):
        raise ReconciliationBlocked(
            f"{label}.expected.current_run_id must be a positive integer or null"
        )
    for field in ("running", "blocked"):
        if type(expected[field]) is not int or expected[field] not in (0, 1):
            raise ReconciliationBlocked(f"{label}.expected.{field} must be zero or one")
    _require_optional_string(
        expected["work_contract_id"], f"{label}.expected.work_contract_id"
    )

    if card["card_disposition"] not in CARD_DISPOSITIONS:
        raise ReconciliationBlocked(
            f"unknown card disposition: {card['card_disposition']!r}"
        )
    if card["qualification_disposition"] not in QUALIFICATION_DISPOSITIONS:
        raise ReconciliationBlocked(
            f"unknown qualification disposition: {card['qualification_disposition']!r}"
        )

    lineage = _require_exact_fields(
        card["qualification_lineage"],
        _LINEAGE_FIELDS,
        f"{label}.qualification_lineage",
    )
    _validate_string_list(
        lineage["intake_ids"],
        f"{label}.qualification_lineage.intake_ids",
        allow_empty=True,
    )
    _validate_string_list(
        lineage["contract_ids"],
        f"{label}.qualification_lineage.contract_ids",
        allow_empty=True,
    )
    _validate_string_list(card["evidence"], f"{label}.evidence", allow_empty=False)
    return card


def _load_and_validate_manifest(
    manifest_path: Path,
) -> tuple[dict[str, Any], str]:
    try:
        raw = manifest_path.read_bytes()
        manifest_value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationBlocked(f"invalid reconciliation manifest: {exc}") from exc
    manifest = _require_exact_fields(manifest_value, _TOP_LEVEL_FIELDS, "manifest")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ReconciliationBlocked("manifest version must be 1")
    if manifest["scope"] != RECONCILIATION_SCOPE:
        raise ReconciliationBlocked("manifest scope is not approved")
    if manifest["boards"] != APPROVED_BOARDS:
        raise ReconciliationBlocked("manifest must list the exact approved boards")
    if not isinstance(manifest["cards"], list):
        raise ReconciliationBlocked("manifest cards must be a list")

    seen_task_ids: dict[str, str] = {}
    for index, card_value in enumerate(manifest["cards"]):
        card = _validate_card(card_value, index)
        task_id = card["task_id"]
        if task_id in seen_task_ids:
            raise ReconciliationBlocked(
                f"duplicate card {task_id!r} on boards "
                f"{seen_task_ids[task_id]!r} and {card['board']!r}"
            )
        seen_task_ids[task_id] = card["board"]

    digest = hashlib.sha256(raw).hexdigest()
    return manifest, digest


def _load_and_validate_approval(
    approval_path: Path,
    *,
    manifest_digest: str,
) -> dict[str, Any]:
    try:
        approval_value = json.loads(approval_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationBlocked(f"invalid reconciliation approval: {exc}") from exc
    approval = _require_exact_fields(
        approval_value, _APPROVAL_FIELDS, "approval"
    )
    if approval["version"] != 1:
        raise ReconciliationBlocked("approval version must be 1")
    if approval["authority"] != "break-glass":
        raise ReconciliationBlocked("approval authority must be break-glass")
    if approval["scope"] != RECONCILIATION_SCOPE:
        raise ReconciliationBlocked("approval scope is not approved")
    if approval["approved_by"] != "Ole Ørum-Petersen":
        raise ReconciliationBlocked("approval must be from Ole Ørum-Petersen")
    for field in ("approved_at", "evidence"):
        if not isinstance(approval[field], str) or not approval[field]:
            raise ReconciliationBlocked(f"approval {field} must be a non-empty string")
    if approval["manifest_sha256"] != manifest_digest:
        raise ReconciliationBlocked("approval hash does not match manifest")
    return approval


def _internal_task_id(source: str, raw_request: str, intake_id: str) -> str:
    try:
        payload = json.loads(raw_request)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationBlocked(
            f"internal qualification intake {intake_id!r} has invalid JSON"
        ) from exc
    task_field = "legacy_task_id" if source == "hermes-migration" else "target_task_id"
    task_id = payload.get(task_field) if isinstance(payload, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise ReconciliationBlocked(
            f"internal qualification intake {intake_id!r} has no {task_field}"
        )
    return task_id


def _read_board_query_only(board: str) -> dict[str, Any]:
    path = kb.kanban_db_path(board)
    if not path.is_file():
        raise ReconciliationBlocked(f"board database is missing: {board}")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise ReconciliationBlocked(f"board {board!r} is not query-only")
        tasks = {
            str(row["id"]): {
                "status": row["status"],
                "current_step_key": row["current_step_key"],
                "current_run_id": row["current_run_id"],
                "running": row["running"],
                "blocked": row["blocked"],
                "work_contract_id": row["work_contract_id"],
            }
            for row in conn.execute(
                """
                SELECT id, status, current_step_key, current_run_id,
                       running, blocked, work_contract_id
                FROM tasks
                """
            )
        }
        active_run_ids = {
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM task_runs WHERE ended_at IS NULL"
            )
        }
        all_intake_ids = {
            str(row["id"])
            for row in conn.execute("SELECT id FROM qualification_intake")
        }
        all_contract_ids = {
            str(row["id"]) for row in conn.execute("SELECT id FROM work_contracts")
        }
        lineage: dict[str, dict[str, set[str]]] = {}
        for row in conn.execute(
            """
            SELECT qi.id AS intake_id, qi.raw_request, qi.source,
                   wc.id AS contract_id
            FROM qualification_intake AS qi
            LEFT JOIN work_contracts AS wc ON wc.request_id = qi.id
            WHERE qi.source IN ('hermes-migration', 'hermes-reconcile')
            ORDER BY qi.id, wc.id
            """
        ):
            source = str(row["source"])
            if source not in _INTERNAL_QUALIFICATION_SOURCES:
                raise ReconciliationBlocked(
                    f"unexpected internal qualification source: {source!r}"
                )
            task_id = _internal_task_id(
                source, str(row["raw_request"]), str(row["intake_id"])
            )
            if task_id not in tasks:
                raise ReconciliationBlocked(
                    f"internal lineage references unknown card {task_id!r}"
                )
            task_lineage = lineage.setdefault(
                task_id, {"intake_ids": set(), "contract_ids": set()}
            )
            task_lineage["intake_ids"].add(str(row["intake_id"]))
            if row["contract_id"] is not None:
                task_lineage["contract_ids"].add(str(row["contract_id"]))
    except sqlite3.Error as exc:
        raise ReconciliationBlocked(f"could not audit board {board!r}: {exc}") from exc
    finally:
        if "conn" in locals():
            conn.close()
    return {
        "tasks": tasks,
        "intake_ids": all_intake_ids,
        "contract_ids": all_contract_ids,
        "lineage": lineage,
        "active_run_ids": active_run_ids,
    }


def _read_boards_query_only(boards: list[str]) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    task_boards: dict[str, str] = {}
    for board in boards:
        board_state = _read_board_query_only(board)
        observed[board] = board_state
        for task_id in board_state["tasks"]:
            if task_id in task_boards:
                raise ReconciliationBlocked(
                    f"cross-board duplicate card {task_id!r} on boards "
                    f"{task_boards[task_id]!r} and {board!r}"
                )
            task_boards[task_id] = board
    return observed


def _validate_exact_inventory(
    cards: list[dict[str, Any]],
    observed: dict[str, dict[str, Any]],
) -> None:
    expected_inventory = {(card["board"], card["task_id"]) for card in cards}
    observed_inventory = {
        (board, task_id)
        for board, board_state in observed.items()
        for task_id in board_state["tasks"]
    }
    if expected_inventory != observed_inventory:
        missing = sorted(expected_inventory - observed_inventory)
        extra = sorted(observed_inventory - expected_inventory)
        raise ReconciliationBlocked(
            f"manifest inventory mismatch: missing={missing!r} extra={extra!r}"
        )


def _validate_guards_and_lineage(
    cards: list[dict[str, Any]],
    observed: dict[str, dict[str, Any]],
) -> None:
    for card in cards:
        board = card["board"]
        task_id = card["task_id"]
        board_state = observed[board]
        actual = board_state["tasks"][task_id]
        if actual != card["expected"]:
            differences = sorted(
                field
                for field in _EXPECTED_FIELDS
                if actual[field] != card["expected"][field]
            )
            raise ReconciliationBlocked(
                f"guard mismatch for {board}/{task_id}: {differences!r}"
            )

        lineage = card["qualification_lineage"]
        intake_ids = set(lineage["intake_ids"])
        contract_ids = set(lineage["contract_ids"])
        missing_intakes = intake_ids - board_state["intake_ids"]
        missing_contracts = contract_ids - board_state["contract_ids"]
        if missing_intakes or missing_contracts:
            raise ReconciliationBlocked(
                f"lineage mismatch for {board}/{task_id}: "
                f"missing_intakes={sorted(missing_intakes)!r} "
                f"missing_contracts={sorted(missing_contracts)!r}"
            )
        expected_lineage = board_state["lineage"].get(
            task_id, {"intake_ids": set(), "contract_ids": set()}
        )
        if (
            intake_ids != expected_lineage["intake_ids"]
            or contract_ids != expected_lineage["contract_ids"]
        ):
            raise ReconciliationBlocked(f"lineage mismatch for {board}/{task_id}")
        correction = (
            card["qualification_disposition"] == "migration_artifact_not_qualification"
        )
        if correction != bool(intake_ids or contract_ids):
            raise ReconciliationBlocked(
                f"qualification disposition mismatch for {board}/{task_id}"
            )
        work_contract_id = card["expected"]["work_contract_id"]
        if (
            work_contract_id is not None
            and work_contract_id not in board_state["contract_ids"]
        ):
            raise ReconciliationBlocked(
                f"guarded Work Contract does not exist: {work_contract_id!r}"
            )


def _counts(cards: list[dict[str, Any]]) -> dict[str, int]:
    dispositions = Counter(card["card_disposition"] for card in cards)
    return {
        "cards": len(cards),
        "verify": dispositions["verify"],
        "legacy_reconciled": dispositions["legacy_reconciled"],
        "keep_open": dispositions["keep_open"],
        "review": dispositions["review"],
        "archive": dispositions["archive"],
        "qualification_corrections": sum(
            card["qualification_disposition"] == "migration_artifact_not_qualification"
            for card in cards
        ),
    }


def audit_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest, digest = _load_and_validate_manifest(manifest_path)
    observed = _read_boards_query_only(manifest["boards"])
    _validate_exact_inventory(manifest["cards"], observed)
    _validate_guards_and_lineage(manifest["cards"], observed)
    return {
        "version": 1,
        "mode": "dry-run",
        "scope": manifest["scope"],
        "manifest_sha256": digest,
        "boards": manifest["boards"],
        "counts": _counts(manifest["cards"]),
        "ready_to_apply": True,
    }


def _expected_event_payload(
    entry: dict[str, Any], kind: str, digest: str
) -> dict[str, Any]:
    if kind == "qualification_history_corrected":
        return {
            "manifest_sha256": digest,
            "actor": "hermes/default",
            "reason": (
                "Legacy migration/reconciliation was not "
                "Qualification or Requalification"
            ),
            **entry["qualification_lineage"],
        }
    return {
        "manifest_sha256": digest,
        "actor": "hermes/default",
        "reason": (
            "Approved evidence proves this externally orchestrated "
            "legacy work is complete"
        ),
        "evidence": entry["evidence"],
    }


def _append_once(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    digest: str,
) -> bool:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchall()
    for row in rows:
        try:
            existing = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if existing.get("manifest_sha256") == digest:
            return False
    kb._append_event(conn, task_id, kind, payload)
    return True


def _apply_board(
    board: str,
    entries: list[dict[str, Any]],
    digest: str,
    now: int,
) -> Counter[str]:
    changed: Counter[str] = Counter()
    with kb.connect_closing(board=board) as conn:
        with kb.authorized_governance_write(), kb.write_txn(conn):
            for entry in entries:
                task_id = entry["task_id"]
                if (
                    entry["qualification_disposition"]
                    == "migration_artifact_not_qualification"
                ):
                    payload = _expected_event_payload(
                        entry, "qualification_history_corrected", digest
                    )
                    if _append_once(
                        conn,
                        task_id,
                        "qualification_history_corrected",
                        payload,
                        digest=digest,
                    ):
                        changed["qualification_history_corrected"] += 1

                disposition = entry["card_disposition"]
                if disposition == "legacy_reconciled":
                    payload = _expected_event_payload(
                        entry, "legacy_reconciled", digest
                    )
                    if _append_once(
                        conn,
                        task_id,
                        "legacy_reconciled",
                        payload,
                        digest=digest,
                    ):
                        changed["legacy_reconciled"] += 1
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'done', current_step_key = 'done',
                            completed_at = COALESCE(completed_at, ?),
                            running = 0, blocked = 0,
                            claim_lock = NULL, claim_expires = NULL,
                            worker_pid = NULL, block_kind = NULL,
                            block_recurrences = 0
                        WHERE id = ?
                        """,
                        (now, task_id),
                    )
                elif disposition == "archive":
                    if kb.archive_task(conn, task_id):
                        changed["archived"] += 1
    return changed


def _integrity(board: str) -> str:
    path = kb.kanban_db_path(board)
    with contextlib.closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def _matching_event_count(
    manifest: dict[str, Any], digest: str, *, exact_payload: bool
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for board in manifest["boards"]:
        entries = [card for card in manifest["cards"] if card["board"] == board]
        path = kb.kanban_db_path(board)
        with contextlib.closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            for entry in entries:
                kinds: list[str] = []
                if (
                    entry["qualification_disposition"]
                    == "migration_artifact_not_qualification"
                ):
                    kinds.append("qualification_history_corrected")
                if entry["card_disposition"] == "legacy_reconciled":
                    kinds.append("legacy_reconciled")
                for kind in kinds:
                    expected = _expected_event_payload(entry, kind, digest)
                    for row in conn.execute(
                        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                        (entry["task_id"], kind),
                    ):
                        try:
                            payload = json.loads(row["payload"] or "{}")
                        except json.JSONDecodeError:
                            continue
                        if payload.get("manifest_sha256") == digest and (
                            not exact_payload or payload == expected
                        ):
                            counts[kind] += 1
    return counts


def _find_successful_receipt(
    recovery_base: Path,
    digest: str,
) -> tuple[Path, dict[str, Any]] | None:
    if not recovery_base.is_dir():
        return None
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for receipt_path in recovery_base.glob("*/receipt.json"):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            receipt.get("status") == "applied"
            and receipt.get("manifest_sha256") == digest
        ):
            candidates.append((int(receipt.get("created_at") or 0), receipt_path, receipt))
    if not candidates:
        return None
    _created_at, receipt_path, receipt = max(candidates, key=lambda item: item[0])
    return receipt_path, receipt


def _verify_post_state(
    manifest: dict[str, Any],
    digest: str,
    *,
    boards: list[str] | None = None,
) -> tuple[dict[str, str], Counter[str]]:
    integrity: dict[str, str] = {}
    event_counts: Counter[str] = Counter()
    for board in boards or manifest["boards"]:
        entries = [card for card in manifest["cards"] if card["board"] == board]
        path = kb.kanban_db_path(board)
        with contextlib.closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            for entry in entries:
                row = conn.execute(
                    """
                    SELECT status, current_step_key, current_run_id,
                           running, blocked, work_contract_id
                    FROM tasks WHERE id = ?
                    """,
                    (entry["task_id"],),
                ).fetchone()
                if row is None:
                    raise ReconciliationBlocked(
                        f"post-apply card missing: {board}/{entry['task_id']}"
                    )
                disposition = entry["card_disposition"]
                actual = dict(row)
                expected = entry["expected"]
                if disposition == "legacy_reconciled":
                    canonical = {
                        **expected,
                        "status": "done",
                        "current_step_key": "done",
                        "current_run_id": None,
                        "running": 0,
                        "blocked": 0,
                    }
                elif disposition == "archive":
                    canonical = {
                        **expected,
                        "status": "archived",
                        "current_run_id": None,
                        "running": 0,
                        "blocked": 0,
                    }
                else:
                    canonical = expected
                if actual != canonical:
                    raise ReconciliationBlocked(
                        f"post-apply state mismatch: {board}/{entry['task_id']}"
                    )
                expected_events = []
                if entry["qualification_disposition"] == "migration_artifact_not_qualification":
                    expected_events.append("qualification_history_corrected")
                if disposition == "legacy_reconciled":
                    expected_events.append("legacy_reconciled")
                for kind in expected_events:
                    matches = 0
                    for event in conn.execute(
                        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                        (entry["task_id"], kind),
                    ):
                        try:
                            payload = json.loads(event["payload"] or "{}")
                        except json.JSONDecodeError:
                            continue
                        expected_payload = _expected_event_payload(
                            entry, kind, digest
                        )
                        matches += payload == expected_payload
                    if matches != 1:
                        raise ReconciliationBlocked(
                            f"post-apply {kind} event mismatch: {board}/{entry['task_id']}"
                        )
                    event_counts[kind] += matches
        integrity[board] = _integrity(board)
        if integrity[board] != "ok":
            raise ReconciliationBlocked(
                f"post-apply integrity check failed for {board}: {integrity[board]}"
            )
    return integrity, event_counts


def _restore_snapshots(
    snapshots: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    integrity: dict[str, str] = {}
    errors: dict[str, str] = {}
    for board, record in snapshots.items():
        try:
            snapshot_path = Path(record["snapshot"]["snapshot"]["db"])
            live_path = kb.kanban_db_path(board)
            snapshot_uri = f"file:{snapshot_path}?mode=ro&immutable=1"
            with contextlib.closing(
                sqlite3.connect(snapshot_uri, uri=True)
            ) as source, contextlib.closing(sqlite3.connect(str(live_path))) as target:
                source.backup(target)
            integrity[board] = _integrity(board)
            if integrity[board] != "ok":
                errors[board] = f"integrity={integrity[board]}"
        except Exception as exc:
            errors[board] = str(exc)
    return integrity, errors


def _status_counts(observed: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts = Counter(
        task["status"]
        for board_state in observed.values()
        for task in board_state["tasks"].values()
    )
    return dict(sorted(counts.items()))


def apply_manifest(
    manifest_path: Path,
    approval_path: Path,
    *,
    recovery_root: Path | None = None,
) -> dict[str, Any]:
    """Apply the exact manifest after validating break-glass authority."""

    manifest, digest = _load_and_validate_manifest(manifest_path)
    approval = _load_and_validate_approval(
        approval_path, manifest_digest=digest
    )
    recovery_base = Path(recovery_root) if recovery_root is not None else (
        get_default_hermes_root() / "recovery" / "legacy-reconciliation"
    )
    prior = _find_successful_receipt(recovery_base, digest)
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + f"-{digest[:12]}-{time.time_ns()}"
    )
    run_dir = recovery_base / run_id

    with contextlib.ExitStack() as stack:
        for board in sorted(manifest["boards"]):
            held = stack.enter_context(
                kb._dispatch_tick_lock(kb.kanban_db_path(board))
            )
            if not held:
                raise ReconciliationBlocked(
                    f"board {board!r} has an active dispatch tick"
                )

        observed = _read_boards_query_only(manifest["boards"])
        _validate_exact_inventory(manifest["cards"], observed)
        if prior is not None:
            prior_path, prior_receipt = prior
            integrity, event_counts = _verify_post_state(manifest, digest)
            return {
                "status": "applied",
                "manifest_sha256": digest,
                "counts": prior_receipt["counts"],
                "receipt_path": str(prior_path),
                "integrity": integrity,
                "event_counts": dict(event_counts),
                "already_applied": True,
            }
        _validate_guards_and_lineage(manifest["cards"], observed)
        if _matching_event_count(manifest, digest, exact_payload=False):
            raise ReconciliationBlocked(
                "partial reconciliation state exists without a successful receipt"
            )
        before_counts = _status_counts(observed)
        mutation_entries = [
            card
            for card in manifest["cards"]
            if card["card_disposition"] in {"legacy_reconciled", "archive"}
            or card["qualification_disposition"]
            == "migration_artifact_not_qualification"
        ]
        for entry in mutation_entries:
            task = observed[entry["board"]]["tasks"][entry["task_id"]]
            current_run_id = task["current_run_id"]
            if task["running"] or (
                current_run_id is not None
                and current_run_id in observed[entry["board"]]["active_run_ids"]
            ):
                raise ReconciliationBlocked(
                    f"active run blocks {entry['board']}/{entry['task_id']}"
                )

        snapshots: dict[str, Any] = {}
        changed: Counter[str] = Counter()
        now = int(time.time())
        try:
            for board in manifest["boards"]:
                receipt_dir, snapshot = migration._snapshot_board(
                    board,
                    recovery_root=run_dir,
                    audit={
                        "scope": manifest["scope"],
                        "manifest_sha256": digest,
                        "cards": sum(
                            card["board"] == board for card in manifest["cards"]
                        ),
                    },
                )
                snapshots[board] = {
                    "receipt_dir": str(receipt_dir),
                    "snapshot": snapshot,
                }

            integrity: dict[str, str] = {}
            event_counts: Counter[str] = Counter()
            for board in manifest["boards"]:
                entries = [
                    card for card in manifest["cards"] if card["board"] == board
                ]
                changed.update(_apply_board(board, entries, digest, now))
                board_integrity, board_events = _verify_post_state(
                    manifest, digest, boards=[board]
                )
                integrity.update(board_integrity)
                event_counts.update(board_events)

            after_observed = _read_boards_query_only(manifest["boards"])
            counts = {
                "legacy_reconciled": changed["legacy_reconciled"],
                "archived": changed["archived"],
                "qualification_history_corrected": changed[
                    "qualification_history_corrected"
                ],
            }
            receipt = {
                "version": 1,
                "status": "applied",
                "scope": manifest["scope"],
                "manifest_sha256": digest,
                "approval": approval,
                "counts": counts,
                "before_status_counts": before_counts,
                "after_status_counts": _status_counts(after_observed),
                "event_counts": dict(event_counts),
                "snapshots": snapshots,
                "integrity": integrity,
                "created_at": now,
            }
            receipt_path = run_dir / "receipt.json"
            receipt["receipt_path"] = str(receipt_path)
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            migration._make_read_only(run_dir)
            run_dir.chmod(0o500)
        except Exception as exc:
            restored_integrity, restore_errors = _restore_snapshots(snapshots)
            status = "restore_failed" if restore_errors else "restored"
            failure_receipt = {
                "version": 1,
                "status": status,
                "scope": manifest["scope"],
                "manifest_sha256": digest,
                "error": str(exc),
                "restore_errors": restore_errors,
                "snapshots": snapshots,
                "integrity": restored_integrity,
                "created_at": int(time.time()),
            }
            failure_path = run_dir / "failure-receipt.json"
            run_dir.mkdir(parents=True, exist_ok=True)
            failure_path.write_text(
                json.dumps(
                    failure_receipt,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            migration._make_read_only(run_dir)
            run_dir.chmod(0o500)
            if status == "restored":
                raise ReconciliationBlocked(
                    f"reconciliation failed and all boards were restored: {exc}"
                ) from exc
            raise ReconciliationBlocked(
                f"reconciliation failed and snapshot restore failed: {restore_errors}"
            ) from exc

        return {
            "status": "applied",
            "manifest_sha256": digest,
            "counts": counts,
            "receipt_path": str(receipt_path),
            "integrity": integrity,
            "event_counts": dict(event_counts),
            "already_applied": False,
        }
