from __future__ import annotations

import json
from pathlib import Path

from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncResult,
    AutonomousSyncState,
)
from ops.cloudadvisor.hermes_ops.sync_status import (
    SyncNotificationStore,
    SyncStatus,
    status_from_result,
)


def _result(
    state: AutonomousSyncState,
    *,
    candidate_sha: str = "a" * 40,
    reason: str | None = None,
) -> AutonomousSyncResult:
    return AutonomousSyncResult(
        state=state,
        candidate_sha=candidate_sha,
        needs_ole=state is AutonomousSyncState.NEEDS_OLE,
        reason=reason,
    )


def test_pr_updated_is_not_terminal_success() -> None:
    status = status_from_result(_result(AutonomousSyncState.PENDING_REFRESH))

    assert status.sync_state == "PR_UPDATED"
    assert status.converged is False


def test_same_needs_ole_fingerprint_notified_once(tmp_path: Path) -> None:
    store = SyncNotificationStore(tmp_path / "notifications.json")
    packet = _result(AutonomousSyncState.NEEDS_OLE, reason="major conflict")

    assert store.should_notify(packet) is True
    store.record_notified(packet)
    assert store.should_notify(packet) is False


def test_notification_fingerprint_changes_with_escalation_evidence(
    tmp_path: Path,
) -> None:
    store = SyncNotificationStore(tmp_path / "notifications.json")
    first = _result(AutonomousSyncState.NEEDS_OLE, reason="major conflict")
    second = _result(
        AutonomousSyncState.NEEDS_OLE,
        candidate_sha="b" * 40,
        reason="major conflict",
    )

    store.record_notified(first)

    assert store.should_notify(second) is True


def test_non_escalation_result_never_notifies(tmp_path: Path) -> None:
    store = SyncNotificationStore(tmp_path / "notifications.json")

    assert store.should_notify(_result(AutonomousSyncState.DEPLOYED)) is False


def test_status_write_is_canonical_atomic_and_excludes_reason(tmp_path: Path) -> None:
    status_path = tmp_path / "state" / "sync-status.json"
    result = _result(
        AutonomousSyncState.NEEDS_OLE,
        reason="raw command output must not be persisted",
    )
    status = status_from_result(result)

    status.write(status_path)

    raw = status_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert raw == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    assert "reason" not in payload
    assert SyncStatus.load(status_path) == status
