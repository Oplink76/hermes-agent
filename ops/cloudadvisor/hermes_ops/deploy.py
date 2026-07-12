"""Approval-gated exact-SHA deployment with automatic rollback."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tomllib
import uuid
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol

from .command import CommandRunner
from .health import HealthCheck, HealthReport
from .locking import try_exclusive_file_lock
from .sync_receipt import SyncEligibilityReceipt, SyncReceiptError
from utils import atomic_json_write


logger = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    pass


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalRecord:
    approver: str
    pr_number: int
    merge_sha: str
    approved_at: str
    decision_packet_sha256: str
    artifact_path: Path
    artifact_sha256: str
    decision_packet_path: Path

    @classmethod
    def load(cls, path: Path) -> "ApprovalRecord":
        try:
            artifact_path = Path(path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise PreflightError("approval artifact is missing or unreadable") from exc
        try:
            if os.name != "nt" and stat.S_IMODE(artifact_path.stat().st_mode) & 0o222:
                raise PreflightError("approval artifact must be read-only")
            raw = artifact_path.read_bytes()
        except OSError as exc:
            raise PreflightError("approval artifact is missing or unreadable") from exc
        try:
            payload = json.loads(raw)
            packet_path = Path(payload["decision_packet"]).expanduser()
            if not packet_path.is_absolute():
                packet_path = artifact_path.parent / packet_path
            return cls(
                approver=str(payload["approver"]),
                pr_number=int(payload["pr_number"]),
                merge_sha=str(payload["merge_sha"]),
                approved_at=str(payload["approved_at"]),
                decision_packet_sha256=str(payload["decision_packet_sha256"]),
                artifact_path=artifact_path,
                artifact_sha256=hashlib.sha256(raw).hexdigest(),
                decision_packet_path=packet_path.resolve(strict=True),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
            raise PreflightError("approval artifact is incomplete or invalid") from exc


@dataclass(frozen=True)
class DeployRequest:
    sha: str
    pr_number: int
    actor: str
    authority_kind: Literal["human", "automated_sync"] = "human"
    authority_record: Path | None = None
    approval_record: Path | None = None


@dataclass(frozen=True)
class DeployConfig:
    install_root: Path
    origin: str
    record_root: Path
    lock_path: Path | None = None
    required_approver: str = "Ole Ørum-Petersen"
    required_check: str = "All required checks pass"
    uv_extras: tuple[str, ...] = ()
    postinstall_commands: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ReleaseEvidence:
    pr_number: int
    merged: bool
    merge_sha: str
    required_check: str
    required_check_conclusion: str


@dataclass(frozen=True)
class DeploymentRecord:
    id: str
    requested_sha: str
    previous_sha: str
    snapshot: object
    runtime_before: object
    runtime_after: object
    checks: tuple[HealthCheck, ...]
    status: str
    rollback: object

    def to_dict(self) -> dict[str, object]:
        return _jsonable(asdict(self))


class ReleaseVerifier(Protocol):
    def verify(self, pr_number: int) -> ReleaseEvidence: ...


class GhReleaseVerifier:
    """Read merge and required-check evidence from GitHub CLI JSON."""

    def __init__(
        self,
        *,
        repo_slug: str,
        required_check: str,
        runner: CommandRunner,
        cwd: Path,
    ):
        self.repo_slug = repo_slug
        self.required_check = required_check
        self.runner = runner
        self.cwd = Path(cwd)

    def verify(self, pr_number: int) -> ReleaseEvidence:
        completed = self.runner.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                self.repo_slug,
                "--json",
                "number,state,mergedAt,mergeCommit,statusCheckRollup",
            ],
            cwd=self.cwd,
            timeout=300,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise PreflightError(f"could not verify GitHub release evidence: {detail}")
        try:
            payload = json.loads(completed.stdout or "{}")
            merge_commit = payload.get("mergeCommit") or {}
            checks = payload.get("statusCheckRollup") or []
            required = next(
                (
                    check
                    for check in checks
                    if (check.get("name") or check.get("context"))
                    == self.required_check
                ),
                None,
            )
            conclusion = (
                (required.get("conclusion") or required.get("state") or "missing")
                if isinstance(required, dict)
                else "missing"
            )
            return ReleaseEvidence(
                pr_number=int(payload["number"]),
                merged=bool(
                    payload.get("mergedAt") or payload.get("state") == "MERGED"
                ),
                merge_sha=str(merge_commit["oid"]),
                required_check=self.required_check,
                required_check_conclusion=str(conclusion).lower(),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PreflightError("GitHub release evidence was incomplete") from exc


class SnapshotProvider(Protocol):
    def verify_preservation(self) -> bool: ...
    def create(self, previous_sha: str) -> object: ...
    def verify(self, snapshot: object) -> bool: ...
    def restore(self, snapshot: object) -> None: ...


class ServiceController(Protocol):
    def loaded_services(self) -> tuple[str, ...]: ...
    def running_services(self) -> tuple[str, ...]: ...
    def inventory(self) -> object: ...
    def stop(self, services: tuple[str, ...]) -> None: ...
    def start(self, services: tuple[str, ...]) -> None: ...


class HealthChecker(Protocol):
    def check(
        self,
        *,
        expected_sha: str,
        services: tuple[str, ...],
        identity_required: bool = True,
        apply_injection: bool = True,
    ) -> HealthReport: ...


class RecordStore(Protocol):
    def write(self, record: DeploymentRecord) -> None: ...


class DeploymentStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve(strict=False)

    def write(self, record: DeploymentRecord) -> None:
        path = self.root / f"{record.id}.json"
        atomic_json_write(path, record.to_dict(), mode=0o600, sort_keys=True)


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def dependency_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock", "package-lock.json"):
        path = root / name
        digest.update(name.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _uv_sync_command(config: DeployConfig) -> list[str]:
    command = ["env", "UV_PROJECT_ENVIRONMENT=.venv", "uv", "sync", "--locked"]
    for extra in config.uv_extras:
        command.extend(("--extra", extra))
    return command


def _validate_uv_extras_for_revisions(
    config: DeployConfig,
    *,
    runner: CommandRunner,
    root: Path,
    revisions: tuple[str, ...],
) -> None:
    if not config.uv_extras:
        raise PreflightError("deploy.uv_extras must contain at least one extra")
    for revision in dict.fromkeys(revisions):
        raw = _run_required(
            runner,
            ["git", "show", f"{revision}:pyproject.toml"],
            cwd=root,
        )
        try:
            payload = tomllib.loads(raw)
            available = payload["project"]["optional-dependencies"]
        except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise PreflightError(
                f"could not read optional dependencies at {revision}"
            ) from exc
        if not isinstance(available, dict):
            raise PreflightError(
                f"could not read optional dependencies at {revision}"
            )
        missing = sorted(set(config.uv_extras) - set(available))
        if missing:
            raise PreflightError(
                f"configured uv extras are unavailable at {revision}: "
                + ", ".join(missing)
            )


def _run_required(
    runner: CommandRunner,
    argv: list[str],
    *,
    cwd: Path,
) -> str:
    completed = runner.run(argv, cwd=cwd, timeout=600)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise DeploymentError(f"command failed ({' '.join(argv)}): {detail}")
    return (completed.stdout or "").strip()


def _validate_human_authority(
    request: DeployRequest,
    approval: ApprovalRecord,
    config: DeployConfig,
    evidence: ReleaseEvidence,
) -> None:
    if not request.actor.strip():
        raise PreflightError("deployment actor is missing")
    if approval.approver != config.required_approver:
        raise PreflightError("approval record does not name the required approver")
    if (
        approval.pr_number != request.pr_number
        or evidence.pr_number != request.pr_number
    ):
        raise PreflightError("approval or GitHub evidence names a different PR")
    if approval.merge_sha != request.sha or evidence.merge_sha != request.sha:
        raise PreflightError("requested SHA does not equal the PR merge SHA")
    try:
        approved_at = datetime.fromisoformat(
            approval.approved_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PreflightError("approval record timestamp is invalid") from exc
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise PreflightError("approval record timestamp must include a timezone")
    if not re.fullmatch(r"[0-9a-f]{64}", approval.decision_packet_sha256):
        raise PreflightError("approval record decision packet hash is invalid")
    try:
        artifact_sha = hashlib.sha256(approval.artifact_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PreflightError(
            "approval artifact is missing during verification"
        ) from exc
    if artifact_sha != approval.artifact_sha256:
        raise PreflightError("approval artifact changed while being verified")
    try:
        packet_bytes = approval.decision_packet_path.read_bytes()
    except OSError as exc:
        raise PreflightError("decision packet is missing during verification") from exc
    actual_packet_sha = hashlib.sha256(packet_bytes).hexdigest()
    if actual_packet_sha != approval.decision_packet_sha256:
        raise PreflightError("decision packet hash does not match the approval record")
    try:
        packet = json.loads(packet_bytes)
        packet_tests = packet["test_results"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PreflightError("decision packet is incomplete or invalid") from exc
    packet_ready = bool(
        isinstance(packet, dict)
        and packet.get("pr_number") == request.pr_number
        and packet.get("candidate_sha") == request.sha
        and packet.get("approve_available") is True
        and packet.get("ci_status") == "success"
        and packet.get("independent_review_status") == "green"
        and isinstance(packet_tests, list)
        and packet_tests
        and all(
            isinstance(result, dict) and result.get("status") == "passed"
            for result in packet_tests
        )
    )
    if not packet_ready:
        raise PreflightError(
            "decision packet is not approval-ready for this PR and SHA"
        )
    if not evidence.merged:
        raise PreflightError("pull request is not merged")
    if evidence.required_check != config.required_check:
        raise PreflightError("required GitHub check is missing")
    if evidence.required_check_conclusion.lower() != "success":
        raise PreflightError("required GitHub check did not conclude success")


def _validate_sync_authority(
    request: DeployRequest,
    receipt: SyncEligibilityReceipt,
    config: DeployConfig,
    evidence: ReleaseEvidence,
) -> None:
    if not request.actor.strip():
        raise PreflightError("deployment actor is missing")
    if receipt.pr_number != request.pr_number or evidence.pr_number != request.pr_number:
        raise PreflightError("sync authority or GitHub evidence names a different PR")
    if receipt.merge_sha is None:
        raise PreflightError("sync authority must be a finalized merged receipt")
    if receipt.merge_sha != request.sha:
        raise PreflightError("requested SHA is not the exact merged SHA in sync authority")
    if evidence.merge_sha != request.sha:
        raise PreflightError("requested SHA does not equal the PR merge SHA")
    if not evidence.merged:
        raise PreflightError("pull request is not merged")
    if (
        receipt.required_check != config.required_check
        or evidence.required_check != config.required_check
    ):
        raise PreflightError("required GitHub check identity does not match")
    if (
        receipt.required_check_conclusion != "success"
        or evidence.required_check_conclusion.lower() != "success"
    ):
        raise PreflightError("required GitHub check did not conclude success")


def _load_authority(
    request: DeployRequest,
) -> ApprovalRecord | SyncEligibilityReceipt:
    if request.authority_kind == "human":
        if request.authority_record is not None:
            raise PreflightError("human deployment received a sync authority record")
        if request.approval_record is None:
            raise PreflightError("human authority record is missing")
        try:
            approval = ApprovalRecord.load(request.approval_record)
        except PreflightError as exc:
            raise PreflightError(f"human authority record is invalid: {exc}") from exc
        return approval
    if request.authority_kind == "automated_sync":
        if request.approval_record is not None:
            raise PreflightError("automated sync received a human authority record")
        if request.authority_record is None:
            raise PreflightError("sync authority record is missing")
        try:
            return SyncEligibilityReceipt.load(request.authority_record)
        except SyncReceiptError as exc:
            raise PreflightError(
                f"sync authority record is not eligible: {exc}"
            ) from exc
    raise PreflightError(f"unsupported deployment authority kind: {request.authority_kind}")


def _validate_authority(
    request: DeployRequest,
    authority: ApprovalRecord | SyncEligibilityReceipt,
    config: DeployConfig,
    evidence: ReleaseEvidence,
) -> HealthCheck:
    if request.authority_kind == "human" and isinstance(authority, ApprovalRecord):
        _validate_human_authority(request, authority, config, evidence)
        return HealthCheck(
            "preflight:approval",
            True,
            f"approver={authority.approver} "
            f"pr={request.pr_number} actor={request.actor} "
            f"approval_artifact={authority.artifact_sha256} "
            f"packet={authority.decision_packet_sha256}",
        )
    if request.authority_kind == "automated_sync" and isinstance(
        authority, SyncEligibilityReceipt
    ):
        _validate_sync_authority(request, authority, config, evidence)
        return HealthCheck(
            "preflight:authority",
            True,
            f"kind=automated_sync pr={request.pr_number} actor={request.actor} "
            f"receipt={Path(request.authority_record).name} "
            f"candidate={authority.candidate_sha} merge={authority.merge_sha}",
        )
    raise PreflightError("deployment authority record type does not match authority kind")


def _record_id(sha: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{sha[:12]}-{uuid.uuid4().hex[:8]}"


class _HealthFailure(DeploymentError):
    def __init__(self, report: HealthReport):
        super().__init__("mandatory post-deploy health check failed")
        self.report = report


def deploy(
    request: DeployRequest,
    *,
    config: DeployConfig,
    runner: CommandRunner,
    github: ReleaseVerifier,
    snapshots: SnapshotProvider,
    services: ServiceController,
    health: HealthChecker,
    store: RecordStore | None = None,
    fingerprint_fn: Callable[[Path], str] = dependency_fingerprint,
) -> DeploymentRecord:
    lock_path = config.lock_path or (Path(config.record_root) / "deploy.lock")
    with try_exclusive_file_lock(lock_path) as acquired:
        if not acquired:
            raise PreflightError("another deployment is already in progress")
        return _deploy_locked(
            request,
            config=config,
            runner=runner,
            github=github,
            snapshots=snapshots,
            services=services,
            health=health,
            store=store,
            fingerprint_fn=fingerprint_fn,
        )


def _deploy_locked(
    request: DeployRequest,
    *,
    config: DeployConfig,
    runner: CommandRunner,
    github: ReleaseVerifier,
    snapshots: SnapshotProvider,
    services: ServiceController,
    health: HealthChecker,
    store: RecordStore | None = None,
    fingerprint_fn: Callable[[Path], str] = dependency_fingerprint,
) -> DeploymentRecord:
    root = Path(config.install_root).expanduser().resolve(strict=True)
    record_store = store or DeploymentStore(config.record_root)

    authority = _load_authority(request)
    evidence = github.verify(request.pr_number)
    authority_check = _validate_authority(request, authority, config, evidence)
    preflight_checks = [
        authority_check,
        HealthCheck(
            "preflight:github",
            True,
            f"merge={evidence.merge_sha} check={evidence.required_check}",
        ),
    ]
    if not snapshots.verify_preservation():
        raise PreflightError("Package 1 preservation verification failed")
    preflight_checks.append(
        HealthCheck("preflight:preservation", True, "Package 1 verified")
    )
    status = _run_required(
        runner,
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
    )
    if status:
        raise PreflightError("install checkout is dirty")
    preflight_checks.append(
        HealthCheck("preflight:clean_checkout", True, "install checkout clean")
    )
    _run_required(runner, ["git", "fetch", config.origin, "main"], cwd=root)
    origin_sha = _run_required(
        runner,
        ["git", "rev-parse", f"{config.origin}/main"],
        cwd=root,
    )
    if request.sha != origin_sha:
        raise PreflightError("requested SHA does not equal fetched origin/main")
    preflight_checks.append(
        HealthCheck("preflight:exact_sha", True, f"origin/main={origin_sha}")
    )

    previous_sha = _run_required(runner, ["git", "rev-parse", "HEAD"], cwd=root)
    _validate_uv_extras_for_revisions(
        config,
        runner=runner,
        root=root,
        revisions=(previous_sha, request.sha),
    )
    previous_identity_required = (root / "gateway" / "runtime_identity.py").is_file()
    previous_fingerprint = fingerprint_fn(root)
    prior_services = services.running_services()
    runtime_before = services.inventory()
    snapshot = snapshots.create(previous_sha)
    if not snapshots.verify(snapshot):
        raise PreflightError("predeploy snapshot verification failed")
    preflight_checks.append(
        HealthCheck("preflight:snapshot", True, "predeploy snapshot verified")
    )

    record = DeploymentRecord(
        id=_record_id(request.sha),
        requested_sha=request.sha,
        previous_sha=previous_sha,
        snapshot=snapshot,
        runtime_before=runtime_before,
        runtime_after=None,
        checks=tuple(preflight_checks),
        status="preparing",
        rollback=None,
    )
    record_store.write(record)

    state_may_have_changed = False
    candidate_fingerprint = previous_fingerprint
    candidate_report = HealthReport(checks=())
    try:
        services.stop(prior_services)
        record = replace(record, status="deploying")
        record_store.write(record)

        _run_required(
            runner,
            ["git", "switch", "--detach", request.sha],
            cwd=root,
        )
        candidate_fingerprint = fingerprint_fn(root)
        _run_required(
            runner,
            _uv_sync_command(config),
            cwd=root,
        )
        for command in config.postinstall_commands:
            state_may_have_changed = True
            _run_required(runner, list(command), cwd=root)

        record = replace(record, status="restarting")
        record_store.write(record)
        state_may_have_changed = True
        services.start(prior_services)
        record = replace(record, status="verifying")
        record_store.write(record)

        candidate_report = health.check(
            expected_sha=request.sha,
            services=prior_services,
            identity_required=True,
            apply_injection=True,
        )
        if not candidate_report.healthy:
            raise _HealthFailure(candidate_report)
        record = replace(
            record,
            runtime_after=services.inventory(),
            checks=tuple(preflight_checks) + candidate_report.checks,
            status="deployed",
        )
        record_store.write(record)
        return record
    except Exception as failure:
        if not isinstance(failure, _HealthFailure):
            logger.exception("Exact-SHA deployment failed; starting rollback")
        if isinstance(failure, _HealthFailure):
            candidate_report = failure.report
        rollback = {"trigger": str(failure), "status": "running"}
        record = replace(
            record,
            checks=tuple(preflight_checks) + candidate_report.checks,
            status="rolling_back",
            rollback=rollback,
        )
        record_store.write(record)
        try:
            loaded_during_failure = services.loaded_services()
            candidate_services_loaded = tuple(
                service
                for service in prior_services
                if service in loaded_during_failure
            )
            if candidate_services_loaded:
                services.stop(candidate_services_loaded)
            _run_required(
                runner,
                ["git", "switch", "--detach", previous_sha],
                cwd=root,
            )
            if state_may_have_changed:
                snapshots.restore(snapshot)
            if candidate_fingerprint != previous_fingerprint:
                _run_required(
                    runner,
                    _uv_sync_command(config),
                    cwd=root,
                )
            services.start(prior_services)
            rollback_report = health.check(
                expected_sha=previous_sha,
                services=prior_services,
                identity_required=previous_identity_required,
                apply_injection=False,
            )
            final_status = (
                "rolled_back_healthy" if rollback_report.healthy else "rollback_failed"
            )
            rollback = {
                "trigger": str(failure),
                "status": final_status,
                "checks": _jsonable(rollback_report.checks),
            }
            record = replace(
                record,
                runtime_after=services.inventory(),
                checks=(
                    tuple(preflight_checks)
                    + candidate_report.checks
                    + rollback_report.checks
                ),
                status=final_status,
                rollback=rollback,
            )
        except Exception as rollback_failure:
            logger.exception("Exact-SHA deployment rollback failed")
            record = replace(
                record,
                status="rollback_failed",
                rollback={
                    "trigger": str(failure),
                    "status": "rollback_failed",
                    "error": str(rollback_failure),
                },
            )
        record_store.write(record)
        return record
