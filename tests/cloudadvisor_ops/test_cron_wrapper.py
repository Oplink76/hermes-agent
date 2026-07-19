from __future__ import annotations

import json
import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops import cron_wrapper
from ops.cloudadvisor.hermes_ops.cron_wrapper import (
    CronWrapperConfig,
    run_agent_memory_attention,
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


class RecordingRun:
    def __init__(self, completed: subprocess.CompletedProcess[str]):
        self.completed = completed
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append((argv, kwargs))
        return self.completed


def _config(tmp_path: Path) -> CronWrapperConfig:
    return CronWrapperConfig(
        python=tmp_path / "python",
        install_root=tmp_path / "repo",
        operations_config=tmp_path / "operations.yaml",
        trusted_root=tmp_path / "receipts",
        outbox_store=tmp_path / "sync-notifications.json",
        delivery_command=("hermes", "send", "--to", "slack:C123", "--file", "-"),
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


def _memory_status(**overrides: object) -> dict[str, object]:
    status: dict[str, object] = {
        "enabled": True,
        "vault_available": False,
        "pending": 2,
        "oldest_pending_hours": 25.0,
        "attention_required": True,
        "reason": "pending_for_24_hours",
        "fingerprint": "a" * 64,
        "notify_ole": True,
    }
    status.update(overrides)
    return status


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

    delivered_messages: list[str] = []
    assert run_sync_auto(
        _config(tmp_path), run=run, deliver=delivered_messages.append
    ) == 0
    delivered = delivered_messages[0]
    assert "Hermes upstream sync needs attention" in delivered
    assert "Recommendation: Wait" in delivered
    assert "Approve / Wait / Details" in delivered
    assert "secret" not in delivered
    assert capsys.readouterr().out == ""

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


def test_direct_delivery_acknowledges_only_after_command_success(
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
        fingerprint="5" * 64,
        trusted_root=tmp_path / "receipts",
        repo_slug="Oplink76/hermes-agent",
    )
    outbox = SyncDecisionOutbox(tmp_path / "sync-notifications.json")
    outbox.stage(
        fingerprint="5" * 64,
        packet_path=packet.path,
        packet_sha256=packet.sha256,
    )
    delivery = RecordingRun(subprocess.CompletedProcess([], 0, stdout="", stderr=""))

    assert run_sync_auto(_config(tmp_path), delivery_run=delivery) == 0

    argv, kwargs = delivery.calls[0]
    assert argv == list(_config(tmp_path).delivery_command)
    assert "Decision id:" in str(kwargs["input"])
    assert "GITHUB_AUTHORITY_INVALID" in str(kwargs["input"])
    assert outbox.load().status == "acknowledged"
    assert capsys.readouterr() == ("", "")


def test_direct_delivery_failure_stays_pending_without_leaking_output(
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
        fingerprint="6" * 64,
        trusted_root=tmp_path / "receipts",
        repo_slug="Oplink76/hermes-agent",
    )
    outbox = SyncDecisionOutbox(tmp_path / "sync-notifications.json")
    outbox.stage(
        fingerprint="6" * 64,
        packet_path=packet.path,
        packet_sha256=packet.sha256,
    )
    delivery = RecordingRun(
        subprocess.CompletedProcess(
            [], 1, stdout="token=must-not-leak", stderr="secret=must-not-leak"
        )
    )

    assert run_sync_auto(_config(tmp_path), delivery_run=delivery) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must-not-leak" not in captured.err
    assert outbox.load().status == "pending"


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


def test_scheduled_wrapper_delivers_one_memory_attention_per_fingerprint(
    tmp_path: Path,
) -> None:
    statuses = [
        _memory_status(),
        _memory_status(notify_ole=False),
    ]
    run = SequenceRun(
        [
            subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(status), stderr=""
            )
            for status in statuses
        ]
    )
    sent: list[str] = []
    acknowledged: list[str] = []

    assert run_agent_memory_attention(
        _config(tmp_path),
        run=run,
        deliver=sent.append,
        acknowledge=acknowledged.append,
    ) == 0
    assert run_agent_memory_attention(
        _config(tmp_path),
        run=run,
        deliver=sent.append,
        acknowledge=acknowledged.append,
    ) == 0

    assert len(sent) == 1
    assert "Agent Memory needs attention" in sent[0]
    assert acknowledged == ["a" * 64]


def test_scheduled_memory_check_is_quiet_before_24_hours(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = SequenceRun([
        subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                _memory_status(
                    oldest_pending_hours=23.99,
                    attention_required=False,
                    reason="none",
                    notify_ole=False,
                )
            ),
            stderr="",
        )
    ])

    assert run_agent_memory_attention(_config(tmp_path), run=run) == 0
    assert capsys.readouterr() == ("", "")


def test_scheduled_memory_check_delivers_for_corrupt_envelope(
    tmp_path: Path,
) -> None:
    run = SequenceRun([
        subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                _memory_status(
                    reason="corrupt_or_unsafe",
                    oldest_pending_hours=0.0,
                )
            ),
            stderr="",
        )
    ])
    sent: list[str] = []

    assert run_agent_memory_attention(
        _config(tmp_path),
        run=run,
        deliver=sent.append,
        acknowledge=lambda _fingerprint: None,
    ) == 0
    assert "Reason: unsafe or corrupt outbox entry" in sent[0]


def test_scheduled_memory_attention_never_contains_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "SECRET-GIST-QUERY-PROMPT-CONTENT"
    run = SequenceRun([
        subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(_memory_status()),
            stderr=json.dumps({"gist": sentinel, "query": sentinel}),
        )
    ])
    sent: list[str] = []

    assert run_agent_memory_attention(
        _config(tmp_path),
        run=run,
        deliver=sent.append,
        acknowledge=lambda _fingerprint: None,
    ) == 0
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert sentinel not in sent[0]


def test_scheduled_memory_check_skips_acknowledged_fingerprint(
    tmp_path: Path,
) -> None:
    run = SequenceRun([
        subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(_memory_status(notify_ole=False)),
            stderr="",
        )
    ])
    sent: list[str] = []
    acknowledged: list[str] = []

    assert run_agent_memory_attention(
        _config(tmp_path),
        run=run,
        deliver=sent.append,
        acknowledge=acknowledged.append,
    ) == 0
    assert sent == []
    assert acknowledged == []


def test_scheduled_memory_check_redelivers_after_fingerprint_change(
    tmp_path: Path,
) -> None:
    run = SequenceRun([
        subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(_memory_status()), stderr=""
        ),
        subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(_memory_status(fingerprint="b" * 64)),
            stderr="",
        ),
    ])
    sent: list[str] = []
    acknowledged: list[str] = []

    for _ in range(2):
        assert run_agent_memory_attention(
            _config(tmp_path),
            run=run,
            deliver=sent.append,
            acknowledge=acknowledged.append,
        ) == 0

    assert len(sent) == 2
    assert acknowledged == ["a" * 64, "b" * 64]


def test_scheduled_memory_delivery_failure_does_not_acknowledge(
    tmp_path: Path,
) -> None:
    run = SequenceRun([
        subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(_memory_status()), stderr=""
        )
    ])
    acknowledged: list[str] = []

    def fail_delivery(_message: str) -> None:
        raise OSError("delivery unavailable")

    assert run_agent_memory_attention(
        _config(tmp_path),
        run=run,
        deliver=fail_delivery,
        acknowledge=acknowledged.append,
    ) == 2
    assert acknowledged == []


def test_main_runs_memory_check_after_sync_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        cron_wrapper,
        "_config_from_args",
        lambda _args: _config(tmp_path),
    )
    monkeypatch.setattr(
        cron_wrapper,
        "run_sync_auto",
        lambda _config: order.append("sync-auto") or 75,
    )
    monkeypatch.setattr(
        cron_wrapper,
        "run_agent_memory_attention",
        lambda _config: order.append("memory") or 2,
    )

    assert cron_wrapper.main([
        "sync-auto",
        "--config",
        str(tmp_path / "operations.yaml"),
        "--python",
        str(tmp_path / "python"),
        "--install-root",
        str(tmp_path / "repo"),
    ]) == 75
    assert order == ["sync-auto", "memory"]


def test_main_runs_memory_check_after_health_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        cron_wrapper,
        "_config_from_args",
        lambda _args: _config(tmp_path),
    )
    monkeypatch.setattr(
        cron_wrapper,
        "run_health",
        lambda _config: order.append("health") or 0,
    )
    monkeypatch.setattr(
        cron_wrapper,
        "run_agent_memory_attention",
        lambda _config: order.append("memory") or 2,
    )

    assert cron_wrapper.main([
        "health",
        "--config",
        str(tmp_path / "operations.yaml"),
        "--python",
        str(tmp_path / "python"),
        "--install-root",
        str(tmp_path / "repo"),
    ]) == 2
    assert order == ["health", "memory"]


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


def _no_agent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    import cron.jobs
    import cron.scheduler

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)
    importlib.reload(cron.scheduler)
    return home


def _no_agent_policy(tmp_path: Path) -> Path:
    config = tmp_path / "operations.yaml"
    config.write_text(
        "\n".join(
            [
                "sync:",
                f"  receipt_root: {tmp_path / 'receipts'}",
                f"  status_file: {tmp_path / 'sync-status.json'}",
                f"  notification_store: {tmp_path / 'sync-notifications.json'}",
                "  required_check: All required checks pass",
                "  check_timeout_seconds: 2700",
                "  poll_interval_seconds: 15",
                "  resolver_backend: codex",
                "  reviewer_backend: claude",
                "  delivery_command: [/usr/bin/true]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config


@pytest.mark.skipif(sys.platform == "win32", reason="symlink requires privileges")
def test_wrapper_preserves_venv_python_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _no_agent_policy(tmp_path)
    install_root = tmp_path / "repo"
    install_root.mkdir()
    target = tmp_path / "python-install" / "python3.12"
    target.parent.mkdir()
    target.touch()
    launcher = tmp_path / ".venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(target)
    captured: dict[str, CronWrapperConfig] = {}

    def capture(wrapper_config: CronWrapperConfig) -> int:
        captured["config"] = wrapper_config
        return 0

    monkeypatch.setattr(cron_wrapper, "run_sync_auto", capture)
    monkeypatch.setattr(
        cron_wrapper,
        "run_agent_memory_attention",
        lambda _config: 0,
    )

    assert cron_wrapper.main(
        [
            "sync-auto",
            "--config",
            str(config),
            "--python",
            str(launcher),
            "--install-root",
            str(install_root),
        ]
    ) == 0

    assert captured["config"].python == launcher.absolute()


def _install_no_agent_wrapper(
    home: Path,
    *,
    config: Path,
    inner_command: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    wrapper = (repository / "ops" / "cloudadvisor" / "upstream-sync.sh").read_text(
        encoding="utf-8"
    )
    wrapper = wrapper.replace(
        "/Users/cloudadvisor/.hermes/hermes-agent",
        str(repository),
    ).replace(
        f"--python {repository}/.venv/bin/python",
        f"--python {inner_command}",
    ).replace(
        f"exec {repository}/.venv/bin/python",
        f"exec {sys.executable}",
    ).replace(
        "/Users/cloudadvisor/.hermes/operations/hermes-operations.yaml",
        str(config),
    )
    (home / "scripts" / "upstream-sync.sh").write_text(wrapper, encoding="utf-8")


def _run_as_no_agent() -> tuple[bool, str, str, str | None]:
    from cron.scheduler import run_job

    return run_job(
        {
            "id": "upstream-sync-test",
            "name": "Hermes fork upstream sync",
            "script": "upstream-sync.sh",
            "no_agent": True,
            "deliver": "slack:C0BFLTFC2LS",
        }
    )


def test_no_agent_routine_and_direct_success_are_scheduler_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _no_agent_home(tmp_path, monkeypatch)
    config = _no_agent_policy(tmp_path)
    inner = tmp_path / "fake-sync-auto"
    memory_status = json.dumps(
        _memory_status(
            enabled=False,
            pending=0,
            oldest_pending_hours=0.0,
            attention_required=False,
            reason="none",
            fingerprint=hashlib.sha256(b"[]").hexdigest(),
            notify_ole=False,
        ),
        separators=(",", ":"),
    )
    inner.write_text(
        "#!/bin/sh\n"
        "if [ \"$2\" = \"hermes_cli.main\" ]; then\n"
        "  printf '%s\\n' '"
        + memory_status
        + "'\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' '"
        + json.dumps(_payload(), separators=(",", ":"))
        + "'\n",
        encoding="utf-8",
    )
    inner.chmod(0o755)
    _install_no_agent_wrapper(home, config=config, inner_command=inner)

    success, _, final_response, error = _run_as_no_agent()

    from cron.scheduler import SILENT_MARKER

    assert error is None
    assert success is True
    assert final_response == SILENT_MARKER

    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        needs_ole=True,
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
    )
    packet = publish_escalation_decision_packet(
        result,
        fingerprint="7" * 64,
        trusted_root=tmp_path / "receipts",
        repo_slug="Oplink76/hermes-agent",
    )
    outbox = SyncDecisionOutbox(tmp_path / "sync-notifications.json")
    outbox.stage(
        fingerprint="7" * 64,
        packet_path=packet.path,
        packet_sha256=packet.sha256,
    )

    success, _, final_response, error = _run_as_no_agent()

    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER
    assert outbox.load().status == "acknowledged"


def test_no_agent_config_failure_is_safe_scheduler_fallback_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _no_agent_home(tmp_path, monkeypatch)
    config = tmp_path / "operations.yaml"
    config.write_text("sync: {}\nsecret: must-not-leak\n", encoding="utf-8")
    _install_no_agent_wrapper(
        home,
        config=config,
        inner_command=tmp_path / "unused-inner-command",
    )

    success, _, final_response, error = _run_as_no_agent()

    assert success is False
    assert error is not None
    assert "sync-auto wrapper failed: invalid configuration" in final_response
    assert "Cron watchdog" in final_response
    assert "Traceback" not in final_response
    assert "must-not-leak" not in final_response
