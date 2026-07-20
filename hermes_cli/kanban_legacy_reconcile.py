"""Exact, read-only audit for the approved legacy board reconciliation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as kb


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
