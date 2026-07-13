"""Packaged schema and persistence for official-upstream synchronization status."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


_TERMINAL_SUCCESS = {"NO_CHANGE", "DEPLOYED"}
_ACTIVE_STATES = {"PR_UPDATED", "PENDING_REFRESH"}
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DEPLOYMENT_FIELDS = {
    "schema_version",
    "repo_slug",
    "stage",
    "candidate_sha",
    "candidate_tree_sha",
    "pr_number",
    "pr_head_sha",
    "base_sha",
    "upstream_sha",
    "premerge_receipt_path",
    "premerge_receipt_sha256",
    "merge_sha",
    "final_receipt_path",
    "final_receipt_sha256",
    "install_root",
    "previous_installed_sha",
    "terminal_reason",
    "terminal_reason_code",
    "terminal_failed_gate",
    "rollback_state",
    "rollback_sha",
    "revert_state",
    "revert_sha",
}


def _optional_nonnegative_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string or null")
    return value.strip()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class SyncStatus:
    schema_version: int
    checked_at: str
    upstream_behind: int | None
    fork_behind: int | None
    sync_state: str
    sync_pr_number: int | None
    required_check: str | None
    fork_main_sha: str | None
    installed_sha: str | None
    escalation_fingerprint: str | None

    @property
    def converged(self) -> bool:
        return (
            self.sync_state in _TERMINAL_SUCCESS
            and self.upstream_behind == 0
            and self.fork_behind == 0
            and self.fork_main_sha is not None
            and self.fork_main_sha == self.installed_sha
        )

    def write(self, path: Path) -> None:
        write_json_atomic(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> "SyncStatus":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("sync status must contain a JSON object")
        if set(payload) != {field.name for field in fields(cls)}:
            raise ValueError("sync status fields do not match schema")
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported sync status schema")
        checked_at = _optional_string(payload.get("checked_at"), field="checked_at")
        sync_state = _optional_string(payload.get("sync_state"), field="sync_state")
        if checked_at is None or sync_state is None:
            raise ValueError("sync status identity is incomplete")
        return cls(
            schema_version=1,
            checked_at=checked_at,
            upstream_behind=_optional_nonnegative_int(
                payload.get("upstream_behind"), field="upstream_behind"
            ),
            fork_behind=_optional_nonnegative_int(
                payload.get("fork_behind"), field="fork_behind"
            ),
            sync_state=sync_state,
            sync_pr_number=_optional_nonnegative_int(
                payload.get("sync_pr_number"), field="sync_pr_number"
            ),
            required_check=_optional_string(
                payload.get("required_check"), field="required_check"
            ),
            fork_main_sha=_optional_string(
                payload.get("fork_main_sha"), field="fork_main_sha"
            ),
            installed_sha=_optional_string(
                payload.get("installed_sha"), field="installed_sha"
            ),
            escalation_fingerprint=_optional_string(
                payload.get("escalation_fingerprint"),
                field="escalation_fingerprint",
            ),
        )


def installed_sync_message(
    *,
    installed_current: bool,
    upstream_behind: int | None,
    sync_state: str | None,
) -> str | None:
    if not installed_current:
        return None
    if sync_state == "NEEDS_OLE":
        return "Installed current · Official upstream sync needs attention"
    if sync_state == "LOCKED":
        return "Installed current · Official upstream sync already running"
    if upstream_behind is None or upstream_behind <= 0:
        return None
    noun = "commit" if upstream_behind == 1 else "commits"
    suffix = "syncing" if sync_state in _ACTIVE_STATES else "pending"
    if sync_state == "ROLLED_BACK_REVERTED":
        suffix = "pending after safe rollback"
    return f"Installed current · {upstream_behind} official upstream {noun} {suffix}"


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class PendingDeploymentEvidence:
    artifact_sha256: str
    payload: dict[str, Any]


def _read_trusted_file(path: Path, *, mode: int) -> bytes:
    try:
        metadata = path.lstat()
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError("deployment checkpoint file is unreadable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise ValueError("deployment checkpoint file is untrusted")
    return content


def _decode_exact(content: bytes, *, fields: set[str]) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("deployment checkpoint JSON is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != fields
        or _canonical_json(payload) != content
    ):
        raise ValueError("deployment checkpoint schema is invalid")
    return payload


def _valid_repo_slug(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and value.count("/") == 1
        and all(value.split("/"))
    )


def load_deployment_artifact_payload(
    path: Path,
    *,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    path = Path(path)
    match = re.fullmatch(r"pending-deployment-([0-9a-f]{64})\.json", path.name)
    if match is None:
        raise ValueError("deployment artifact name is invalid")
    digest = match.group(1)
    if expected_digest is not None and digest != expected_digest:
        raise ValueError("deployment artifact digest is crossed")
    content = _read_trusted_file(path, mode=0o400)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("deployment artifact digest is invalid")
    return _decode_exact(content, fields=_DEPLOYMENT_FIELDS)


def load_pending_deployment_evidence(
    receipt_root: Path,
    *,
    repo_slug: str | None = None,
) -> PendingDeploymentEvidence | None:
    root = Path(receipt_root).expanduser().resolve(strict=False) / "deployment"
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("deployment checkpoint scope is unreadable") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("deployment checkpoint scope is untrusted")
    pointer = root / "pending.json"
    try:
        pointer.lstat()
    except FileNotFoundError:
        return None
    pointer_payload = _decode_exact(
        _read_trusted_file(pointer, mode=0o600),
        fields={"schema_version", "repo_slug", "artifact_sha256"},
    )
    pointer_repo = pointer_payload.get("repo_slug")
    digest = pointer_payload.get("artifact_sha256")
    if (
        pointer_payload.get("schema_version") != 1
        or not _valid_repo_slug(pointer_repo)
        or _DIGEST.fullmatch(str(digest)) is None
        or (repo_slug is not None and pointer_repo != repo_slug)
    ):
        raise ValueError("deployment pointer identity is invalid")
    payload = load_deployment_artifact_payload(
        root / f"pending-deployment-{digest}.json",
        expected_digest=digest,
    )
    if (
        payload.get("schema_version") != 1
        or payload.get("repo_slug") != pointer_repo
    ):
        raise ValueError("deployment artifact identity is crossed")
    return PendingDeploymentEvidence(artifact_sha256=digest, payload=payload)


def _base_blocking_evidence_valid(payload: dict[str, Any]) -> bool:
    shas = (
        payload.get("candidate_sha"),
        payload.get("candidate_tree_sha"),
        payload.get("pr_head_sha"),
        payload.get("base_sha"),
        payload.get("upstream_sha"),
        payload.get("previous_installed_sha"),
    )
    return (
        all(isinstance(value, str) and _FULL_SHA.fullmatch(value) for value in shas)
        and payload.get("candidate_sha") == payload.get("pr_head_sha")
        and type(payload.get("pr_number")) is int
        and payload["pr_number"] >= 1
        and _DIGEST.fullmatch(str(payload.get("premerge_receipt_sha256"))) is not None
        and isinstance(payload.get("premerge_receipt_path"), str)
        and Path(payload["premerge_receipt_path"]).is_absolute()
        and isinstance(payload.get("install_root"), str)
        and Path(payload["install_root"]).is_absolute()
    )


_TERMINAL_FIELDS = (
    "terminal_reason",
    "terminal_reason_code",
    "terminal_failed_gate",
    "rollback_state",
    "rollback_sha",
    "revert_state",
    "revert_sha",
)


def _merged_blocking_evidence_valid(payload: dict[str, Any]) -> bool:
    return (
        isinstance(payload.get("merge_sha"), str)
        and _FULL_SHA.fullmatch(payload["merge_sha"]) is not None
        and isinstance(payload.get("final_receipt_path"), str)
        and Path(payload["final_receipt_path"]).is_absolute()
        and _DIGEST.fullmatch(str(payload.get("final_receipt_sha256"))) is not None
    )


def _terminal_blocking_evidence_valid(payload: dict[str, Any]) -> bool:
    sha_values = (payload.get("rollback_sha"), payload.get("revert_sha"))
    return (
        isinstance(payload.get("terminal_reason_code"), str)
        and re.fullmatch(r"[A-Z][A-Z0-9_]*", payload["terminal_reason_code"])
        is not None
        and isinstance(payload.get("terminal_failed_gate"), str)
        and re.fullmatch(r"[a-z][a-z0-9_]*", payload["terminal_failed_gate"])
        is not None
        and isinstance(payload.get("rollback_state"), str)
        and isinstance(payload.get("revert_state"), str)
        and all(
            value is None
            or (isinstance(value, str) and _FULL_SHA.fullmatch(value) is not None)
            for value in sha_values
        )
    )


def _blocking_stage(payload: dict[str, Any]) -> str:
    if not _base_blocking_evidence_valid(payload):
        return "crossed_invalid"
    stage = payload.get("stage")
    if stage == "merge_intent":
        crossed = (
            payload.get("merge_sha") is not None
            or payload.get("final_receipt_path") is not None
            or payload.get("final_receipt_sha256") is not None
            or any(payload.get(field) is not None for field in _TERMINAL_FIELDS)
        )
        return "crossed_invalid" if crossed else stage
    if stage not in {"merged_pending_deploy", "failed_terminal"}:
        return "crossed_invalid"
    if not _merged_blocking_evidence_valid(payload):
        return "crossed_invalid"
    if stage == "merged_pending_deploy":
        return (
            "crossed_invalid"
            if any(payload.get(field) is not None for field in _TERMINAL_FIELDS)
            else stage
        )
    return stage if _terminal_blocking_evidence_valid(payload) else "crossed_invalid"


def pending_deployment_state(receipt_root: Path) -> str | None:
    """Inspect packaged checkpoint evidence; malformed/crossed state blocks."""
    try:
        evidence = load_pending_deployment_evidence(receipt_root)
    except ValueError:
        return "crossed_invalid"
    return None if evidence is None else _blocking_stage(evidence.payload)
