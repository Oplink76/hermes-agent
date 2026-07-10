"""Approval-gated exact-SHA deployment with automatic rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .command import CommandRunner
from .health import HealthCheck, HealthReport


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


@dataclass(frozen=True)
class DeployRequest:
    sha: str
    pr_number: int
    approval_record: ApprovalRecord
    actor: str


@dataclass(frozen=True)
class DeployConfig:
    install_root: Path
    origin: str
    record_root: Path
    required_approver: str = "Ole Ørum-Petersen"
    required_check: str = "All required checks pass"
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
    ) -> HealthReport: ...


class RecordStore(Protocol):
    def write(self, record: DeploymentRecord) -> None: ...


class DeploymentStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve(strict=False)

    def write(self, record: DeploymentRecord) -> None:
        path = self.root / f"{record.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record.to_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise


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


def _validate_approval(
    request: DeployRequest,
    config: DeployConfig,
    evidence: ReleaseEvidence,
) -> None:
    approval = request.approval_record
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
    if not evidence.merged:
        raise PreflightError("pull request is not merged")
    if evidence.required_check != config.required_check:
        raise PreflightError("required GitHub check is missing")
    if evidence.required_check_conclusion.lower() != "success":
        raise PreflightError("required GitHub check did not conclude success")


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
    root = Path(config.install_root).expanduser().resolve(strict=True)
    record_store = store or DeploymentStore(config.record_root)

    evidence = github.verify(request.pr_number)
    _validate_approval(request, config, evidence)
    preflight_checks = [
        HealthCheck(
            "preflight:approval",
            True,
            f"approver={request.approval_record.approver} "
            f"pr={request.pr_number} actor={request.actor} "
            f"packet={request.approval_record.decision_packet_sha256}",
        ),
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
            ["env", "UV_PROJECT_ENVIRONMENT=.venv", "uv", "sync", "--locked"],
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
            services.stop(prior_services)
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
                    [
                        "env",
                        "UV_PROJECT_ENVIRONMENT=.venv",
                        "uv",
                        "sync",
                        "--locked",
                    ],
                    cwd=root,
                )
            services.start(prior_services)
            rollback_report = health.check(
                expected_sha=previous_sha,
                services=prior_services,
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
