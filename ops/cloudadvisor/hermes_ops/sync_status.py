"""Collect and publish CloudAdvisor's packaged upstream-sync status."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hermes_cli.upstream_sync_status import (
    SyncStatus,
    write_json_atomic,
)

from .command import CommandRunner
from .sync import SyncConfig
from .sync_controller import AutonomousSyncResult, AutonomousSyncState


@dataclass(frozen=True)
class SyncStatusContext:
    sync: SyncConfig
    install_root: Path
    required_check: str


def _state_value(result: AutonomousSyncResult) -> str:
    state = result.state
    return state.value if isinstance(state, AutonomousSyncState) else str(state)


def escalation_fingerprint(result: AutonomousSyncResult) -> str | None:
    if _state_value(result) != AutonomousSyncState.NEEDS_OLE.value:
        return None
    evidence = {
        "affected_files": list(result.affected_files),
        "candidate_sha": result.candidate_sha,
        "details_artifact": result.details_artifact,
        "failed_gate": result.failed_gate,
        "installed_sha": result.installed_sha,
        "merge_sha": result.merge_sha,
        "pr_number": result.pr_number,
        "reason_code": result.reason_code,
        "revert_sha": result.revert_sha,
        "revert_state": result.revert_state,
        "rollback_sha": result.rollback_sha,
        "rollback_state": result.rollback_state,
        "state": _state_value(result),
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _git_output(
    runner: CommandRunner,
    cwd: Path,
    argv: list[str],
) -> str | None:
    try:
        completed = runner.run(argv, cwd=cwd, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and value else None


def _git_count(
    runner: CommandRunner,
    cwd: Path,
    revision_range: str,
) -> int | None:
    value = _git_output(
        runner,
        cwd,
        ["git", "rev-list", "--count", revision_range],
    )
    if value is None:
        return None
    try:
        count = int(value)
    except ValueError:
        return None
    return count if count >= 0 else None


def status_from_result(
    result: AutonomousSyncResult,
    *,
    context: SyncStatusContext,
    runner: CommandRunner,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SyncStatus:
    origin_main = f"refs/remotes/{context.sync.origin}/main"
    upstream_main = f"refs/remotes/{context.sync.upstream}/main"
    fork_main_sha = _git_output(
        runner,
        context.sync.repo,
        ["git", "rev-parse", origin_main],
    )
    installed_sha = _git_output(
        runner,
        context.install_root,
        ["git", "rev-parse", "HEAD"],
    )
    return SyncStatus(
        schema_version=1,
        checked_at=now().astimezone(timezone.utc).isoformat(),
        upstream_behind=_git_count(
            runner,
            context.sync.repo,
            f"{origin_main}..{upstream_main}",
        ),
        fork_behind=_git_count(
            runner,
            context.install_root,
            f"HEAD..{origin_main}",
        ),
        sync_state=_state_value(result),
        sync_pr_number=result.pr_number,
        required_check=context.required_check,
        fork_main_sha=result.fork_main_sha or fork_main_sha,
        installed_sha=installed_sha,
        escalation_fingerprint=escalation_fingerprint(result),
    )


@dataclass(frozen=True)
class SyncDecisionOutboxRecord:
    status: str
    escalation_fingerprint: str
    packet_path: str
    packet_sha256: str
    idempotency_key: str


class SyncDecisionOutbox:
    """Durable at-least-once handoff; acknowledgement follows delivery."""

    _FIELDS = {
        "schema_version",
        "status",
        "escalation_fingerprint",
        "packet_path",
        "packet_sha256",
        "idempotency_key",
    }

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _digest(value: object, *, field: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{field} must be 64 lowercase hex characters")
        return value

    def load(self) -> SyncDecisionOutboxRecord | None:
        try:
            metadata = self.path.lstat()
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("sync decision outbox is unreadable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
            or not isinstance(payload, dict)
            or set(payload) != self._FIELDS
            or payload.get("schema_version") != 2
            or payload.get("status") not in {"pending", "acknowledged"}
            or raw
            != json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ):
            raise ValueError("sync decision outbox schema is invalid")
        packet_path = payload.get("packet_path")
        if not isinstance(packet_path, str) or not Path(packet_path).is_absolute():
            raise ValueError("sync decision outbox packet path is invalid")
        return SyncDecisionOutboxRecord(
            status=payload["status"],
            escalation_fingerprint=self._digest(
                payload.get("escalation_fingerprint"), field="fingerprint"
            ),
            packet_path=packet_path,
            packet_sha256=self._digest(
                payload.get("packet_sha256"), field="packet digest"
            ),
            idempotency_key=self._digest(
                payload.get("idempotency_key"), field="idempotency key"
            ),
        )

    def stage(
        self,
        *,
        fingerprint: str,
        packet_path: Path,
        packet_sha256: str,
    ) -> bool:
        fingerprint = self._digest(fingerprint, field="fingerprint")
        packet_sha256 = self._digest(packet_sha256, field="packet digest")
        path = Path(packet_path).expanduser().resolve(strict=False)
        idempotency_key = hashlib.sha256(
            f"{fingerprint}:{packet_sha256}".encode("ascii")
        ).hexdigest()
        requested = SyncDecisionOutboxRecord(
            status="pending",
            escalation_fingerprint=fingerprint,
            packet_path=str(path),
            packet_sha256=packet_sha256,
            idempotency_key=idempotency_key,
        )
        current = self.load()
        if current is not None and current.status == "pending" and current != requested:
            raise ValueError("a different sync decision is still pending delivery")
        if current is not None and current.status == "acknowledged" and (
            current.escalation_fingerprint,
            current.packet_sha256,
            current.idempotency_key,
        ) == (
            requested.escalation_fingerprint,
            requested.packet_sha256,
            requested.idempotency_key,
        ):
            return False
        if current != requested:
            write_json_atomic(
                self.path,
                {"schema_version": 2, **asdict(requested)},
            )
        return True

    def acknowledge(
        self,
        *,
        fingerprint: str,
        packet_sha256: str,
        idempotency_key: str,
    ) -> None:
        current = self.load()
        expected = (
            self._digest(fingerprint, field="fingerprint"),
            self._digest(packet_sha256, field="packet digest"),
            self._digest(idempotency_key, field="idempotency key"),
        )
        if current is None or current.status != "pending" or (
            current.escalation_fingerprint,
            current.packet_sha256,
            current.idempotency_key,
        ) != expected:
            raise ValueError("delivery acknowledgement does not match pending packet")
        write_json_atomic(
            self.path,
            {
                "schema_version": 2,
                **asdict(replace(current, status="acknowledged")),
            },
        )

    def clear_resolved(self) -> None:
        current = self.load()
        if current is not None and current.status == "pending":
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
