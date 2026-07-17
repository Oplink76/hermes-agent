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
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_default_hermes_root


CONTRACT_VERSION = 1
DEFAULT_POLICY_VERSION = "product-handoff-v2+qualification-v1"
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


class WorkContractError(ValueError):
    """The Work Contract is missing, invalid, or violates board policy."""


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
        result = subprocess.run(
            [
                icacls,
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{username}:F",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise WorkContractError("cannot secure Work Contract signing key with Windows ACLs")
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

    routing = contract["routing"]
    phase = routing["entry_phase"]
    assignee = routing["assignee"]
    phase_assignees = policy.get("phase_assignees")
    if not isinstance(phase_assignees, Mapping):
        raise WorkContractError("strict board policy requires a phase_assignees mapping")
    if phase not in phase_assignees or phase_assignees.get(phase) != assignee:
        raise WorkContractError(
            f"phase {phase!r} and assignee {assignee!r} are not an allowed phase/assignee pair"
        )

    work = contract["work"]
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
