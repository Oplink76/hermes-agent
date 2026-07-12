from __future__ import annotations

import json
import hashlib
import os
import shutil
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
from ops.cloudadvisor.hermes_ops.sync import (
    CheckResult,
    SyncClassification,
    SyncResult,
    SyncState,
)
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_receipt import (
    finalize_sync_receipt,
    write_sync_receipt,
)


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

PYPROJECT_WITH_EXTRAS = """\
[project]
name = "hermes"

[project.optional-dependencies]
all = []
dev = []
slack = []
"""

BASE_SHA = "a" * 40
UPSTREAM_SHA = "b" * 40
CANDIDATE_SHA = "c" * 40
MERGE_SHA = "d" * 40
LOCAL_CHECKS = tuple(
    CheckResult(name, "passed")
    for name in (
        "diff_check",
        "unmerged_index",
        "conflict_markers",
        "compileall",
        "tests",
    )
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
        apply_injection: bool = True,
    ) -> HealthReport:
        self.events.append(
            ("health", expected_sha, services, identity_required, apply_injection)
        )
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
    approver: str = "Ole Ørum-Petersen",
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
            "approver": approver,
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
        repo_slug="Oplink76/hermes-agent",
        sync_receipt_root=tmp_path / "sync-receipts",
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
        ("git", "show", "old-sha:pyproject.toml"): (
            0,
            PYPROJECT_WITH_EXTRAS,
            "",
        ),
        ("git", "show", "new-sha:pyproject.toml"): (
            0,
            PYPROJECT_WITH_EXTRAS,
            "",
        ),
        ("git", "switch", "--detach", "new-sha"): (0, "", ""),
        ("git", "switch", "--detach", "old-sha"): (0, "", ""),
        UV_SYNC_COMMAND: (0, "", ""),
        (".venv/bin/python", "scripts/docker_config_migrate.py"): (0, "", ""),
    }


def _evidence(**updates: object) -> ReleaseEvidence:
    values: dict[str, object] = {
        "pr_number": 41,
        "merged": True,
        "merge_sha": "new-sha",
        "repo_slug": "Oplink76/hermes-agent",
        "head_sha": "new-sha",
        "base_ref_name": "main",
        "base_sha": "base-sha",
        "required_check": "All required checks pass",
        "required_check_conclusion": "success",
    }
    values.update(updates)
    return ReleaseEvidence(**values)


def _green(name: str) -> HealthReport:
    return HealthReport(checks=(HealthCheck(name, True),))


def _sync_receipt(tmp_path: Path) -> Path:
    candidate = SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=BASE_SHA,
        upstream_sha=UPSTREAM_SHA,
        candidate_sha=CANDIDATE_SHA,
        pr_number=7,
        checks=LOCAL_CHECKS,
        classification=SyncClassification.CLEAN,
    )
    evidence = SyncPullRequestEvidence(
        number=7,
        state="open",
        base_sha=BASE_SHA,
        head_sha=CANDIDATE_SHA,
        required_check="All required checks pass",
        required_check_conclusion="success",
        workflow_run_id=101,
        required_check_run_id=202,
    )
    premerge = write_sync_receipt(
        tmp_path / "sync-receipts",
        candidate,
        evidence,
        repo_slug="Oplink76/hermes-agent",
        created_at="2026-07-12T16:00:00Z",
    )
    return finalize_sync_receipt(premerge.path, merge_sha=MERGE_SHA).path


def _mutate_sync_receipt(path: Path, **updates: object) -> Path:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    content = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    mutated = path.parent / f"sync-merged-{digest}.json"
    mutated.write_bytes(content)
    mutated.chmod(0o400)
    return mutated


def _sync_responses() -> dict[tuple[str, ...], tuple[int, str, str]]:
    responses = _responses()
    responses[("git", "rev-parse", "origin/main")] = (0, f"{MERGE_SHA}\n", "")
    responses[("git", "show", f"{MERGE_SHA}:pyproject.toml")] = (
        0,
        PYPROJECT_WITH_EXTRAS,
        "",
    )
    responses[("git", "switch", "--detach", MERGE_SHA)] = (0, "", "")
    responses[("git", "merge-base", "--is-ancestor", CANDIDATE_SHA, MERGE_SHA)] = (
        0,
        "",
        "",
    )
    return responses


def _sync_evidence(**updates: object) -> ReleaseEvidence:
    values: dict[str, object] = {
        "pr_number": 7,
        "merged": True,
        "merge_sha": MERGE_SHA,
        "repo_slug": "Oplink76/hermes-agent",
        "head_sha": CANDIDATE_SHA,
        "base_ref_name": "main",
        "base_sha": BASE_SHA,
        "required_check": "All required checks pass",
        "required_check_conclusion": "success",
    }
    values.update(updates)
    return ReleaseEvidence(**values)


def _sync_request(receipt: Path, *, sha: str = MERGE_SHA, pr_number: int = 7):
    return DeployRequest(
        sha=sha,
        pr_number=pr_number,
        actor="hermes-upstream-sync",
        authority_kind="automated_sync",
        authority_record=receipt,
    )


def _deploy_dependencies(
    tmp_path: Path,
    *,
    sync: bool = False,
    events: list[tuple] | None = None,
):
    observed = events if events is not None else []
    return {
        "config": _config(tmp_path),
        "runner": FakeRunner(_sync_responses() if sync else _responses(), observed),
        "github": FakeGitHub(_sync_evidence() if sync else _evidence()),
        "snapshots": FakeSnapshots(observed),
        "services": FakeServices(observed),
        "health": FakeHealth([_green("candidate-runtime")], observed),
        "store": RecordingStore(observed),
    }


def test_human_deploy_still_requires_named_approver(tmp_path: Path):
    request = DeployRequest(
        sha="new-sha",
        pr_number=41,
        approval_record=_approval(tmp_path, approver="Someone Else"),
        actor="Oplink76",
    )

    with pytest.raises(PreflightError, match="required approver"):
        deploy(request, **_deploy_dependencies(tmp_path))


@pytest.mark.parametrize(
    ("evidence_update", "message"),
    [
        ({"repo_slug": "Other/hermes-agent"}, "repository"),
        ({"base_ref_name": "release"}, "base branch"),
    ],
)
def test_human_deploy_rejects_crossed_release_repository_or_base(
    tmp_path: Path,
    evidence_update: dict[str, object],
    message: str,
):
    dependencies = _deploy_dependencies(tmp_path)
    dependencies["github"] = FakeGitHub(_evidence(**evidence_update))

    with pytest.raises(PreflightError, match=message):
        deploy(_request(tmp_path), **dependencies)


def test_sync_deploy_accepts_only_exact_merged_receipt(tmp_path: Path):
    events: list[tuple] = []
    receipt = _sync_receipt(tmp_path)

    record = deploy(
        _sync_request(receipt),
        **_deploy_dependencies(tmp_path, sync=True, events=events),
    )

    assert record.status == "deployed"
    authority = next(check for check in record.checks if check.name == "preflight:authority")
    assert "automated_sync" in authority.detail
    assert receipt.name in authority.detail


def test_sync_deploy_rejects_candidate_sha_instead_of_merge_sha(tmp_path: Path):
    receipt = _sync_receipt(tmp_path)

    with pytest.raises(PreflightError, match="exact merged SHA"):
        deploy(
            _sync_request(receipt, sha=CANDIDATE_SHA),
            **_deploy_dependencies(tmp_path, sync=True),
        )


@pytest.mark.parametrize(
    ("receipt_mutation", "message"),
    [
        ({"required_check_conclusion": "failure"}, "not eligible"),
        ({"pr_number": 8}, "different PR"),
        ({"required_check": "Different check"}, "required GitHub check"),
    ],
)
def test_sync_deploy_rejects_receipt_identity_or_green_state_mismatch(
    tmp_path: Path,
    receipt_mutation: dict[str, object],
    message: str,
):
    receipt = _mutate_sync_receipt(_sync_receipt(tmp_path), **receipt_mutation)

    with pytest.raises(PreflightError, match=message):
        deploy(
            _sync_request(receipt), **_deploy_dependencies(tmp_path, sync=True)
        )


@pytest.mark.parametrize(
    ("evidence_update", "message"),
    [
        ({"repo_slug": "Other/hermes-agent"}, "repository"),
        ({"head_sha": "e" * 40}, "PR head"),
        ({"base_ref_name": "release"}, "base branch"),
        ({"base_sha": "f" * 40}, "base SHA"),
    ],
)
def test_sync_deploy_rejects_crossed_or_refreshed_github_identity(
    tmp_path: Path,
    evidence_update: dict[str, object],
    message: str,
):
    receipt = _sync_receipt(tmp_path)
    dependencies = _deploy_dependencies(tmp_path, sync=True)
    dependencies["github"] = FakeGitHub(_sync_evidence(**evidence_update))

    with pytest.raises(PreflightError, match=message):
        deploy(_sync_request(receipt), **dependencies)


def test_sync_deploy_rejects_receipt_outside_configured_issuer_root(tmp_path: Path):
    receipt = _sync_receipt(tmp_path)
    outside = tmp_path / "outside" / receipt.name
    outside.parent.mkdir()
    shutil.copyfile(receipt, outside)
    outside.chmod(0o400)

    with pytest.raises(PreflightError, match="trusted receipt root"):
        deploy(
            _sync_request(outside), **_deploy_dependencies(tmp_path, sync=True)
        )


def test_sync_deploy_requires_candidate_to_be_contained_in_merge(tmp_path: Path):
    receipt = _sync_receipt(tmp_path)
    events: list[tuple] = []
    dependencies = _deploy_dependencies(tmp_path, sync=True, events=events)
    runner = dependencies["runner"]
    assert isinstance(runner, FakeRunner)
    runner.responses[
        ("git", "merge-base", "--is-ancestor", CANDIDATE_SHA, MERGE_SHA)
    ] = (1, "", "not an ancestor")

    with pytest.raises(PreflightError, match="not contained in merge SHA"):
        deploy(_sync_request(receipt), **dependencies)
    assert not any(event[0] == "snapshot" for event in events)


@pytest.mark.parametrize("artifact_state", ["writable", "tampered"])
def test_sync_deploy_rejects_mutable_or_tampered_receipt(
    tmp_path: Path,
    artifact_state: str,
):
    receipt = _sync_receipt(tmp_path)
    if artifact_state == "writable":
        receipt.chmod(0o600)
    else:
        receipt.chmod(0o600)
        receipt.write_text("{}\n", encoding="utf-8")
        receipt.chmod(0o400)

    with pytest.raises(PreflightError, match="sync authority record"):
        deploy(
            _sync_request(receipt), **_deploy_dependencies(tmp_path, sync=True)
        )


@pytest.mark.parametrize("authority_kind", ["human_with_sync", "sync_with_human"])
def test_deploy_rejects_crossed_human_and_sync_artifacts(
    tmp_path: Path,
    authority_kind: str,
):
    sync_receipt = _sync_receipt(tmp_path)
    if authority_kind == "human_with_sync":
        request = DeployRequest(
            sha=MERGE_SHA,
            pr_number=7,
            actor="Oplink76",
            approval_record=sync_receipt,
        )
    else:
        request = DeployRequest(
            sha="new-sha",
            pr_number=41,
            actor="hermes-upstream-sync",
            authority_kind="automated_sync",
            authority_record=_approval(tmp_path),
        )

    with pytest.raises(PreflightError, match="authority record"):
        deploy(request, **_deploy_dependencies(tmp_path, sync=True))


def test_github_verifier_requires_the_named_successful_check(tmp_path: Path):
    command = (
        "gh",
        "pr",
        "view",
        "41",
        "--repo",
        "Oplink76/hermes-agent",
        "--json",
        "number,state,mergedAt,mergeCommit,headRefOid,baseRefName,baseRefOid,statusCheckRollup",
    )
    runner = FakeRunner({
        command: (
            0,
            json.dumps({
                "number": 41,
                "state": "MERGED",
                "mergedAt": "2026-07-10T10:00:00Z",
                "mergeCommit": {"oid": "d" * 40},
                "headRefOid": "a" * 40,
                "baseRefName": "main",
                "baseRefOid": "b" * 40,
                "statusCheckRollup": [
                    {
                        "name": "All required checks pass",
                        "conclusion": "SUCCESS",
                        "detailsUrl": (
                            "https://github.com/Oplink76/hermes-agent/"
                            "actions/runs/101/job/202"
                        ),
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
        gh_executable="gh",
    )

    evidence = verifier.verify(41)

    assert evidence == _evidence(
        merge_sha="d" * 40,
        head_sha="a" * 40,
        base_sha="b" * 40,
    )


def test_github_verifier_rejects_duplicate_required_check_context(tmp_path: Path):
    command = (
        "gh",
        "pr",
        "view",
        "41",
        "--repo",
        "Oplink76/hermes-agent",
        "--json",
        "number,state,mergedAt,mergeCommit,headRefOid,baseRefName,baseRefOid,statusCheckRollup",
    )
    payload = {
        "number": 41,
        "state": "MERGED",
        "mergedAt": "2026-07-10T10:00:00Z",
        "mergeCommit": {"oid": "d" * 40},
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "statusCheckRollup": [
            {"name": "All required checks pass", "conclusion": "SUCCESS"},
            {"context": "All required checks pass", "state": "SUCCESS"},
        ],
    }
    verifier = GhReleaseVerifier(
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        runner=FakeRunner({command: (0, json.dumps(payload), "")}),
        cwd=tmp_path,
        gh_executable="gh",
    )

    with pytest.raises(PreflightError, match="required check evidence is ambiguous"):
        verifier.verify(41)


def test_github_verifier_uses_explicit_windows_cli_and_full_authority_ids(
    tmp_path: Path,
):
    executable = tmp_path / "bin" / "gh.cmd"
    command = (
        str(executable),
        "pr",
        "view",
        "41",
        "--repo",
        "Oplink76/hermes-agent",
        "--json",
        "number,state,mergedAt,mergeCommit,headRefOid,baseRefName,baseRefOid,statusCheckRollup",
    )
    payload = {
        "number": 41,
        "state": "MERGED",
        "mergedAt": "2026-07-10T10:00:00Z",
        "mergeCommit": {"oid": "d" * 40},
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "statusCheckRollup": [
            {
                "name": "All required checks pass",
                "conclusion": "SUCCESS",
                "detailsUrl": (
                    "https://github.com/Oplink76/hermes-agent/"
                    "actions/runs/101/job/202"
                ),
            }
        ],
    }
    verifier = GhReleaseVerifier(
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        runner=FakeRunner({command: (0, json.dumps(payload), "")}),
        cwd=tmp_path,
        gh_executable=executable,
    )

    evidence = verifier.verify(41)

    assert evidence.merge_sha == "d" * 40
    assert evidence.head_sha == "a" * 40
    assert evidence.base_sha == "b" * 40


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
        False,
    ) in events


def test_modern_rollback_keeps_runtime_identity_mandatory(tmp_path: Path):
    events: list[tuple] = []
    config = _config(tmp_path)
    identity_module = config.install_root / "gateway" / "runtime_identity.py"
    identity_module.parent.mkdir()
    identity_module.write_text("# modern runtime identity\n", encoding="utf-8")
    failing = HealthReport(checks=(HealthCheck("candidate-runtime", False),))

    record = deploy(
        _request(tmp_path),
        config=config,
        runner=FakeRunner(_responses(), events),
        github=FakeGitHub(_evidence()),
        snapshots=FakeSnapshots(events),
        services=FakeServices(events),
        health=FakeHealth([failing, _green("rollback-runtime")], events),
        store=RecordingStore(events),
    )

    assert record.status == "rolled_back_healthy"
    assert (
        "health",
        "old-sha",
        ("ai.hermes.gateway", "com.cloudadvisor.hermes-dashboard"),
        True,
        False,
    ) in events


def test_candidate_service_start_counts_as_state_mutation_for_rollback(tmp_path: Path):
    events = []
    config = _config(tmp_path)
    config = DeployConfig(
        install_root=config.install_root,
        origin=config.origin,
        record_root=config.record_root,
        repo_slug=config.repo_slug,
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
    assert any(
        event[0] == "health" and event[-1] is False
        for event in events
    )


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


def test_preflight_rejects_empty_or_revision_incompatible_uv_extras(
    tmp_path: Path,
):
    empty_root = tmp_path / "empty"
    mismatch_root = tmp_path / "mismatch"
    candidate_root = tmp_path / "candidate-mismatch"
    empty_root.mkdir()
    mismatch_root.mkdir()
    candidate_root.mkdir()
    empty_base = _config(empty_root)
    empty_config = DeployConfig(
        install_root=empty_base.install_root,
        origin=empty_base.origin,
        record_root=empty_base.record_root,
        repo_slug=empty_base.repo_slug,
    )

    for index, (config, responses, message) in enumerate((
        (
            empty_config,
            _responses(),
            "deploy.uv_extras must contain at least one extra",
        ),
        (
            _config(mismatch_root),
            {
                **_responses(),
                ("git", "show", "old-sha:pyproject.toml"): (
                    0,
                    PYPROJECT_WITH_EXTRAS.replace("slack = []\n", ""),
                    "",
                ),
            },
            "old-sha.*slack",
        ),
        (
            _config(candidate_root),
            {
                **_responses(),
                ("git", "show", "new-sha:pyproject.toml"): (
                    0,
                    PYPROJECT_WITH_EXTRAS.replace("slack = []\n", ""),
                    "",
                ),
            },
            "new-sha.*slack",
        ),
    )):
        events: list[tuple] = []
        request_root = tmp_path / f"request-{index}"
        request_root.mkdir()
        with pytest.raises(PreflightError, match=message):
            deploy(
                _request(request_root),
                config=config,
                runner=FakeRunner(responses, events),
                github=FakeGitHub(_evidence()),
                snapshots=FakeSnapshots(events),
                services=FakeServices(events),
                health=FakeHealth([_green("unused")], events),
                store=RecordingStore(events),
            )
        assert not any(event[0] == "snapshot" for event in events)
        assert not any(event[0] == "services_stopped" for event in events)


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
