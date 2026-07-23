"""Qualification intake and signed Work Contract domain boundary.

This module contains policy and cryptographic behavior only. Durable storage
stays in :mod:`hermes_cli.kanban_db`, and clients do not materialize cards
through this module until the strict write boundary is enabled separately.
"""

from __future__ import annotations

import copy
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hermes_constants import get_default_hermes_root


logger = logging.getLogger(__name__)


CONTRACT_VERSION = 1
DEFAULT_POLICY_VERSION = "product-handoff-v2+qualification-v1"
REQUALIFICATION_QUALIFIER_REVISION = 3
SIGNING_KEY_RELATIVE_PATH = "kanban/work_contract_signing.key"

_SIGNING_METADATA_FIELDS = {"canonical_json", "digest", "signature", "contract"}
_REQUIRED_TOP_LEVEL_FIELDS = {
    "version",
    "policy_version",
    "qualification_path",
    "request_id",
    "work",
    "routing",
    "handover",
    "rules",
    "classification",
    "issuer",
}
_REQUIRED_NESTED_FIELDS = {
    "work": {"item_kind", "work_type", "title", "outcome", "scope", "out_of_scope"},
    "routing": {"entry_phase", "assignee", "epic_id", "dependencies"},
    "handover": {
        "deliverables",
        "required_evidence",
        "done_when",
        "next_phase",
        "next_role",
    },
    "rules": {"allowed", "forbidden"},
    "issuer": {"profile", "run_id", "issued_at"},
}
_QUALIFICATION_PATHS = {"po", "hermes", "override"}
_WORK_ITEM_KINDS = {"card", "epic"}
_INTAKE_WAKERS: set[Callable[[], None]] = set()
_INTAKE_WAKERS_LOCK = threading.Lock()


class WorkContractError(ValueError):
    """The Work Contract is missing, invalid, or violates board policy."""


def _register_intake_waker(callback: Callable[[], None]) -> None:
    with _INTAKE_WAKERS_LOCK:
        _INTAKE_WAKERS.add(callback)


def _unregister_intake_waker(callback: Callable[[], None]) -> None:
    with _INTAKE_WAKERS_LOCK:
        _INTAKE_WAKERS.discard(callback)


def _wake_intake_qualifier() -> None:
    with _INTAKE_WAKERS_LOCK:
        callbacks = tuple(_INTAKE_WAKERS)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            logger.warning(
                "intake qualifier wake callback failed",
                exc_info=True,
            )
            continue


def _unsigned_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise WorkContractError("contract must be an object")
    if isinstance(payload.get("contract"), Mapping):
        payload = payload["contract"]
    return {
        str(key): copy.deepcopy(value)
        for key, value in payload.items()
        if str(key) not in _SIGNING_METADATA_FIELDS
    }


def _validate_contract_shape(contract: Mapping[str, Any]) -> None:
    version = contract.get("version")
    if type(version) is not int or version != CONTRACT_VERSION:
        raise WorkContractError(
            f"unsupported Work Contract version: {version!r}; expected {CONTRACT_VERSION}"
        )

    missing = sorted(_REQUIRED_TOP_LEVEL_FIELDS - set(contract))
    if missing:
        raise WorkContractError(f"contract is missing required fields: {', '.join(missing)}")

    if not isinstance(contract.get("policy_version"), str) or not contract["policy_version"].strip():
        raise WorkContractError("policy_version is required")
    if contract.get("qualification_path") not in _QUALIFICATION_PATHS:
        raise WorkContractError("qualification_path must be po, hermes, or override")
    override_authority = contract.get("override_authority")
    if contract.get("qualification_path") == "override":
        if not isinstance(override_authority, Mapping) or not all(
            isinstance(override_authority.get(field), str)
            and override_authority.get(field).strip()
            for field in ("reason", "source_session", "instruction_ref")
        ):
            raise WorkContractError(
                "override contract requires reason, source session, and instruction reference"
            )
    elif override_authority is not None:
        raise WorkContractError("override authority is only valid on override contracts")
    if not isinstance(contract.get("request_id"), str) or not contract["request_id"].strip():
        raise WorkContractError("request_id is required")

    for section, required in _REQUIRED_NESTED_FIELDS.items():
        value = contract.get(section)
        if not isinstance(value, Mapping):
            raise WorkContractError(f"{section} must be an object")
        section_missing = sorted(required - set(value))
        if section_missing:
            raise WorkContractError(
                f"{section} is missing required fields: {', '.join(section_missing)}"
            )

    if contract["work"].get("item_kind") not in _WORK_ITEM_KINDS:
        raise WorkContractError("work.item_kind must be card or epic")
    if not isinstance(contract.get("classification"), list):
        raise WorkContractError("classification must be a list")


def canonical_contract_json(contract: Mapping[str, Any]) -> str:
    """Return the stable canonical JSON representation of an unsigned contract."""

    unsigned = _unsigned_contract(contract)
    _validate_contract_shape(unsigned)
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def contract_digest(contract: Mapping[str, Any]) -> str:
    """Return the SHA-256 digest of the canonical unsigned contract."""

    canonical = canonical_contract_json(contract)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _signing_key_path(hermes_home: Optional[Path] = None) -> Path:
    root = Path(hermes_home) if hermes_home is not None else get_default_hermes_root()
    return root / SIGNING_KEY_RELATIVE_PATH


def _restrict_signing_key_permissions(path: Path) -> None:
    """Restrict the service key to the current account on every platform."""

    if sys.platform == "win32":
        icacls = shutil.which("icacls")
        if not icacls:
            raise WorkContractError("cannot secure Work Contract signing key: icacls not found")
        username = getpass.getuser().strip()
        if not username:
            raise WorkContractError("cannot secure Work Contract signing key: user is unknown")
        commands = (
            [icacls, str(path), "/reset"],
            [
                icacls,
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{username}:F",
            ],
        )
        for command in commands:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise WorkContractError(
                    "cannot secure Work Contract signing key with Windows ACLs"
                )
        return
    os.chmod(path, 0o600)


def _load_signing_secret(
    hermes_home: Optional[Path] = None, *, create: bool
) -> bytes:
    """Load the service key, atomically creating it with mode 0600 once."""

    path = _signing_key_path(hermes_home)
    if path.is_symlink():
        raise WorkContractError("Work Contract signing key cannot be a symlink")
    if not create and not path.is_file():
        raise WorkContractError("Work Contract signing key is missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
    if path.is_symlink() or not path.is_file():
        raise WorkContractError("Work Contract signing key is not a regular file")
    _restrict_signing_key_permissions(path)
    value = path.read_bytes()
    if len(value) < 32:
        raise WorkContractError("Work Contract signing key is invalid")
    return value


def _service_secret(
    *, secret: Optional[bytes], hermes_home: Optional[Path], create: bool
) -> bytes:
    if secret is not None:
        if not isinstance(secret, bytes) or not secret:
            raise WorkContractError("signing secret must be non-empty bytes")
        return secret
    return _load_signing_secret(hermes_home, create=create)


def sign_work_contract(
    contract: Mapping[str, Any],
    *,
    secret: Optional[bytes] = None,
    hermes_home: Optional[Path] = None,
) -> dict[str, Any]:
    """Canonicalize and service-sign a contract, discarding caller metadata."""

    unsigned = _unsigned_contract(contract)
    canonical = canonical_contract_json(unsigned)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    key = _service_secret(secret=secret, hermes_home=hermes_home, create=True)
    signature = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "contract": unsigned,
        "canonical_json": canonical,
        "digest": digest,
        "signature": signature,
    }


def verify_work_contract(
    signed_contract: Mapping[str, Any],
    *,
    secret: Optional[bytes] = None,
    hermes_home: Optional[Path] = None,
) -> bool:
    """Fail closed unless canonical JSON, digest, signature, and version agree."""

    try:
        if not isinstance(signed_contract, Mapping):
            return False
        contract = signed_contract.get("contract")
        if not isinstance(contract, Mapping):
            return False
        canonical = canonical_contract_json(contract)
        supplied_canonical = signed_contract.get("canonical_json")
        supplied_digest = signed_contract.get("digest")
        supplied_signature = signed_contract.get("signature")
        if not all(isinstance(value, str) for value in (
            supplied_canonical,
            supplied_digest,
            supplied_signature,
        )):
            return False
        if not hmac.compare_digest(canonical, supplied_canonical):
            return False
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, supplied_digest):
            return False
        key = _service_secret(secret=secret, hermes_home=hermes_home, create=False)
        expected = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, supplied_signature)
    except (OSError, TypeError, ValueError, WorkContractError):
        return False


def materialization_fields(
    board_metadata: Mapping[str, Any],
    *,
    signed_contract: Optional[Mapping[str, Any]],
    caller_fields: Optional[Mapping[str, Any]] = None,
    secret: Optional[bytes] = None,
    hermes_home: Optional[Path] = None,
) -> dict[str, Any]:
    """Return governed task fields, or pass generic-board fields through.

    This is a policy primitive only. Task 2 wires it into every create surface.
    """

    fields = copy.deepcopy(dict(caller_fields or {}))
    qualification = board_metadata.get("qualification")
    policy = qualification if isinstance(qualification, Mapping) else {}
    if policy.get("required") is not True:
        return fields
    if signed_contract is None:
        raise WorkContractError("a valid Work Contract is required on this board")
    if not verify_work_contract(
        signed_contract, secret=secret, hermes_home=hermes_home
    ):
        raise WorkContractError("Work Contract signature is invalid")

    contract = signed_contract["contract"]
    expected_version = policy.get("contract_version", CONTRACT_VERSION)
    if contract.get("version") != expected_version:
        raise WorkContractError("Work Contract version does not match board policy")
    expected_policy = policy.get("policy_version")
    if expected_policy and contract.get("policy_version") != expected_policy:
        raise WorkContractError("Work Contract policy version does not match board policy")
    allowed_paths = policy.get("paths")
    if not isinstance(allowed_paths, (list, tuple)) or not allowed_paths:
        raise WorkContractError("strict board policy requires allowed qualification paths")
    path = contract.get("qualification_path")
    if path != "override" and path not in allowed_paths:
        raise WorkContractError(
            f"qualification_path {path!r} is not allowed by board policy"
        )

    routing = contract["routing"]
    work = contract["work"]
    if work["item_kind"] == "epic":
        if routing["entry_phase"] is not None or routing["assignee"] is not None:
            raise WorkContractError("Epic contracts cannot declare a phase or assignee")
        if routing["epic_id"] is not None or routing["dependencies"]:
            raise WorkContractError(
                "Epic contracts cannot declare membership or dependencies"
            )
        fields.update(
            {
                "title": work["title"],
                "body": work["outcome"],
                "assignee": None,
                "workflow_template_id": None,
                "current_step_key": None,
                "work_item_kind": "epic",
                "epic_id": None,
                "parents": [],
                "classification": copy.deepcopy(contract["classification"]),
                "contract_digest": signed_contract["digest"],
            }
        )
        return fields

    phase = routing["entry_phase"]
    assignee = routing["assignee"]
    phase_assignees = policy.get("phase_assignees")
    if not isinstance(phase_assignees, Mapping):
        raise WorkContractError("strict board policy requires a phase_assignees mapping")
    if phase not in phase_assignees or phase_assignees.get(phase) != assignee:
        raise WorkContractError(
            f"phase {phase!r} and assignee {assignee!r} are not an allowed phase/assignee pair"
        )

    fields.update(
        {
            "title": work["title"],
            "body": work["outcome"],
            "assignee": assignee,
            "workflow_template_id": "product",
            "current_step_key": phase,
            "work_item_kind": work["item_kind"],
            "epic_id": routing["epic_id"],
            "parents": copy.deepcopy(routing["dependencies"]),
            "classification": copy.deepcopy(contract["classification"]),
            "contract_digest": signed_contract["digest"],
        }
    )
    return fields


def qualification_required(board_metadata: Mapping[str, Any]) -> bool:
    """Return whether executable work on this board requires qualification."""

    policy = board_metadata.get("qualification")
    return isinstance(policy, Mapping) and policy.get("required") is True


def intake_payload(intake: Mapping[str, Any]) -> dict[str, Any]:
    """Return the structured request stored in an intake, or an empty mapping."""

    try:
        value = json.loads(str(intake.get("raw_request") or ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def requalification_target_id(intake: Mapping[str, Any]) -> Optional[str]:
    """Return the existing task targeted by a requalification intake."""

    payload = intake_payload(intake)
    target_task_id = payload.get("target_task_id")
    if payload.get("kind") != "task_requalification":
        return None
    return target_task_id if isinstance(target_task_id, str) else None


def existing_requalification_intake(
    conn: Any,
    task_id: str,
    *,
    evidence_digest: str,
    qualifier_revision: int,
) -> Optional[dict[str, Any]]:
    """Return an active intake or a rejection of the same evidence."""

    rows = conn.execute(
        "SELECT * FROM qualification_intake "
        "WHERE status IN ('pending', 'rejected') ORDER BY created_at, id"
    ).fetchall()
    for row in rows:
        record = dict(row)
        if requalification_target_id(record) != task_id:
            continue
        if record["status"] == "pending":
            return record
        payload = intake_payload(record)
        if (
            payload.get("evidence_digest") == evidence_digest
            and payload.get("qualifier_revision", 1) == qualifier_revision
        ):
            return record
    return None


def _requalification_evidence_digest(evidence: Mapping[str, Any]) -> str:
    """Hash evidence while excluding the intake's own bookkeeping event."""

    stable = copy.deepcopy(dict(evidence))
    events = stable.get("events")
    if isinstance(events, list):
        stable["events"] = [
            event
            for event in events
            if not isinstance(event, Mapping)
            or event.get("kind") != "requalification_requested"
        ]
    canonical = json.dumps(
        stable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def submit_requalification(
    conn: Any,
    *,
    task_id: str,
    reason: str,
) -> dict[str, Any]:
    """Submit one Hermes-owned, inert requalification request for a parked card."""

    from hermes_cli import kanban_db

    reason = str(reason or "").strip()
    if not reason:
        raise WorkContractError("requalification reason is required")

    with kanban_db.authorized_governance_write():
        with kanban_db.write_txn(conn):
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise WorkContractError(f"unknown requalification task: {task_id}")
            if row["work_contract_id"] is None:
                raise WorkContractError("requalification requires a qualified task")
            if row["status"] != "scheduled":
                raise WorkContractError(
                    "automatic requalification requires a scheduled task"
                )
            if row["current_run_id"] is not None or row["claim_lock"] is not None:
                raise WorkContractError("cannot requalify a task with an active worker")
            if row["current_step_key"] == "release_measure":
                raise WorkContractError("release_measure follows release evidence policy")
            if kanban_db._has_sticky_block(conn, task_id):
                raise WorkContractError(
                    "cannot requalify a task with an unresolved blocker"
                )

            stored_contract = kanban_db.get_work_contract(
                conn, str(row["work_contract_id"])
            )
            evidence = {
                "task": dict(row),
                "contract": (
                    stored_contract.get("contract") if stored_contract else None
                ),
                "dependencies": kanban_db.parent_ids(conn, task_id),
                "epic_id": kanban_db.epic_id_for_task(conn, task_id),
                "runs": [
                    asdict(run) for run in kanban_db.list_runs(conn, task_id)
                ],
                "events": [
                    asdict(event) for event in kanban_db.list_events(conn, task_id)
                ],
                "comments": [
                    asdict(comment)
                    for comment in kanban_db.list_comments(conn, task_id)
                ],
                "repository": kanban_db.task_repository_evidence(row),
            }
            evidence_digest = _requalification_evidence_digest(evidence)
            existing = existing_requalification_intake(
                conn,
                task_id,
                evidence_digest=evidence_digest,
                qualifier_revision=REQUALIFICATION_QUALIFIER_REVISION,
            )
            if existing is not None:
                intake_id = str(existing["id"])
                return {
                    "status": (
                        "requalification_required"
                        if existing["status"] == "pending"
                        else "requalification_rejected"
                    ),
                    "created": False,
                    "intake_id": intake_id,
                    "intake_status": str(existing["status"]),
                    "task_id": task_id,
                }
            raw_request = json.dumps(
                {
                    "kind": "task_requalification",
                    "target_task_id": task_id,
                    "reason": reason,
                    "evidence": evidence,
                    "evidence_digest": evidence_digest,
                    "qualifier_revision": REQUALIFICATION_QUALIFIER_REVISION,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            intake_id = kanban_db.create_qualification_intake(
                conn,
                raw_request=raw_request,
                source="hermes-reconcile",
            )
            kanban_db._append_event(
                conn,
                task_id,
                "requalification_requested",
                {"intake_id": intake_id, "reason": reason},
            )

    _wake_intake_qualifier()
    return {
        "status": "requalification_required",
        "created": True,
        "intake_id": intake_id,
        "intake_status": "pending",
        "task_id": task_id,
    }


def submit_intake(
    conn: Any,
    *,
    request: Mapping[str, Any],
    source: str,
    session_id: Optional[str] = None,
    attachments: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Persist one untrusted create request and return a stable receipt."""

    from hermes_cli import kanban_db

    raw_request = json.dumps(
        {"kind": "task_create", "request": copy.deepcopy(dict(request))},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    intake_id = kanban_db.create_qualification_intake(
        conn,
        raw_request=raw_request,
        source=source,
        session_id=session_id,
        attachments=attachments,
    )
    # Same-process gateway submissions wake the embedded qualifier now. Other
    # processes are recovered by its normal bounded sweep.
    _wake_intake_qualifier()
    return {
        "status": "qualification_required",
        "intake_id": intake_id,
        "intake_status": "pending",
    }


def _apply_requalification(
    conn: Any,
    *,
    intake_record: Mapping[str, Any],
    contract_id: str,
    fields: Mapping[str, Any],
) -> str:
    """Apply a successor Work Contract to its existing scheduled card."""

    from hermes_cli import kanban_db

    target_task_id = requalification_target_id(intake_record)
    if target_task_id is None:
        raise WorkContractError("requalification intake is missing its target task")

    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (target_task_id,)
    ).fetchone()
    if row is None:
        raise WorkContractError(f"unknown requalification task: {target_task_id}")
    if row["status"] != "scheduled":
        raise WorkContractError("requalification target is no longer scheduled")
    if row["current_run_id"] is not None or row["claim_lock"] is not None:
        raise WorkContractError("cannot requalify a task with an active worker")
    if row["work_contract_id"] is None:
        raise WorkContractError("requalification requires a qualified task")
    if fields["work_item_kind"] != row["work_item_kind"]:
        raise WorkContractError("requalification must preserve the work item kind")

    old_contract_id = str(row["work_contract_id"])
    parents = tuple(dict.fromkeys(str(item) for item in fields["parents"]))
    if target_task_id in parents:
        raise WorkContractError("a requalified task cannot depend on itself")

    with kanban_db.authorized_governance_write():
        updated = conn.execute(
            """
            UPDATE tasks
               SET title = ?, body = ?, assignee = ?,
                   workflow_template_id = ?, current_step_key = ?,
                   work_contract_id = ?, work_item_kind = ?
             WHERE id = ?
               AND status = 'scheduled'
               AND current_run_id IS NULL
               AND claim_lock IS NULL
               AND work_contract_id = ?
            """,
            (
                str(fields["title"]),
                str(fields["body"]),
                fields["assignee"],
                fields["workflow_template_id"],
                fields["current_step_key"],
                contract_id,
                fields["work_item_kind"],
                target_task_id,
                old_contract_id,
            ),
        )
        if updated.rowcount != 1:
            raise WorkContractError("requalification target changed during qualification")

        conn.execute("DELETE FROM task_links WHERE child_id = ?", (target_task_id,))
        for parent_id in parents:
            kanban_db.link_tasks(conn, parent_id, target_task_id)

        conn.execute(
            "DELETE FROM epic_memberships WHERE task_id = ?", (target_task_id,)
        )
        epic_id = fields.get("epic_id")
        if epic_id:
            kanban_db.add_epic_membership(
                conn, epic_id=str(epic_id), task_id=target_task_id
            )

        kanban_db._append_event(
            conn,
            target_task_id,
            "requalified",
            {
                "intake_id": str(intake_record["id"]),
                "old_work_contract_id": old_contract_id,
                "new_work_contract_id": contract_id,
                "entry_phase": fields["current_step_key"],
            },
        )
        if not kanban_db.unblock_task(conn, target_task_id):
            raise WorkContractError("requalification target could not resume")
    return target_task_id


def _epic_story_contract(
    *,
    epic_contract: Mapping[str, Any],
    story: Mapping[str, Any],
    epic_id: str,
    dependencies: list[str],
    board_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one first-phase story contract from a signed Epic decomposition."""

    policy = board_metadata["qualification"]
    phase_assignees = policy["phase_assignees"]
    phases = list(phase_assignees)
    if not phases:
        raise WorkContractError("strict board has no workflow phases")
    entry_phase = phases[0]
    assignee = phase_assignees[entry_phase]
    if assignee is None:
        raise WorkContractError("the first workflow phase must have an assignee")
    next_phase = phases[1] if len(phases) > 1 else "done"
    next_role = phase_assignees.get(next_phase)
    intake_labels = [
        label
        for label in epic_contract["classification"]
        if isinstance(label, str) and label.startswith("intake:")
    ]
    contract = {
        "version": epic_contract["version"],
        "policy_version": epic_contract["policy_version"],
        "qualification_path": epic_contract["qualification_path"],
        "request_id": epic_contract["request_id"],
        "work": {
            "item_kind": "card",
            "work_type": "story",
            "title": story["title"],
            "outcome": story["outcome"],
            "scope": copy.deepcopy(story["scope"]),
            "out_of_scope": copy.deepcopy(story["out_of_scope"]),
        },
        "routing": {
            "entry_phase": entry_phase,
            "assignee": assignee,
            "epic_id": epic_id,
            "dependencies": dependencies,
        },
        "entry_assessment": {
            "reason": "Qualified Epic member starts at the first workflow phase",
            "skipped_phases": [],
            "evidence": [],
        },
        "handover": {
            "deliverables": [story["outcome"]],
            "required_evidence": copy.deepcopy(story["done_when"]),
            "done_when": copy.deepcopy(story["done_when"]),
            "next_phase": next_phase,
            "next_role": next_role,
        },
        "rules": {
            "allowed": ["Deliver only this qualified story through the normal workflow"],
            "forbidden": [
                "Bypass Hermes-owned workflow routing",
                "Expand beyond the qualified Epic outcome",
            ],
        },
        "classification": [
            "framework:story",
            f"path:{epic_contract['qualification_path']}",
            "epic-member",
            *intake_labels,
        ],
        "issuer": copy.deepcopy(epic_contract["issuer"]),
    }
    for field in ("po_evidence", "override_authority"):
        if field in epic_contract:
            contract[field] = copy.deepcopy(epic_contract[field])
    return contract


def _create_materialized_task(
    conn: Any,
    *,
    board: str,
    board_metadata: Mapping[str, Any],
    contract: Mapping[str, Any],
    contract_id: str,
    fields: Mapping[str, Any],
) -> str:
    from hermes_cli import kanban_db

    workspace_kind = (
        "worktree"
        if (
            fields["work_item_kind"] == "card"
            and isinstance(board_metadata.get("product_workflow"), dict)
            and board_metadata["product_workflow"].get("handoff_v2") is True
        )
        else "scratch"
    )
    task_id = kanban_db.create_task(
        conn,
        title=str(fields["title"]),
        body=str(fields["body"]),
        assignee=fields["assignee"],
        created_by="hermes-qualification",
        parents=tuple(fields["parents"]),
        board=board,
        workflow_template_id=fields["workflow_template_id"],
        current_step_key=fields["current_step_key"],
        work_contract_id=contract_id,
        work_item_kind=fields["work_item_kind"],
        workspace_kind=workspace_kind,
    )
    epic_id = fields.get("epic_id")
    if epic_id:
        kanban_db.add_epic_membership(
            conn, epic_id=str(epic_id), task_id=task_id
        )
    kanban_db._append_event(
        conn,
        task_id,
        "contract_materialized",
        {
            "request_id": contract["request_id"],
            "work_contract_id": contract_id,
            "contract_digest": fields["contract_digest"],
            "qualification_path": contract["qualification_path"],
            "classification": fields["classification"],
        },
    )
    return task_id


def materialize_contract(
    conn: Any,
    *,
    board: str,
    signed_contract: Mapping[str, Any],
    secret: Optional[bytes] = None,
    hermes_home: Optional[Path] = None,
) -> str:
    """Atomically materialize a standalone card or an Epic and its stories."""

    from hermes_cli import kanban_db

    metadata = kanban_db.read_board_metadata(board)
    if not qualification_required(metadata):
        raise WorkContractError("contract materialization requires a strict board")
    fields = materialization_fields(
        metadata,
        signed_contract=signed_contract,
        secret=secret,
        hermes_home=hermes_home,
    )
    contract = signed_contract["contract"]
    request_id = contract["request_id"]

    with kanban_db.write_txn(conn):
        existing = conn.execute(
            """
            SELECT tasks.id
            FROM tasks
            JOIN work_contracts ON work_contracts.id = tasks.work_contract_id
            WHERE work_contracts.digest = ?
            """,
            (signed_contract["digest"],),
        ).fetchone()
        if existing:
            return str(existing["id"])

        intake_record = kanban_db.get_qualification_intake(conn, request_id)
        if intake_record is None:
            raise WorkContractError(f"unknown qualification intake: {request_id}")
        allowed_intake_statuses = (
            {"pending", "rejected"}
            if contract["qualification_path"] == "override"
            else {"pending"}
        )
        if intake_record["status"] not in allowed_intake_statuses:
            raise WorkContractError(
                f"qualification intake {request_id} is already {intake_record['status']}"
            )
        payload = intake_payload(intake_record)
        is_requalification = payload.get("kind") == "task_requalification"
        if is_requalification and contract["qualification_path"] == "override":
            raise WorkContractError(
                "requalification cannot use the break-glass override"
            )

        from hermes_cli import kanban_qualifier

        try:
            kanban_qualifier.revalidate_contract_evidence(
                conn,
                board_metadata=metadata,
                intake=intake_record,
                contract=contract,
            )
        except kanban_qualifier.QualificationValidationError as exc:
            raise WorkContractError(str(exc)) from exc

        contract_id = kanban_db.store_work_contract(
            conn,
            dict(signed_contract),
            secret=secret,
            hermes_home=hermes_home,
        )

        is_override = contract["qualification_path"] == "override"
        override_authority = contract.get("override_authority") or {}
        decision_reason = "Work Contract validated and materialized"
        if is_override:
            decision_reason = (
                "Authenticated Ole-to-Hermes override; "
                f"session={override_authority['source_session']}; "
                f"instruction={override_authority['instruction_ref']}; "
                f"reason={override_authority['reason']}"
            )
        kanban_db.record_qualification_decision(
            conn,
            intake_id=request_id,
            decision="overridden" if is_override else "qualified",
            actor_profile=str(contract["issuer"]["profile"]),
            reason=decision_reason,
            contract_id=contract_id,
        )
        with kanban_db.authorized_governance_write():
            if is_requalification:
                task_id = _apply_requalification(
                    conn,
                    intake_record=intake_record,
                    contract_id=contract_id,
                    fields=fields,
                )
            else:
                task_id = _create_materialized_task(
                    conn,
                    board=board,
                    board_metadata=metadata,
                    contract=contract,
                    contract_id=contract_id,
                    fields=fields,
                )
                story_task_ids: list[str] = []
                for story in contract.get("stories", []):
                    dependency_ids = [
                        story_task_ids[index] for index in story["depends_on"]
                    ]
                    story_contract = _epic_story_contract(
                        epic_contract=contract,
                        story=story,
                        epic_id=task_id,
                        dependencies=dependency_ids,
                        board_metadata=metadata,
                    )
                    signed_story = sign_work_contract(
                        story_contract,
                        secret=secret,
                        hermes_home=hermes_home,
                    )
                    story_fields = materialization_fields(
                        metadata,
                        signed_contract=signed_story,
                        secret=secret,
                        hermes_home=hermes_home,
                    )
                    story_contract_id = kanban_db.store_work_contract(
                        conn,
                        signed_story,
                        secret=secret,
                        hermes_home=hermes_home,
                    )
                    story_task_ids.append(
                        _create_materialized_task(
                            conn,
                            board=board,
                            board_metadata=metadata,
                            contract=story_contract,
                            contract_id=story_contract_id,
                            fields=story_fields,
                        )
                    )
                if story_task_ids:
                    kanban_db._append_event(
                        conn,
                        task_id,
                        "epic_decomposed",
                        {"story_task_ids": story_task_ids},
                    )
        if is_requalification:
            kanban_db._append_event(
                conn,
                task_id,
                "contract_materialized",
                {
                    "request_id": request_id,
                    "work_contract_id": contract_id,
                    "contract_digest": fields["contract_digest"],
                    "qualification_path": contract["qualification_path"],
                    "classification": fields["classification"],
                },
            )
    return task_id
