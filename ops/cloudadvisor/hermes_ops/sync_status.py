"""Collect and publish CloudAdvisor's packaged upstream-sync status."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hermes_cli.upstream_sync_status import (
    SyncNotificationState,
    SyncStatus,
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
        "candidate_sha": result.candidate_sha,
        "installed_sha": result.installed_sha,
        "merge_sha": result.merge_sha,
        "pr_number": result.pr_number,
        "reason": result.reason,
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


class SyncNotificationStore:
    """Result-aware facade over the packaged active-decision state."""

    def __init__(self, path: Path):
        self._state = SyncNotificationState(path)

    def should_emit(self, result: AutonomousSyncResult) -> bool:
        fingerprint = escalation_fingerprint(result)
        return fingerprint is not None and self._state.should_emit(fingerprint)

    def record_emitted(self, result: AutonomousSyncResult) -> None:
        fingerprint = escalation_fingerprint(result)
        if fingerprint is None:
            raise ValueError("only NEEDS_OLE decisions can be recorded")
        self._state.record_emitted(fingerprint)

    def clear_resolved(self) -> None:
        self._state.clear()
