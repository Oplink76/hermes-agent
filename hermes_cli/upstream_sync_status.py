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
_ACTIVE_STATES = {"PR_UPDATED", "PENDING_REFRESH", "REFRESH_REQUIRED"}
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


class SyncNotificationState:
    """Persist the active emitted escalation decision, not delivery claims."""

    def __init__(self, path: Path):
        self.path = path

    def _active_fingerprint(self) -> str | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "active_fingerprint",
        }:
            return None
        if payload.get("schema_version") != 1:
            return None
        value = payload.get("active_fingerprint")
        return value if isinstance(value, str) and value else None

    def should_emit(self, fingerprint: str) -> bool:
        fingerprint = _optional_string(fingerprint, field="fingerprint") or ""
        return fingerprint != self._active_fingerprint()

    def record_emitted(self, fingerprint: str) -> None:
        fingerprint = _optional_string(fingerprint, field="fingerprint") or ""
        write_json_atomic(
            self.path,
            {"schema_version": 1, "active_fingerprint": fingerprint},
        )

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def pending_deployment_state(receipt_root: Path) -> str | None:
    """Inspect packaged checkpoint evidence; malformed/crossed state blocks."""
    root = Path(receipt_root).expanduser().resolve(strict=False) / "deployment"
    pointer = root / "pending.json"
    try:
        root_metadata = root.lstat()
        metadata = pointer.lstat()
        pointer_content = pointer.read_bytes()
        pointer_payload = json.loads(pointer_content)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "crossed_invalid"
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
        or not isinstance(pointer_payload, dict)
        or set(pointer_payload)
        != {"schema_version", "repo_slug", "artifact_sha256"}
        or pointer_payload.get("schema_version") != 1
        or _canonical_json(pointer_payload) != pointer_content
        or _DIGEST.fullmatch(str(pointer_payload.get("artifact_sha256"))) is None
    ):
        return "crossed_invalid"
    repo_slug = pointer_payload.get("repo_slug")
    if (
        not isinstance(repo_slug, str)
        or repo_slug != repo_slug.strip()
        or repo_slug.count("/") != 1
        or not all(repo_slug.split("/"))
    ):
        return "crossed_invalid"
    digest = pointer_payload["artifact_sha256"]
    artifact = root / f"pending-deployment-{digest}.json"
    try:
        artifact_metadata = artifact.lstat()
        content = artifact.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "crossed_invalid"
    if (
        stat.S_ISLNK(artifact_metadata.st_mode)
        or not stat.S_ISREG(artifact_metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(artifact_metadata.st_mode) != 0o400)
        or hashlib.sha256(content).hexdigest() != digest
        or not isinstance(payload, dict)
        or set(payload) != _DEPLOYMENT_FIELDS
        or _canonical_json(payload) != content
        or payload.get("schema_version") != 1
        or payload.get("repo_slug") != repo_slug
    ):
        return "crossed_invalid"
    shas = (
        payload.get("candidate_sha"),
        payload.get("candidate_tree_sha"),
        payload.get("pr_head_sha"),
        payload.get("base_sha"),
        payload.get("upstream_sha"),
        payload.get("previous_installed_sha"),
    )
    if (
        not all(isinstance(value, str) and _FULL_SHA.fullmatch(value) for value in shas)
        or payload.get("candidate_sha") != payload.get("pr_head_sha")
        or type(payload.get("pr_number")) is not int
        or payload["pr_number"] < 1
        or _DIGEST.fullmatch(str(payload.get("premerge_receipt_sha256"))) is None
        or not isinstance(payload.get("premerge_receipt_path"), str)
        or not Path(payload["premerge_receipt_path"]).is_absolute()
        or not isinstance(payload.get("install_root"), str)
        or not Path(payload["install_root"]).is_absolute()
    ):
        return "crossed_invalid"
    stage = payload.get("stage")
    terminal_fields = (
        "terminal_reason",
        "terminal_reason_code",
        "terminal_failed_gate",
        "rollback_state",
        "rollback_sha",
        "revert_state",
        "revert_sha",
    )
    if stage == "merge_intent":
        crossed = (
            payload.get("merge_sha") is not None
            or payload.get("final_receipt_path") is not None
            or payload.get("final_receipt_sha256") is not None
            or any(payload.get(field) is not None for field in terminal_fields)
        )
        return "crossed_invalid" if crossed else stage
    if stage not in {"merged_pending_deploy", "failed_terminal"}:
        return "crossed_invalid"
    if (
        not isinstance(payload.get("merge_sha"), str)
        or _FULL_SHA.fullmatch(payload["merge_sha"]) is None
        or not isinstance(payload.get("final_receipt_path"), str)
        or not Path(payload["final_receipt_path"]).is_absolute()
        or _DIGEST.fullmatch(str(payload.get("final_receipt_sha256"))) is None
    ):
        return "crossed_invalid"
    if stage == "merged_pending_deploy":
        return (
            "crossed_invalid"
            if any(payload.get(field) is not None for field in terminal_fields)
            else stage
        )
    if (
        not isinstance(payload.get("terminal_reason_code"), str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", payload["terminal_reason_code"]) is None
        or not isinstance(payload.get("terminal_failed_gate"), str)
        or re.fullmatch(r"[a-z][a-z0-9_]*", payload["terminal_failed_gate"]) is None
        or not isinstance(payload.get("rollback_state"), str)
        or not isinstance(payload.get("revert_state"), str)
    ):
        return "crossed_invalid"
    for field in ("rollback_sha", "revert_sha"):
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None
        ):
            return "crossed_invalid"
    return stage
