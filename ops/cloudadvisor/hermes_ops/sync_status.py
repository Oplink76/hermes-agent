"""Durable, secret-free status for autonomous upstream synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sync_controller import AutonomousSyncResult, AutonomousSyncState


_TERMINAL_SUCCESS = {
    AutonomousSyncState.NO_CHANGE.value,
    AutonomousSyncState.DEPLOYED.value,
    AutonomousSyncState.ROLLED_BACK_REVERTED.value,
}
_PR_PENDING = {
    AutonomousSyncState.REFRESH_REQUIRED.value,
    AutonomousSyncState.PENDING_REFRESH.value,
}


def _state_value(result: AutonomousSyncResult) -> str:
    state = result.state
    return state.value if isinstance(state, AutonomousSyncState) else str(state)


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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
    sync_state: str
    sync_pr_number: int | None
    required_check: str | None
    fork_main_sha: str | None
    installed_sha: str | None
    escalation_fingerprint: str | None

    @property
    def converged(self) -> bool:
        return self.sync_state in _TERMINAL_SUCCESS and self.upstream_behind in {
            None,
            0,
        }

    def write(self, path: Path) -> None:
        _write_json_atomic(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> "SyncStatus":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("sync status must contain a JSON object")
        expected = {field.name for field in cls.__dataclass_fields__.values()}
        if set(payload) != expected:
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


def _escalation_fingerprint(result: AutonomousSyncResult) -> str | None:
    if _state_value(result) != AutonomousSyncState.NEEDS_OLE.value:
        return None
    explicit = getattr(result, "escalation_fingerprint", None)
    if explicit:
        return _optional_string(explicit, field="escalation_fingerprint")
    evidence = {
        "candidate_sha": result.candidate_sha,
        "installed_sha": result.installed_sha,
        "merge_sha": result.merge_sha,
        "reason": result.reason,
        "state": _state_value(result),
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def status_from_result(result: AutonomousSyncResult) -> SyncStatus:
    """Convert one controller result into the canonical public status shape."""

    raw_state = _state_value(result)
    sync_state = "PR_UPDATED" if raw_state in _PR_PENDING else raw_state
    checked_at = (
        getattr(result, "checked_at", None) or datetime.now(timezone.utc).isoformat()
    )
    return SyncStatus(
        schema_version=1,
        checked_at=_optional_string(checked_at, field="checked_at") or "",
        upstream_behind=_optional_nonnegative_int(
            getattr(result, "upstream_behind", None), field="upstream_behind"
        ),
        sync_state=sync_state,
        sync_pr_number=_optional_nonnegative_int(
            getattr(result, "sync_pr_number", getattr(result, "pr_number", None)),
            field="sync_pr_number",
        ),
        required_check=_optional_string(
            getattr(result, "required_check", None), field="required_check"
        ),
        fork_main_sha=_optional_string(result.fork_main_sha, field="fork_main_sha"),
        installed_sha=_optional_string(result.installed_sha, field="installed_sha"),
        escalation_fingerprint=_escalation_fingerprint(result),
    )


class SyncNotificationStore:
    """Remember escalation fingerprints so cron alerts Ole only once."""

    def __init__(self, path: Path):
        self.path = path

    def _fingerprints(self) -> set[str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return set()
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return set()
        values = payload.get("fingerprints")
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            return set()
        return set(values)

    def should_notify(self, result: AutonomousSyncResult) -> bool:
        fingerprint = _escalation_fingerprint(result)
        return fingerprint is not None and fingerprint not in self._fingerprints()

    def record_notified(self, result: AutonomousSyncResult) -> None:
        fingerprint = _escalation_fingerprint(result)
        if fingerprint is None:
            raise ValueError("only NEEDS_OLE results can be recorded")
        fingerprints = self._fingerprints()
        fingerprints.add(fingerprint)
        _write_json_atomic(
            self.path,
            {"schema_version": 1, "fingerprints": sorted(fingerprints)},
        )
