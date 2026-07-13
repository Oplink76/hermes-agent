from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.cron_wrapper import (
    CronWrapperConfig,
    run_health,
    run_sync_auto,
)
from ops.cloudadvisor.hermes_ops.decision_packet import (
    publish_escalation_decision_packet,
)
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncResult,
    AutonomousSyncState,
)
from ops.cloudadvisor.hermes_ops.sync_status import SyncDecisionOutbox


class FakeRun:
    def __init__(
        self,
        completed: subprocess.CompletedProcess[str],
        before_return=None,
    ):
        self.completed = completed
        self.before_return = before_return
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append(argv)
        if self.before_return is not None:
            self.before_return()
        return self.completed


class SequenceRun:
    def __init__(self, completed: list[subprocess.CompletedProcess[str]]):
        self.completed = iter(completed)

    def __call__(self, argv: list[str], **kwargs):
        return next(self.completed)


def _config(tmp_path: Path) -> CronWrapperConfig:
    return CronWrapperConfig(
        python=tmp_path / "python",
        install_root=tmp_path / "repo",
        operations_config=tmp_path / "operations.yaml",
        trusted_root=tmp_path / "receipts",
        outbox_store=tmp_path / "sync-notifications.json",
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "state": "NO_CHANGE",
        "candidate_sha": None,
        "pr_number": None,
        "merge_sha": None,
        "deployed_sha": None,
        "fork_main_sha": None,
        "installed_sha": None,
        "needs_ole": False,
        "reason": None,
        "reason_code": None,
        "failed_gate": None,
        "repo_slug": "Oplink76/hermes-agent",
        "affected_files": [],
        "rollback_state": None,
        "rollback_sha": None,
        "revert_state": None,
        "revert_sha": None,
        "details_artifact": None,
        "checked_at": "2026-07-13T00:00:00+00:00",
        "upstream_behind": 0,
        "fork_behind": 0,
        "sync_required_check": "All required checks pass",
        "notify_ole": False,
        "escalation_fingerprint": None,
        "decision_packet_path": None,
        "decision_packet_sha256": None,
        "decision_idempotency_key": None,
    }
    payload.update(overrides)
    return payload


def _idempotency(fingerprint: str, packet_sha256: str) -> str:
    return hashlib.sha256(
        f"{fingerprint}:{packet_sha256}".encode("ascii")
    ).hexdigest()


@pytest.mark.parametrize(
    ("state", "returncode"),
    [
        ("NO_CHANGE", 0),
        ("DEPLOYED", 0),
        ("ROLLED_BACK_REVERTED", 0),
        ("PENDING_REFRESH", 75),
        ("LOCKED", 75),
    ],
)
def test_routine_sync_auto_outcomes_are_quiet(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
    returncode: int,
) -> None:
    run = FakeRun(
        subprocess.CompletedProcess(
            [], returncode, stdout=json.dumps(_payload(state=state)), stderr=""
        )
    )

    assert run_sync_auto(_config(tmp_path), run=run) == 0
    assert capsys.readouterr() == ("", "")
    assert run.calls[0][-3:] == [
        "sync-auto",
        "--config",
        str(tmp_path / "operations.yaml"),
    ]


def test_notify_ole_delivers_one_matching_decision_packet(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        candidate_sha="c" * 40,
        pr_number=7,
        merge_sha="d" * 40,
        fork_main_sha="a" * 40,
        installed_sha="b" * 40,
        needs_ole=True,
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
    )
    packet = publish_escalation_decision_packet(
        result,
        fingerprint="1" * 64,
        trusted_root=tmp_path / "receipts",
        repo_slug="Oplink76/hermes-agent",
    )
    outbox = SyncDecisionOutbox(tmp_path / "sync-notifications.json")
    def stage() -> None:
        outbox.stage(
            fingerprint="1" * 64,
            packet_path=packet.path,
            packet_sha256=packet.sha256,
        )
    raw = json.dumps(
        _payload(
            state="NEEDS_OLE",
            candidate_sha="c" * 40,
            pr_number=7,
            merge_sha="d" * 40,
            fork_main_sha="a" * 40,
            installed_sha="b" * 40,
            needs_ole=True,
            reason="not forwarded",
            reason_code="GITHUB_AUTHORITY_INVALID",
            failed_gate="github_authority",
            details_artifact=str(packet.details_path),
            notify_ole=True,
            escalation_fingerprint="1" * 64,
            decision_packet_path=str(packet.path),
            decision_packet_sha256=packet.sha256,
            decision_idempotency_key=_idempotency("1" * 64, packet.sha256),
        )
    )
    run = FakeRun(
        subprocess.CompletedProcess([], 2, stdout=raw, stderr=""),
        before_return=stage,
    )

    assert run_sync_auto(_config(tmp_path), run=run) == 0
    delivered = capsys.readouterr().out
    assert "Hermes upstream sync needs attention" in delivered
    assert "Recommendation: Wait" in delivered
    assert "Approve / Wait / Details" in delivered
    assert "secret" not in delivered

    assert outbox.load().status == "acknowledged"


def test_malformed_sync_output_fails_closed_without_claiming_no_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = FakeRun(subprocess.CompletedProcess([], 0, stdout="not-json", stderr="boom"))

    assert run_sync_auto(_config(tmp_path), run=run) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid sync-auto JSON" in captured.err
    assert "NO_CHANGE" not in captured.err


def test_notify_ole_rejects_packet_with_wrong_fingerprint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        needs_ole=True,
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
    )
    packet = publish_escalation_decision_packet(
        result,
        fingerprint="2" * 64,
        trusted_root=tmp_path / "receipts",
        repo_slug="Oplink76/hermes-agent",
    )
    outbox = SyncDecisionOutbox(tmp_path / "sync-notifications.json")
    def stage() -> None:
        outbox.stage(
            fingerprint="1" * 64,
            packet_path=packet.path,
            packet_sha256=packet.sha256,
        )
    raw = json.dumps(
        _payload(
            state="NEEDS_OLE",
            needs_ole=True,
            reason_code="GITHUB_AUTHORITY_INVALID",
            failed_gate="github_authority",
            details_artifact=str(packet.details_path),
            notify_ole=True,
            escalation_fingerprint="1" * 64,
            decision_packet_path=str(packet.path),
            decision_packet_sha256=packet.sha256,
            decision_idempotency_key=_idempotency("1" * 64, packet.sha256),
        )
    )
    run = FakeRun(
        subprocess.CompletedProcess([], 2, stdout=raw, stderr=""),
        before_return=stage,
    )

    assert run_sync_auto(_config(tmp_path), run=run) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fingerprint" in captured.err


def test_delivery_failure_leaves_pending_outbox_for_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        candidate_sha="c" * 40,
        needs_ole=True,
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
    )
    packet = publish_escalation_decision_packet(
        result,
        fingerprint="3" * 64,
        trusted_root=tmp_path / "receipts",
        repo_slug="Oplink76/hermes-agent",
    )
    outbox = SyncDecisionOutbox(tmp_path / "sync-notifications.json")
    def stage() -> None:
        outbox.stage(
            fingerprint="3" * 64,
            packet_path=packet.path,
            packet_sha256=packet.sha256,
        )
    raw = json.dumps(
        _payload(
            state="NEEDS_OLE",
            candidate_sha="c" * 40,
            needs_ole=True,
            reason_code="GITHUB_AUTHORITY_INVALID",
            failed_gate="github_authority",
            details_artifact=str(packet.details_path),
            notify_ole=True,
            escalation_fingerprint="3" * 64,
            decision_packet_path=str(packet.path),
            decision_packet_sha256=packet.sha256,
            decision_idempotency_key=_idempotency("3" * 64, packet.sha256),
        )
    )
    run = FakeRun(
        subprocess.CompletedProcess([], 2, stdout=raw, stderr=""),
        before_return=stage,
    )

    def fail_delivery(message: str) -> None:
        raise OSError("delivery unavailable")

    assert run_sync_auto(_config(tmp_path), run=run, deliver=fail_delivery) == 2
    assert outbox.load().status == "pending"
    assert "delivery unavailable" in capsys.readouterr().err

    delivered: list[str] = []
    assert run_sync_auto(_config(tmp_path), run=run, deliver=delivered.append) == 0
    assert len(delivered) == 1
    assert outbox.load().status == "acknowledged"


def test_ack_failure_after_delivery_retries_with_same_idempotency_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        needs_ole=True,
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
    )
    packet = publish_escalation_decision_packet(
        result,
        fingerprint="4" * 64,
        trusted_root=tmp_path / "receipts",
        repo_slug="Oplink76/hermes-agent",
    )
    outbox = SyncDecisionOutbox(tmp_path / "sync-notifications.json")
    def stage() -> None:
        outbox.stage(
            fingerprint="4" * 64,
            packet_path=packet.path,
            packet_sha256=packet.sha256,
        )
    idempotency_key = _idempotency("4" * 64, packet.sha256)
    raw = json.dumps(
        _payload(
            state="NEEDS_OLE",
            needs_ole=True,
            reason_code="GITHUB_AUTHORITY_INVALID",
            failed_gate="github_authority",
            details_artifact=str(packet.details_path),
            notify_ole=True,
            escalation_fingerprint="4" * 64,
            decision_packet_path=str(packet.path),
            decision_packet_sha256=packet.sha256,
            decision_idempotency_key=idempotency_key,
        )
    )
    run = FakeRun(
        subprocess.CompletedProcess([], 2, stdout=raw, stderr=""),
        before_return=stage,
    )
    delivered: list[str] = []
    real_acknowledge = SyncDecisionOutbox.acknowledge
    monkeypatch.setattr(
        SyncDecisionOutbox,
        "acknowledge",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ack crash")),
    )

    assert run_sync_auto(_config(tmp_path), run=run, deliver=delivered.append) == 2
    assert len(delivered) == 1
    assert outbox.load().status == "pending"
    assert "ack crash" in capsys.readouterr().err

    monkeypatch.setattr(SyncDecisionOutbox, "acknowledge", real_acknowledge)
    assert run_sync_auto(_config(tmp_path), run=run, deliver=delivered.append) == 0
    assert len(delivered) == 2
    assert outbox.load().status == "acknowledged"
    assert outbox.load().idempotency_key == idempotency_key


def test_health_action_is_quiet_when_installed_runtime_is_healthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha = "a" * 40
    run = SequenceRun([
        subprocess.CompletedProcess([], 0, stdout=f"{sha}\n", stderr=""),
        subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"expected_sha": sha, "healthy": True, "checks": []}),
            stderr="",
        ),
    ])

    assert run_health(_config(tmp_path), run=run) == 0
    assert capsys.readouterr() == ("", "")


def test_health_action_preserves_attention_output_for_failed_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sha = "a" * 40
    run = SequenceRun([
        subprocess.CompletedProcess([], 0, stdout=f"{sha}\n", stderr=""),
        subprocess.CompletedProcess(
            [],
            3,
            stdout=json.dumps({
                "expected_sha": sha,
                "healthy": False,
                "checks": [{"name": "gateway", "passed": False, "detail": "down"}],
            }),
            stderr="",
        ),
    ])

    assert run_health(_config(tmp_path), run=run) == 0
    assert "gateway: down" in capsys.readouterr().out
