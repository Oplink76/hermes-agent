from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.deploy import (
    DeployConfig,
    DeployRequest,
    DeploymentRecord,
    DeploymentStore,
    GhReleaseVerifier,
    PreflightError,
    ReleaseEvidence,
    deploy,
)
from ops.cloudadvisor.hermes_ops.health import HealthCheck, HealthReport
from ops.cloudadvisor.hermes_ops.locking import try_exclusive_file_lock


UV_SYNC_COMMAND = (
    "env",
    "UV_PROJECT_ENVIRONMENT=.venv",
    "uv",
    "sync",
    "--locked",
    "--extra",
    "all",
    "--extra",
    "dev",
    "--extra",
    "slack",
)


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path


class FakeRunner:
    def __init__(self, responses=None, events=None):
        self.responses = responses or {}
        self.calls: list[Call] = []
        self.events = events if events is not None else []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        key = tuple(argv)
        self.calls.append(Call(key, Path(cwd)))
        self.events.append(("command", key))
        returncode, stdout, stderr = self.responses.get(
            key,
            (1, "", f"unexpected command: {key}"),
        )
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeGitHub:
    def __init__(self, evidence: ReleaseEvidence):
        self.evidence = evidence

    def verify(self, pr_number: int) -> ReleaseEvidence:
        assert pr_number == self.evidence.pr_number
        return self.evidence


class FakeSnapshots:
    def __init__(self, events):
        self.events = events
        self.restored = False

    def verify_preservation(self) -> bool:
        self.events.append(("preservation", "verified"))
        return True

    def create(self, previous_sha: str):
        self.events.append(("snapshot", previous_sha))
        return {"id": "snapshot-1", "previous_sha": previous_sha}

    def verify(self, snapshot) -> bool:
        self.events.append(("snapshot_verified", snapshot["id"]))
        return True

    def restore(self, snapshot) -> None:
        self.events.append(("snapshot_restored", snapshot["id"]))
        self.restored = True


class FakeServices:
    def __init__(self, events):
        self.events = events
        self.start_count = 0
        self.running = (
            "ai.hermes.gateway",
            "com.cloudadvisor.hermes-dashboard",
        )

    def loaded_services(self) -> tuple[str, ...]:
        self.events.append(("services", "captured"))
        return self.running

    def running_services(self) -> tuple[str, ...]:
        self.events.append(("services", "captured_running"))
        return self.running

    def inventory(self):
        return {"generation": self.start_count}

    def stop(self, services: tuple[str, ...]) -> None:
        if any(service not in self.running for service in services):
            raise RuntimeError("service already stopped")
        self.running = tuple(
            service for service in self.running if service not in services
        )
        self.events.append(("services_stopped", services))

    def start(self, services: tuple[str, ...]) -> None:
        self.start_count += 1
        self.running = tuple(dict.fromkeys((*self.running, *services)))
        self.events.append(("services_started", services))


class HalfStartedServices(FakeServices):
    def start(self, services: tuple[str, ...]) -> None:
        if self.start_count == 0:
            self.start_count = 1
            self.running = tuple(dict.fromkeys((*self.running, *services)))
            self.events.append(("services_half_started", services))
            raise RuntimeError("kickstart failed after bootstrap")
        super().start(services)


class LoadedInactiveServices(FakeServices):
    inactive_label = "ai.hermes.gateway-intentionally-inactive"

    def __init__(self, events):
        super().__init__(events)
        self.loaded = (*self.running, self.inactive_label)

    def loaded_services(self) -> tuple[str, ...]:
        return self.loaded

    def running_services(self) -> tuple[str, ...]:
        return self.running

    def stop(self, services: tuple[str, ...]) -> None:
        self.loaded = tuple(
            service for service in self.loaded if service not in services
        )
        super().stop(services)

    def start(self, services: tuple[str, ...]) -> None:
        self.loaded = tuple(dict.fromkeys((*self.loaded, *services)))
        super().start(services)


class FakeHealth:
    def __init__(self, reports, events):
        self.reports = list(reports)
        self.events = events

    def check(
        self,
        *,
        expected_sha: str,
        services: tuple[str, ...],
        identity_required: bool = True,
    ) -> HealthReport:
        self.events.append(("health", expected_sha, services, identity_required))
        return self.reports.pop(0)


class RecordingStore:
    def __init__(self, events):
        self.events = events
        self.records = []

    def write(self, record) -> None:
        self.events.append(("record", record.status))
        self.records.append(record)


def _approval(
    tmp_path: Path,
    sha: str = "new-sha",
    *,
    packet_overrides: dict[str, object] | None = None,
) -> Path:
    packet = tmp_path / "decision-packet.json"
    packet_payload = {
        "pr_number": 41,
        "candidate_sha": sha,
        "approve_available": True,
        "ci_status": "success",
        "independent_review_status": "green",
        "test_results": [{"name": "release suite", "status": "passed"}],
    }
    packet_payload.update(packet_overrides or {})
    packet.write_text(json.dumps(packet_payload), encoding="utf-8")
    packet_sha = hashlib.sha256(packet.read_bytes()).hexdigest()
    artifact = tmp_path / "approval.json"
    artifact.write_text(
        json.dumps({
            "approver": "Ole Ørum-Petersen",
            "pr_number": 41,
            "merge_sha": sha,
            "approved_at": "2026-07-10T10:00:00+00:00",
            "decision_packet": str(packet),
            "decision_packet_sha256": packet_sha,
        }),
        encoding="utf-8",
    )
    artifact.chmod(0o444)
    return artifact


def _request(tmp_path: Path, sha: str = "new-sha") -> DeployRequest:
    return DeployRequest(
        sha=sha,
        pr_number=41,
        approval_record=_approval(tmp_path, sha),
        actor="Oplink76",
    )


def _config(tmp_path: Path) -> DeployConfig:
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "pyproject.toml").write_text("[project]\nname='hermes'\n")
    (install_root / "uv.lock").write_text("lock\n")
    return DeployConfig(
        install_root=install_root,
        origin="origin",
        record_root=tmp_path / "records",
        uv_extras=("all", "dev", "slack"),
        postinstall_commands=(
            (".venv/bin/python", "scripts/docker_config_migrate.py"),
        ),
    )


def _responses():
    return {
        ("git", "status", "--porcelain", "--untracked-files=all"): (0, "", ""),
        ("git", "fetch", "origin", "main"): (0, "", ""),
        ("git", "rev-parse", "origin/main"): (0, "new-sha\n", ""),
        ("git", "rev-parse", "HEAD"): (0, "old-sha\n", ""),
        ("git", "switch", "--detach", "new-sha"): (0, "", ""),
        ("git", "switch", "--detach", "old-sha"): (0, "", ""),
        UV_SYNC_COMMAND: (0, "", ""),
        (".venv/bin/python", "scripts/docker_config_migrate.py"): (0, "", ""),
    }


def _evidence() -> ReleaseEvidence:
    return ReleaseEvidence(
        pr_number=41,
        merged=True,
        merge_sha="new-sha",
        required_check="All required checks pass",
        required_check_conclusion="success",
    )


def _green(name: str) -> HealthReport:
    return HealthReport(checks=(HealthCheck(name, True),))


def test_github_verifier_requires_the_named_successful_check(tmp_path: Path):
    command = (
        "gh",
        "pr",
        "view",
        "41",
        "--repo",
        "Oplink76/hermes-agent",
        "--json",
        "number,state,mergedAt,mergeCommit,statusCheckRollup",
    )
    runner = FakeRunner({
        command: (
            0,
            json.dumps({
                "number": 41,
                "state": "MERGED",
                "mergedAt": "2026-07-10T10:00:00Z",
                "mergeCommit": {"oid": "new-sha"},
                "statusCheckRollup": [
                    {
                        "name": "All required checks pass",
                        "conclusion": "SUCCESS",
                    }
                ],
            }),
            "",
        )
    })
    verifier = GhReleaseVerifier(
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        runner=runner,
        cwd=tmp_path,
    )

    evidence = verifier.verify(41)

    assert evidence == _evidence()


def test_deployment_store_atomically_replaces_one_private_json_record(tmp_path: Path):
    store = DeploymentStore(tmp_path / "records")
    record = DeploymentRecord(
        id="deployment-1",
        requested_sha="new-sha",
        previous_sha="old-sha",
        snapshot={"id": "snapshot-1"},
        runtime_before={},
        runtime_after=None,
        checks=(HealthCheck("preflight", True),),
        status="preparing",
        rollback=None,
    )
    store.write(record)
    store.write(
        DeploymentRecord(**{
            **record.__dict__,
            "status": "deployed",
            "runtime_after": {},
        })
    )

    path = tmp_path / "records" / "deployment-1.json"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "deployed"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".deployment-1.json.*")) == []


def test_successful_deploy_is_exact_sha_snapshot_first_and_health_gated(tmp_path: Path):
    events = []
    config = _config(tmp_path)
    runner = FakeRunner(_responses(), events)
    snapshots = FakeSnapshots(events)
    services = FakeServices(events)
    store = RecordingStore(events)

    record = deploy(
        _request(tmp_path),
        config=config,
        runner=runner,
        github=FakeGitHub(_evidence()),
        snapshots=snapshots,
        services=services,
        health=FakeHealth([_green("candidate-runtime")], events),
        store=store,
    )

    assert record.status == "deployed"
    assert record.previous_sha == "old-sha"
    assert record.requested_sha == "new-sha"
    assert record.runtime_before == {"generation": 0}
    assert record.runtime_after == {"generation": 1}
    check_names = {check.name for check in record.checks}
    assert {
        "preflight:approval",
        "preflight:github",
        "preflight:preservation",
        "preflight:clean_checkout",
        "preflight:exact_sha",
        "preflight:snapshot",
        "candidate-runtime",
    } <= check_names
    assert store.records[0].status == "preparing"
    snapshot_index = events.index(("snapshot", "old-sha"))
    checkout_index = events.index(("command", ("git", "switch", "--detach", "new-sha")))
    assert snapshot_index < checkout_index
    assert (
        "services_started",
        ("ai.hermes.gateway", "com.cloudadvisor.hermes-dashboard"),
    ) in events
    assert ("command", UV_SYNC_COMMAND) in events


def test_health_failure_rolls_back_source_state_services_and_health_checks(
    tmp_path: Path,
):
    events = []
    config = _config(tmp_path)
    runner = FakeRunner(_responses(), events)
    snapshots = FakeSnapshots(events)
    services = FakeServices(events)
    store = RecordingStore(events)
    failing = HealthReport(checks=(HealthCheck("candidate-runtime", False),))

    fingerprints = iter(("old-fingerprint", "new-fingerprint"))
    record = deploy(
        _request(tmp_path),
        config=config,
        runner=runner,
        github=FakeGitHub(_evidence()),
        snapshots=snapshots,
        services=services,
        health=FakeHealth([failing, _green("rollback-runtime")], events),
        store=store,
        fingerprint_fn=lambda root: next(fingerprints),
    )

    assert record.status == "rolled_back_healthy"
    assert snapshots.restored is True
    assert ("command", ("git", "switch", "--detach", "old-sha")) in events
    assert (
        events.count((
            "services_started",
            ("ai.hermes.gateway", "com.cloudadvisor.hermes-dashboard"),
        ))
        == 2
    )
    assert (
        "health",
        "old-sha",
        ("ai.hermes.gateway", "com.cloudadvisor.hermes-dashboard"),
        False,
    ) in events


def test_candidate_service_start_counts_as_state_mutation_for_rollback(tmp_path: Path):
    events = []
    config = _config(tmp_path)
    config = DeployConfig(
        install_root=config.install_root,
        origin=config.origin,
        record_root=config.record_root,
        uv_extras=config.uv_extras,
        postinstall_commands=(),
    )
    snapshots = FakeSnapshots(events)
    failing = HealthReport(checks=(HealthCheck("candidate-runtime", False),))

    record = deploy(
        _request(tmp_path),
        config=config,
        runner=FakeRunner(_responses(), events),
        github=FakeGitHub(_evidence()),
        snapshots=snapshots,
        services=FakeServices(events),
        health=FakeHealth([failing, _green("rollback-runtime")], events),
        store=RecordingStore(events),
    )

    assert record.status == "rolled_back_healthy"
    assert snapshots.restored is True


def test_pre_restart_failure_does_not_stop_already_unloaded_services_twice(
    tmp_path: Path,
):
    events = []
    responses = _responses()
    responses[UV_SYNC_COMMAND] = (
        1,
        "",
        "sync failed",
    )
    snapshots = FakeSnapshots(events)

    record = deploy(
        _request(tmp_path),
        config=_config(tmp_path),
        runner=FakeRunner(responses, events),
        github=FakeGitHub(_evidence()),
        snapshots=snapshots,
        services=FakeServices(events),
        health=FakeHealth([_green("rollback-runtime")], events),
        store=RecordingStore(events),
    )

    assert record.status == "rolled_back_healthy"
    assert sum(event[0] == "services_stopped" for event in events) == 1
    assert sum(event[0] == "services_started" for event in events) == 1
    assert snapshots.restored is False


def test_half_started_loaded_job_is_unloaded_before_rollback_restart(tmp_path: Path):
    events = []
    snapshots = FakeSnapshots(events)

    record = deploy(
        _request(tmp_path),
        config=_config(tmp_path),
        runner=FakeRunner(_responses(), events),
        github=FakeGitHub(_evidence()),
        snapshots=snapshots,
        services=HalfStartedServices(events),
        health=FakeHealth([_green("rollback-runtime")], events),
        store=RecordingStore(events),
    )

    assert record.status == "rolled_back_healthy"
    assert sum(event[0] == "services_stopped" for event in events) == 2
    assert sum(event[0] == "services_half_started" for event in events) == 1
    assert sum(event[0] == "services_started" for event in events) == 1
    assert snapshots.restored is True


def test_loaded_but_inactive_service_is_not_kickstarted_by_deploy(tmp_path: Path):
    events = []
    services = LoadedInactiveServices(events)

    record = deploy(
        _request(tmp_path),
        config=_config(tmp_path),
        runner=FakeRunner(_responses(), events),
        github=FakeGitHub(_evidence()),
        snapshots=FakeSnapshots(events),
        services=services,
        health=FakeHealth([_green("candidate-runtime")], events),
        store=RecordingStore(events),
    )

    assert record.status == "deployed"
    assert services.inactive_label in services.loaded
    assert all(
        services.inactive_label not in event[1]
        for event in events
        if event[0] in {"services_stopped", "services_started"}
    )


@pytest.mark.parametrize(
    ("status_output", "origin_sha", "message"),
    [
        (" M human-work.txt\n", "new-sha", "install checkout is dirty"),
        ("", "different-sha", "requested SHA does not equal fetched origin/main"),
    ],
)
def test_preflight_rejects_dirty_install_or_non_origin_sha(
    tmp_path: Path,
    status_output: str,
    origin_sha: str,
    message: str,
):
    events = []
    responses = _responses()
    responses[("git", "status", "--porcelain", "--untracked-files=all")] = (
        0,
        status_output,
        "",
    )
    responses[("git", "rev-parse", "origin/main")] = (0, f"{origin_sha}\n", "")
    snapshots = FakeSnapshots(events)

    with pytest.raises(PreflightError, match=message):
        deploy(
            _request(tmp_path),
            config=_config(tmp_path),
            runner=FakeRunner(responses, events),
            github=FakeGitHub(_evidence()),
            snapshots=snapshots,
            services=FakeServices(events),
            health=FakeHealth([_green("unused")], events),
            store=RecordingStore(events),
        )

    assert not any(event[0] == "snapshot" for event in events)
    assert not any(
        event[0] == "command" and event[1][:3] == ("git", "switch", "--detach")
        for event in events
    )


def test_preflight_rejects_tampered_decision_packet(tmp_path: Path):
    events = []
    request = _request(tmp_path)
    approval = json.loads(request.approval_record.read_text(encoding="utf-8"))
    Path(approval["decision_packet"]).write_text("tampered\n", encoding="utf-8")

    with pytest.raises(PreflightError, match="decision packet hash does not match"):
        deploy(
            request,
            config=_config(tmp_path),
            runner=FakeRunner(_responses(), events),
            github=FakeGitHub(_evidence()),
            snapshots=FakeSnapshots(events),
            services=FakeServices(events),
            health=FakeHealth([_green("unused")], events),
            store=RecordingStore(events),
        )

    assert not any(event[0] == "snapshot" for event in events)


def test_preflight_rejects_packet_without_green_independent_review(tmp_path: Path):
    request = DeployRequest(
        sha="new-sha",
        pr_number=41,
        approval_record=_approval(
            tmp_path,
            packet_overrides={
                "approve_available": False,
                "independent_review_status": "pending",
            },
        ),
        actor="Oplink76",
    )

    with pytest.raises(PreflightError, match="not approval-ready"):
        deploy(
            request,
            config=_config(tmp_path),
            runner=FakeRunner(_responses()),
            github=FakeGitHub(_evidence()),
            snapshots=FakeSnapshots([]),
            services=FakeServices([]),
            health=FakeHealth([_green("unused")], []),
            store=RecordingStore([]),
        )


def test_preflight_reports_missing_approval_artifact_as_a_gate_failure(tmp_path: Path):
    request = DeployRequest(
        sha="new-sha",
        pr_number=41,
        approval_record=tmp_path / "missing-approval.json",
        actor="Oplink76",
    )

    with pytest.raises(PreflightError, match="approval artifact is missing"):
        deploy(
            request,
            config=_config(tmp_path),
            runner=FakeRunner(_responses()),
            github=FakeGitHub(_evidence()),
            snapshots=FakeSnapshots([]),
            services=FakeServices([]),
            health=FakeHealth([_green("unused")], []),
            store=RecordingStore([]),
        )


def test_deploy_refuses_concurrent_invocation_before_preflight(tmp_path: Path):
    config = _config(tmp_path)
    lock_path = config.record_root / "deploy.lock"

    with try_exclusive_file_lock(lock_path) as acquired:
        assert acquired is True
        with pytest.raises(PreflightError, match="already in progress"):
            deploy(
                _request(tmp_path),
                config=config,
                runner=FakeRunner(_responses()),
                github=FakeGitHub(_evidence()),
                snapshots=FakeSnapshots([]),
                services=FakeServices([]),
                health=FakeHealth([_green("unused")], []),
                store=RecordingStore([]),
            )


def test_preflight_fails_closed_if_packet_disappears_during_verification(
    tmp_path: Path,
):
    request = _request(tmp_path)
    artifact = json.loads(request.approval_record.read_text(encoding="utf-8"))
    packet = Path(artifact["decision_packet"])

    class DeletingGitHub:
        def verify(self, pr_number: int) -> ReleaseEvidence:
            packet.unlink()
            return _evidence()

    with pytest.raises(PreflightError, match="decision packet is missing"):
        deploy(
            request,
            config=_config(tmp_path),
            runner=FakeRunner(_responses()),
            github=DeletingGitHub(),
            snapshots=FakeSnapshots([]),
            services=FakeServices([]),
            health=FakeHealth([_green("unused")], []),
            store=RecordingStore([]),
        )
