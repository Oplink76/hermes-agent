from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.deploy import (
    ApprovalRecord,
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

    def running_services(self) -> tuple[str, ...]:
        self.events.append(("services", "captured"))
        return ("ai.hermes.gateway", "com.cloudadvisor.hermes-dashboard")

    def inventory(self):
        return {"generation": self.start_count}

    def stop(self, services: tuple[str, ...]) -> None:
        self.events.append(("services_stopped", services))

    def start(self, services: tuple[str, ...]) -> None:
        self.start_count += 1
        self.events.append(("services_started", services))


class FakeHealth:
    def __init__(self, reports, events):
        self.reports = list(reports)
        self.events = events

    def check(self, *, expected_sha: str, services: tuple[str, ...]) -> HealthReport:
        self.events.append(("health", expected_sha, services))
        return self.reports.pop(0)


class RecordingStore:
    def __init__(self, events):
        self.events = events
        self.records = []

    def write(self, record) -> None:
        self.events.append(("record", record.status))
        self.records.append(record)


def _approval(sha: str = "new-sha") -> ApprovalRecord:
    return ApprovalRecord(
        approver="Ole Ørum-Petersen",
        pr_number=41,
        merge_sha=sha,
        approved_at="2026-07-10T10:00:00+00:00",
        decision_packet_sha256="a" * 64,
    )


def _request(sha: str = "new-sha") -> DeployRequest:
    return DeployRequest(
        sha=sha,
        pr_number=41,
        approval_record=_approval(sha),
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
        ("env", "UV_PROJECT_ENVIRONMENT=.venv", "uv", "sync", "--locked"): (
            0,
            "",
            "",
        ),
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
        _request(),
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
        _request(),
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
    ) in events


def test_candidate_service_start_counts_as_state_mutation_for_rollback(tmp_path: Path):
    events = []
    config = _config(tmp_path)
    config = DeployConfig(
        install_root=config.install_root,
        origin=config.origin,
        record_root=config.record_root,
        postinstall_commands=(),
    )
    snapshots = FakeSnapshots(events)
    failing = HealthReport(checks=(HealthCheck("candidate-runtime", False),))

    record = deploy(
        _request(),
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
            _request(),
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
