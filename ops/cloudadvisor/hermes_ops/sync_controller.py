"""Single-lock controller for autonomous protected Hermes fork sync."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from .command import CommandRunner
from .deploy import DeployConfig, DeploymentRecord, PreflightError
from .locking import try_exclusive_file_lock
from .sync import (
    FIXED_CANDIDATE_BRANCH,
    ConflictResolver,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
    prepare_candidate,
)
from .sync_reconstruction import (
    ReconstructionError,
    resume_failed_candidate_reconstruction,
)
from .sync_reconstruction_checkpoint import (
    MAX_RESUME_ATTEMPTS,
    PendingReconstructionCheckpoint,
    ReconstructionCheckpointError,
    clear_pending_reconstruction,
    load_pending_reconstruction,
    write_pending_reconstruction,
)
from .sync_github import SyncGitHubPort, SyncPullRequestEvidence, bind_expected_base
from .sync_github import SyncGitHubError
from .sync_receipt import (
    SyncReceiptArtifact,
    SyncReceiptError,
    finalize_sync_receipt,
    write_sync_receipt,
)
from .sync_poll import (
    ExactHeadExpectation,
    ExactHeadPollError,
    RequiredCheckRedError,
    poll_exact_head,
)
from .sync_remediation import SyncRemediationError, SyncRemediationPort
from .sync_resolution import (
    ResolutionRecordArtifact,
    ResolutionRecordError,
    freeze_resolution_record,
)
from .sync_recovery import (
    ProtectedRevertGitHubPort,
    ProtectedRevertState,
    is_quarantined,
    run_protected_revert,
)
from .sync_review import (
    ConflictReviewError,
    ConflictReviewReceipt,
    IndependentConflictReviewer,
    validate_conflict_review,
)


class AutonomousSyncState(str, Enum):
    NO_CHANGE = "NO_CHANGE"
    DEPLOYED = "DEPLOYED"
    ROLLED_BACK_REVERTED = "ROLLED_BACK_REVERTED"
    NEEDS_OLE = "NEEDS_OLE"
    LOCKED = "LOCKED"
    REFRESH_REQUIRED = "REFRESH_REQUIRED"
    PENDING_REFRESH = "PENDING_REFRESH"


@dataclass(frozen=True)
class AutonomousSyncResult:
    state: AutonomousSyncState
    candidate_sha: str | None = None
    pr_number: int | None = None
    merge_sha: str | None = None
    deployed_sha: str | None = None
    fork_main_sha: str | None = None
    installed_sha: str | None = None
    needs_ole: bool = False
    notify_ole: bool = False
    reason: str | None = None

    @classmethod
    def locked(cls) -> "AutonomousSyncResult":
        return cls(state=AutonomousSyncState.LOCKED, reason="sync lock held")

    @classmethod
    def no_change(cls, candidate: SyncResult) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.NO_CHANGE,
            candidate_sha=candidate.candidate_sha,
            pr_number=candidate.pr_number,
        )

    @classmethod
    def needs_human(
        cls,
        reason: str,
        *,
        candidate_sha: str | None = None,
        pr_number: int | None = None,
        merge_sha: str | None = None,
        installed_sha: str | None = None,
    ) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.NEEDS_OLE,
            candidate_sha=candidate_sha,
            pr_number=pr_number,
            merge_sha=merge_sha,
            installed_sha=installed_sha,
            needs_ole=True,
            reason=reason,
        )

    @classmethod
    def refresh_required(cls, candidate: SyncResult) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.PENDING_REFRESH,
            candidate_sha=candidate.candidate_sha,
            pr_number=candidate.pr_number,
            reason="official upstream changed; candidate refresh required",
        )

    @classmethod
    def pending(
        cls, candidate: SyncResult, *, reason: str
    ) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.PENDING_REFRESH,
            candidate_sha=candidate.candidate_sha,
            pr_number=candidate.pr_number,
            needs_ole=False,
            reason=reason,
        )


@dataclass(frozen=True)
class AutonomousSyncConfig:
    sync: SyncConfig
    deploy: DeployConfig
    receipt_root: Path
    required_check: str
    check_timeout_seconds: int = 2700
    poll_interval_seconds: int = 15
    resolver_backend: str | None = None
    resolution_record: Path | None = None
    max_upstream_refreshes: int = 2

    def __post_init__(self) -> None:
        if self.check_timeout_seconds <= 0 or self.poll_interval_seconds <= 0:
            raise ValueError("sync polling settings must be positive")
        if self.max_upstream_refreshes < 0:
            raise ValueError("upstream refresh budget must not be negative")
        if not self.required_check.strip():
            raise ValueError("required check must not be empty")
        if self.deploy.repo_slug and self.deploy.repo_slug != self.sync.repo_slug:
            raise ValueError("sync and deploy repository scopes must match")
        if self.deploy.required_check != self.required_check:
            raise ValueError("sync and deploy required checks must match")
        if self.deploy.sync_receipt_root is not None and (
            Path(self.deploy.sync_receipt_root).resolve(strict=False)
            != Path(self.receipt_root).resolve(strict=False)
        ):
            raise ValueError("sync and deploy receipt roots must match")

    @property
    def quarantine_root(self) -> Path:
        return Path(self.receipt_root) / "quarantine"


class AutonomousSyncError(RuntimeError):
    """An exact evidence gate could not authorize further mutation."""


class _OutcomePublicationError(RuntimeError):
    def __init__(self, cause: Exception):
        super().__init__("autonomous sync outcome publication failed")
        self.cause = cause


logger = logging.getLogger(__name__)


def _log_unexpected(message: str, error: Exception) -> None:
    sanitized = RuntimeError("redacted unexpected failure")
    logger.error(
        message,
        exc_info=(type(sanitized), sanitized, error.__traceback__),
    )


def require_conflict_review(
    candidate: SyncResult,
    *,
    reviewer: IndependentConflictReviewer | None,
    worktree: Path | None = None,
    resolution_record: Path | None = None,
    resolver_backend: str | None = None,
) -> tuple[SyncResult, ConflictReviewReceipt | None]:
    if candidate.classification is SyncClassification.CLEAN:
        return candidate, None
    if candidate.classification is SyncClassification.MAJOR:
        raise AutonomousSyncError("candidate is classified as a major conflict")
    if candidate.classification is not SyncClassification.MINOR_REVIEW_REQUIRED:
        raise AutonomousSyncError("candidate conflict classification is not reviewable")
    if reviewer is None:
        raise AutonomousSyncError("minor conflict has no independent reviewer")
    if worktree is None or resolution_record is None or not resolver_backend:
        raise AutonomousSyncError("minor conflict review context is incomplete")
    receipt = reviewer.review(
        candidate_sha=candidate.candidate_sha,
        worktree=worktree,
        resolution_record=resolution_record,
    )
    classification = validate_conflict_review(
        receipt,
        candidate_sha=candidate.candidate_sha,
        resolver_backend=resolver_backend,
        resolution_record=resolution_record,
        conflicted_files=candidate.conflicted_files,
    )
    if classification is SyncClassification.MAJOR:
        raise AutonomousSyncError("independent review classified conflict as major")
    return replace(candidate, classification=classification), receipt


def wait_for_green_exact_head(
    github: SyncGitHubPort,
    candidate: SyncResult,
    *,
    required_check: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> SyncPullRequestEvidence:
    if candidate.pr_number is None:
        raise AutonomousSyncError("candidate PR number is missing")
    if not candidate.base_sha or not candidate.candidate_sha:
        raise AutonomousSyncError("candidate exact SHA evidence is incomplete")
    try:
        return poll_exact_head(
            github,
            ExactHeadExpectation(
                candidate.pr_number,
                candidate.base_sha,
                candidate.candidate_sha,
                required_check,
            ),
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )
    except RequiredCheckRedError:
        raise
    except ExactHeadPollError as exc:
        raise AutonomousSyncError(str(exc)) from exc


def _review_candidate(
    config: AutonomousSyncConfig,
    candidate: SyncResult,
    reviewer: IndependentConflictReviewer | None,
    runner: CommandRunner,
) -> tuple[SyncResult, ConflictReviewReceipt | None]:
    review_candidate = candidate
    if candidate.classification is SyncClassification.MINOR_REVIEW_REQUIRED:
        if candidate.resolution_strategy == "candidate_repair":
            branch = runner.run(
                ["git", "branch", "--show-current"],
                cwd=config.sync.worktree,
                timeout=300,
            )
            status = runner.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=config.sync.worktree,
                timeout=300,
            )
            if (
                branch.returncode != 0
                or (branch.stdout or "").strip() != FIXED_CANDIDATE_BRANCH
                or status.returncode != 0
                or (status.stdout or "").strip()
            ):
                raise AutonomousSyncError("candidate review worktree is not disposable")
            for argv in (
                ["git", "reset", "--hard", candidate.candidate_sha],
                ["git", "clean", "-fd"],
            ):
                if runner.run(argv, cwd=config.sync.worktree, timeout=300).returncode != 0:
                    raise AutonomousSyncError("candidate review worktree refresh failed")
            head = runner.run(
                ["git", "rev-parse", "HEAD"],
                cwd=config.sync.worktree,
                timeout=300,
            )
            if head.returncode != 0 or (head.stdout or "").strip() != candidate.candidate_sha:
                raise AutonomousSyncError("candidate review worktree head is not exact")
        if candidate.resolution_evidence_dir is None:
            try:
                resolution_artifact = ResolutionRecordArtifact.load(
                    candidate.resolution_record or Path()
                )
            except ResolutionRecordError as exc:
                raise AutonomousSyncError(
                    "checkpoint resolution artifact is invalid"
                ) from exc
            paths = tuple(row["path"] for row in resolution_artifact.conflicts)
            if (
                resolution_artifact.candidate_sha != candidate.candidate_sha
                or resolution_artifact.strategy != candidate.resolution_strategy
                or set(paths) != set(candidate.conflicted_files)
                or len(paths) != len(candidate.conflicted_files)
            ):
                raise AutonomousSyncError(
                    "checkpoint resolution artifact is not exact"
                )
        else:
            resolution_artifact = freeze_resolution_record(
                config.receipt_root, candidate
            )
        review_candidate = replace(
            candidate, resolution_record=resolution_artifact.path
        )
    return require_conflict_review(
        review_candidate,
        reviewer=reviewer,
        worktree=config.sync.worktree,
        resolution_record=(
            review_candidate.resolution_record or config.resolution_record
        ),
        resolver_backend=config.resolver_backend,
    )


def _valid_repair(
    previous: SyncResult,
    repaired: SyncResult | None,
    *,
    runner: CommandRunner,
    repo: Path,
) -> SyncResult:
    if repaired is None:
        raise AutonomousSyncError("bounded candidate repair did not produce a candidate")
    if (
        not previous.candidate_sha
        or not repaired.candidate_sha
        or repaired.candidate_sha == previous.candidate_sha
        or not previous.candidate_tree_sha
        or not repaired.candidate_tree_sha
        or repaired.candidate_tree_sha == previous.candidate_tree_sha
        or repaired.state is not SyncState.PR_UPDATED
        or repaired.pr_number != previous.pr_number
        or repaired.base_sha != previous.base_sha
        or repaired.upstream_sha != previous.upstream_sha
        or repaired.classification is not SyncClassification.MINOR_REVIEW_REQUIRED
        or repaired.resolution_strategy != "candidate_repair"
        or repaired.resolution_record is None
        or repaired.resolution_evidence_dir is None
        or not repaired.conflicted_files
        or not repaired.checks
        or any(check.status != "passed" for check in repaired.checks)
    ):
        raise AutonomousSyncError("bounded candidate repair evidence is invalid")
    ancestry = runner.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            previous.candidate_sha,
            repaired.candidate_sha,
        ],
        cwd=repo,
        timeout=300,
    )
    parent = runner.run(
        ["git", "rev-parse", f"{repaired.candidate_sha}^"],
        cwd=repo,
        timeout=300,
    )
    repair_diff = runner.run(
        [
            "git",
            "diff",
            "--name-only",
            "-z",
            f"{previous.candidate_sha}..{repaired.candidate_sha}",
        ],
        cwd=repo,
        timeout=300,
    )
    actual_repair_paths = tuple(
        path for path in (repair_diff.stdout or "").split("\0") if path
    )
    if (
        ancestry.returncode != 0
        or parent.returncode != 0
        or (parent.stdout or "").strip() != previous.candidate_sha
        or repair_diff.returncode != 0
        or not actual_repair_paths
        or len(actual_repair_paths) != len(set(actual_repair_paths))
        or len(repaired.conflicted_files) != len(set(repaired.conflicted_files))
        or set(actual_repair_paths) != set(repaired.conflicted_files)
    ):
        raise AutonomousSyncError("bounded candidate repair lineage is invalid")
    return repaired


def _upstream_is_current(
    config: AutonomousSyncConfig,
    candidate: SyncResult,
    runner: CommandRunner,
) -> bool:
    return _refresh_current_upstream_sha(config, runner) == candidate.upstream_sha


def _refresh_current_upstream_sha(
    config: AutonomousSyncConfig,
    runner: CommandRunner,
) -> str:
    fetched = runner.run(
        ["git", "fetch", config.sync.upstream, "main"],
        cwd=config.sync.repo,
        timeout=600,
    )
    if fetched.returncode != 0:
        raise AutonomousSyncError("official upstream refresh failed")
    resolved = runner.run(
        ["git", "rev-parse", f"{config.sync.upstream}/main"],
        cwd=config.sync.repo,
        timeout=300,
    )
    current = (resolved.stdout or "").strip()
    if resolved.returncode != 0 or not current:
        raise AutonomousSyncError("official upstream identity is unavailable")
    return current


def _current_upstream_sha(
    config: AutonomousSyncConfig,
    runner: CommandRunner,
) -> str:
    resolved = runner.run(
        ["git", "rev-parse", f"{config.sync.upstream}/main"],
        cwd=config.sync.repo,
        timeout=300,
    )
    current = (resolved.stdout or "").strip()
    if resolved.returncode != 0 or len(current) != 40:
        raise AutonomousSyncError("official upstream identity is unavailable")
    return current


def _failed_from_checkpoint(
    checkpoint: PendingReconstructionCheckpoint,
) -> SyncResult:
    return SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=checkpoint.failed_base_sha,
        upstream_sha=checkpoint.failed_upstream_sha,
        candidate_sha=checkpoint.failed_candidate_sha,
        candidate_tree_sha=checkpoint.failed_candidate_tree_sha,
        pr_number=checkpoint.failed_pr_number,
        classification=SyncClassification.CLEAN,
    )


def _reconstructed_from_checkpoint(
    checkpoint: PendingReconstructionCheckpoint,
) -> SyncResult:
    if checkpoint.stage not in {"reconstructed", "repaired"}:
        raise ReconstructionCheckpointError(
            "checkpoint has no reconstructed candidate"
        )
    return SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=checkpoint.revert_main_sha,
        upstream_sha=checkpoint.target_upstream_sha,
        candidate_sha=checkpoint.reconstructed_candidate_sha,
        candidate_tree_sha=checkpoint.reconstructed_candidate_tree_sha,
        pr_number=checkpoint.reconstructed_pr_number,
        risk="post_revert_reconstruction",
        changed_files=checkpoint.reconstructed_changed_files,
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=checkpoint.reconstructed_changed_files,
        resolution_strategy="candidate_repair",
    )


def _repaired_from_checkpoint(
    config: AutonomousSyncConfig,
    checkpoint: PendingReconstructionCheckpoint,
) -> SyncResult:
    if checkpoint.stage != "repaired" or not checkpoint.resolution_record_sha256:
        raise ReconstructionCheckpointError("checkpoint has no repaired candidate")
    resolution_path = (
        Path(config.receipt_root)
        / "resolutions"
        / f"resolution-{checkpoint.resolution_record_sha256}.json"
    )
    try:
        resolution = ResolutionRecordArtifact.load(resolution_path)
    except ResolutionRecordError as exc:
        raise ReconstructionCheckpointError(
            "checkpoint resolution evidence is invalid"
        ) from exc
    resolution_paths = tuple(row["path"] for row in resolution.conflicts)
    if (
        resolution.sha256 != checkpoint.resolution_record_sha256
        or resolution.candidate_sha != checkpoint.repaired_candidate_sha
        or resolution.strategy != "candidate_repair"
        or set(resolution_paths) != set(checkpoint.repair_paths)
        or len(resolution_paths) != len(checkpoint.repair_paths)
    ):
        raise ReconstructionCheckpointError(
            "checkpoint repair evidence is not exact"
        )
    return SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=checkpoint.revert_main_sha,
        upstream_sha=checkpoint.target_upstream_sha,
        candidate_sha=checkpoint.repaired_candidate_sha,
        candidate_tree_sha=checkpoint.repaired_candidate_tree_sha,
        pr_number=checkpoint.repaired_pr_number,
        checks=checkpoint.repaired_checks,
        risk="post_revert_repair",
        changed_files=checkpoint.reconstructed_changed_files,
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=checkpoint.repair_paths,
        resolution_record=resolution.path,
        resolution_strategy="candidate_repair",
    )


def _checkpoint_reconstructed(
    checkpoint: PendingReconstructionCheckpoint,
    candidate: SyncResult,
) -> PendingReconstructionCheckpoint:
    if (
        not candidate.candidate_sha
        or not candidate.candidate_tree_sha
        or candidate.pr_number is None
        or not candidate.changed_files
        or not candidate.upstream_sha
    ):
        raise ReconstructionCheckpointError(
            "reconstructed candidate evidence is incomplete"
        )
    return replace(
        checkpoint,
        stage="reconstructed",
        target_upstream_sha=candidate.upstream_sha,
        expected_rolling_candidate_sha=candidate.candidate_sha,
        reconstructed_candidate_sha=candidate.candidate_sha,
        reconstructed_candidate_tree_sha=candidate.candidate_tree_sha,
        reconstructed_pr_number=candidate.pr_number,
        reconstructed_changed_files=candidate.changed_files,
        repaired_candidate_sha=None,
        repaired_candidate_tree_sha=None,
        repaired_pr_number=None,
        repair_paths=(),
        repaired_checks=(),
        resolution_record_sha256=None,
    )


def _checkpoint_repaired(
    config: AutonomousSyncConfig,
    checkpoint: PendingReconstructionCheckpoint,
    candidate: SyncResult,
) -> tuple[PendingReconstructionCheckpoint, SyncResult]:
    resolution = freeze_resolution_record(config.receipt_root, candidate)
    repaired = replace(
        candidate,
        resolution_record=resolution.path,
        resolution_evidence_dir=None,
    )
    value = replace(
        checkpoint,
        stage="repaired",
        expected_rolling_candidate_sha=candidate.candidate_sha or "",
        repaired_candidate_sha=candidate.candidate_sha,
        repaired_candidate_tree_sha=candidate.candidate_tree_sha,
        repaired_pr_number=candidate.pr_number,
        repair_paths=candidate.conflicted_files,
        repaired_checks=candidate.checks,
        resolution_record_sha256=resolution.sha256,
    )
    return value, repaired


def attest_candidate(
    config: AutonomousSyncConfig,
    candidate: SyncResult,
    evidence: SyncPullRequestEvidence,
    *,
    conflict_review: ConflictReviewReceipt | None = None,
) -> SyncReceiptArtifact:
    return write_sync_receipt(
        config.receipt_root,
        candidate,
        evidence,
        repo_slug=config.sync.repo_slug,
        conflict_review=conflict_review,
    )


def finish_or_recover(
    config: AutonomousSyncConfig,
    candidate: SyncResult,
    deployment: DeploymentRecord,
    *,
    merge_sha: str,
    runner: CommandRunner,
    github: ProtectedRevertGitHubPort,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    verify_runtime_fn: Callable[[str], bool],
) -> AutonomousSyncResult:
    if deployment.status == "deployed":
        if deployment.requested_sha != merge_sha:
            return AutonomousSyncResult.needs_human(
                "deployment record does not match exact merge SHA",
                candidate_sha=candidate.candidate_sha,
                pr_number=candidate.pr_number,
                merge_sha=merge_sha,
            )
        return AutonomousSyncResult(
            state=AutonomousSyncState.DEPLOYED,
            candidate_sha=candidate.candidate_sha,
            pr_number=candidate.pr_number,
            merge_sha=merge_sha,
            deployed_sha=deployment.requested_sha,
            fork_main_sha=merge_sha,
            installed_sha=deployment.requested_sha,
        )
    recovery = run_protected_revert(
        repo=config.sync.repo,
        origin=config.sync.origin,
        repo_slug=config.sync.repo_slug,
        required_check=config.required_check,
        candidate=candidate,
        merge_sha=merge_sha,
        deployment=deployment,
        quarantine_root=config.quarantine_root,
        runner=runner,
        github=github,
        clock=clock,
        sleeper=sleeper,
        timeout_seconds=config.check_timeout_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
        verify_runtime_fn=verify_runtime_fn,
    )
    if recovery.state is ProtectedRevertState.REVERTED:
        return AutonomousSyncResult(
            state=AutonomousSyncState.ROLLED_BACK_REVERTED,
            candidate_sha=candidate.candidate_sha,
            pr_number=candidate.pr_number,
            merge_sha=merge_sha,
            fork_main_sha=recovery.revert_merge_sha,
            installed_sha=recovery.installed_sha,
            reason="runtime rolled back and protected revert merged",
        )
    return AutonomousSyncResult.needs_human(
        recovery.reason or "automatic recovery failed",
        candidate_sha=candidate.candidate_sha,
        pr_number=candidate.pr_number,
        merge_sha=merge_sha,
        installed_sha=recovery.installed_sha,
    )


def run_autonomous_sync(
    config: AutonomousSyncConfig,
    *,
    runner: CommandRunner,
    github: ProtectedRevertGitHubPort,
    resolver: ConflictResolver | None,
    reviewer: IndependentConflictReviewer | None,
    remediator: SyncRemediationPort | None = None,
    deploy_fn: Callable[[Path, str, int], DeploymentRecord],
    verify_runtime_fn: Callable[[str], bool] | None = None,
    publish_outcome: Callable[[AutonomousSyncResult], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> AutonomousSyncResult:
    """Converge one exact candidate or return one bounded terminal state."""
    with try_exclusive_file_lock(config.sync.lock_path) as acquired:
        if not acquired:
            return AutonomousSyncResult.locked()

        def finish(result: AutonomousSyncResult) -> AutonomousSyncResult:
            if publish_outcome is None:
                return result
            try:
                notify_ole = bool(publish_outcome(result))
            except Exception as exc:
                raise _OutcomePublicationError(exc) from exc
            return replace(result, notify_ole=notify_ole)

        candidate: SyncResult | None = None
        merge_sha: str | None = None
        infrastructure_retry_used = False
        candidate_repair_used = False
        post_rollback_repair = False
        upstream_refreshes = 0
        reconstruction_source: SyncResult | None = None
        reconstruction_merge_sha: str | None = None
        reconstruction_revert_main_sha: str | None = None
        reconstruction_installed_sha: str | None = None
        reconstruction_checkpoint: PendingReconstructionCheckpoint | None = None
        reconstruction_checkpoint_sha: str | None = None
        try:
            pending_reconstruction = load_pending_reconstruction(
                config.receipt_root,
                repo_slug=config.sync.repo_slug,
            )
            if pending_reconstruction is not None:
                if pending_reconstruction.resume_attempts >= MAX_RESUME_ATTEMPTS:
                    raise AutonomousSyncError(
                        "pending reconstruction retry budget is exhausted"
                    )
                if verify_runtime_fn is None:
                    raise AutonomousSyncError(
                        "pending reconstruction dependencies are unavailable"
                    )
                if (
                    pending_reconstruction.stage != "repaired"
                    and remediator is None
                ):
                    raise AutonomousSyncError(
                        "pending reconstruction remediator is unavailable"
                    )
                if not verify_runtime_fn(
                    pending_reconstruction.previous_healthy_installed_sha
                ):
                    raise AutonomousSyncError(
                        "pending reconstruction previous install is not healthy"
                    )
                pending_reconstruction = replace(
                    pending_reconstruction,
                    resume_attempts=pending_reconstruction.resume_attempts + 1,
                )
                pending_artifact = write_pending_reconstruction(
                    config.receipt_root, pending_reconstruction
                )
                reconstruction_checkpoint = pending_reconstruction
                reconstruction_checkpoint_sha = pending_artifact.sha256
                reconstruction_source = _failed_from_checkpoint(
                    pending_reconstruction
                )
                reconstruction_merge_sha = pending_reconstruction.failed_merge_sha
                reconstruction_revert_main_sha = (
                    pending_reconstruction.revert_main_sha
                )
                reconstruction_installed_sha = (
                    pending_reconstruction.previous_healthy_installed_sha
                )
                if pending_reconstruction.stage == "recovered":
                    current_upstream = _refresh_current_upstream_sha(config, runner)
                    if current_upstream != pending_reconstruction.target_upstream_sha:
                        pending_reconstruction = replace(
                            pending_reconstruction,
                            target_upstream_sha=current_upstream,
                            reason=(
                                "official upstream advanced while recovery was "
                                "pending; complete-tree reconstruction refreshed"
                            ),
                        )
                        pending_artifact = write_pending_reconstruction(
                            config.receipt_root, pending_reconstruction
                        )
                        reconstruction_checkpoint = pending_reconstruction
                        reconstruction_checkpoint_sha = pending_artifact.sha256
                    refreshed = resume_failed_candidate_reconstruction(
                        config.sync,
                        failed=reconstruction_source,
                        failed_merge_sha=reconstruction_merge_sha,
                        revert_main_sha=reconstruction_revert_main_sha,
                        expected_candidate_sha=(
                            pending_reconstruction.expected_rolling_candidate_sha
                        ),
                        current_upstream_sha=(
                            pending_reconstruction.target_upstream_sha
                        ),
                        github=github,
                        runner=runner,
                    )
                    pending_reconstruction = _checkpoint_reconstructed(
                        pending_reconstruction, refreshed
                    )
                    pending_artifact = write_pending_reconstruction(
                        config.receipt_root, pending_reconstruction
                    )
                    reconstruction_checkpoint = pending_reconstruction
                    reconstruction_checkpoint_sha = pending_artifact.sha256
                elif pending_reconstruction.stage == "reconstructed":
                    refreshed = _reconstructed_from_checkpoint(
                        pending_reconstruction
                    )
                else:
                    candidate = _repaired_from_checkpoint(
                        config, pending_reconstruction
                    )
                    refreshed = None
                if pending_reconstruction.stage != "repaired":
                    candidate = _valid_repair(
                        refreshed,
                        remediator.repair_candidate(
                            refreshed,
                            health_evidence=(pending_reconstruction.reason,),
                        ),
                        runner=runner,
                        repo=config.sync.repo,
                    )
                    pending_reconstruction, candidate = _checkpoint_repaired(
                        config, pending_reconstruction, candidate
                    )
                    pending_artifact = write_pending_reconstruction(
                        config.receipt_root, pending_reconstruction
                    )
                    reconstruction_checkpoint = pending_reconstruction
                    reconstruction_checkpoint_sha = pending_artifact.sha256
                candidate_repair_used = True
                post_rollback_repair = True
            else:
                candidate = prepare_candidate(
                    config.sync,
                    github=github,
                    runner=runner,
                    resolver=resolver,
                )
                if candidate.state is SyncState.NO_CHANGE:
                    return finish(AutonomousSyncResult.no_change(candidate))
            while True:
                if candidate.state is not SyncState.PR_UPDATED:
                    raise AutonomousSyncError(
                        f"candidate preparation stopped in {candidate.state.value}"
                    )
                if (
                    not post_rollback_repair
                    and is_quarantined(config.quarantine_root, candidate)
                ):
                    raise AutonomousSyncError(
                        "unchanged failed candidate is quarantined"
                    )

                reviewed, conflict_review = _review_candidate(
                    config, candidate, reviewer, runner
                )
                try:
                    evidence = wait_for_green_exact_head(
                        github,
                        reviewed,
                        required_check=config.required_check,
                        timeout_seconds=config.check_timeout_seconds,
                        poll_interval_seconds=config.poll_interval_seconds,
                        clock=clock,
                        sleeper=sleeper,
                    )
                except RequiredCheckRedError as first_red:
                    conclusion = first_red.evidence.required_check_conclusion
                    if conclusion != "failure":
                        return finish(
                            AutonomousSyncResult.pending(
                                reviewed,
                                reason=(
                                    "exact required check ended "
                                    f"{conclusion}; awaiting a new exact run"
                                ),
                            )
                        )
                    if remediator is None:
                        raise AutonomousSyncError(str(first_red)) from first_red
                    if not infrastructure_retry_used:
                        retried = remediator.retry_infrastructure(
                            reviewed, first_red.evidence
                        )
                        if retried:
                            infrastructure_retry_used = True
                            continue
                    if candidate_repair_used:
                        raise AutonomousSyncError(
                            "bounded candidate repair budget is exhausted"
                        )
                    candidate_repair_used = True
                    candidate = _valid_repair(
                        reviewed,
                        remediator.repair_candidate(reviewed),
                        runner=runner,
                        repo=config.sync.repo,
                    )
                    continue

                receipt = attest_candidate(
                    config,
                    reviewed,
                    evidence,
                    conflict_review=conflict_review,
                )
                if not _upstream_is_current(config, reviewed, runner):
                    if post_rollback_repair:
                        if (
                            reconstruction_checkpoint is None
                            or reviewed.candidate_sha is None
                        ):
                            raise AutonomousSyncError(
                                "post-revert reconstruction context is incomplete"
                            )
                        reason = (
                            "official upstream advanced during post-revert repair; "
                            "complete-tree reconstruction must restart"
                        )
                        pending = replace(
                            reconstruction_checkpoint,
                            stage="recovered",
                            target_upstream_sha=_current_upstream_sha(config, runner),
                            expected_rolling_candidate_sha=reviewed.candidate_sha,
                            reconstructed_candidate_sha=None,
                            reconstructed_candidate_tree_sha=None,
                            reconstructed_pr_number=None,
                            reconstructed_changed_files=(),
                            repaired_candidate_sha=None,
                            repaired_candidate_tree_sha=None,
                            repaired_pr_number=None,
                            repair_paths=(),
                            repaired_checks=(),
                            resolution_record_sha256=None,
                            reason=reason,
                        )
                        pending_artifact = write_pending_reconstruction(
                            config.receipt_root, pending
                        )
                        reconstruction_checkpoint = pending
                        reconstruction_checkpoint_sha = pending_artifact.sha256
                        return finish(
                            AutonomousSyncResult.pending(
                                reviewed,
                                reason=reason,
                            )
                        )
                    if upstream_refreshes >= config.max_upstream_refreshes:
                        return finish(
                            AutonomousSyncResult.refresh_required(reviewed)
                        )
                    upstream_refreshes += 1
                    previous_head = candidate.candidate_sha
                    candidate = prepare_candidate(
                        config.sync,
                        github=github,
                        runner=runner,
                        resolver=resolver,
                    )
                    if candidate.state is SyncState.NO_CHANGE:
                        return finish(AutonomousSyncResult.no_change(candidate))
                    if candidate.candidate_sha != previous_head:
                        infrastructure_retry_used = False
                        candidate_repair_used = False
                    post_rollback_repair = False
                    continue
                bind_expected_base(github, evidence.base_sha)
                merge_sha = github.merge_exact(
                    evidence.number, expected_head=reviewed.candidate_sha
                )
                final_receipt = finalize_sync_receipt(
                    receipt.path, merge_sha=merge_sha
                )
                deployment = deploy_fn(
                    final_receipt.path, merge_sha, evidence.number
                )
                if deployment.status != "deployed" and verify_runtime_fn is None:
                    raise AutonomousSyncError(
                        "runtime health verifier is unavailable for recovery"
                    )
                outcome = finish_or_recover(
                    config,
                    reviewed,
                    deployment,
                    merge_sha=merge_sha,
                    runner=runner,
                    github=github,
                    clock=clock,
                    sleeper=sleeper,
                    verify_runtime_fn=verify_runtime_fn or (lambda sha: False),
                )
                if outcome.state is not AutonomousSyncState.ROLLED_BACK_REVERTED:
                    if (
                        outcome.state is AutonomousSyncState.DEPLOYED
                        and reconstruction_checkpoint_sha is not None
                    ):
                        clear_pending_reconstruction(
                            config.receipt_root,
                            sha256=reconstruction_checkpoint_sha,
                        )
                    return finish(outcome)
                if not outcome.fork_main_sha:
                    raise AutonomousSyncError(
                        "protected rollback did not report reconstructed base"
                    )
                reconstruction_source = reviewed
                reconstruction_merge_sha = merge_sha
                reconstruction_revert_main_sha = outcome.fork_main_sha
                reconstruction_installed_sha = outcome.installed_sha
                if not reconstruction_installed_sha:
                    raise AutonomousSyncError(
                        "protected rollback did not report healthy install identity"
                    )
                reconstruction_checkpoint = PendingReconstructionCheckpoint(
                    schema_version=2,
                    repo_slug=config.sync.repo_slug,
                    stage="recovered",
                    failed_base_sha=reviewed.base_sha or "",
                    failed_upstream_sha=reviewed.upstream_sha or "",
                    failed_candidate_sha=reviewed.candidate_sha or "",
                    failed_candidate_tree_sha=reviewed.candidate_tree_sha or "",
                    failed_pr_number=reviewed.pr_number or 0,
                    failed_merge_sha=merge_sha,
                    revert_main_sha=outcome.fork_main_sha,
                    previous_healthy_installed_sha=reconstruction_installed_sha,
                    target_upstream_sha=reviewed.upstream_sha or "",
                    expected_rolling_candidate_sha=reviewed.candidate_sha or "",
                    reconstructed_candidate_sha=None,
                    reconstructed_candidate_tree_sha=None,
                    reconstructed_pr_number=None,
                    reconstructed_changed_files=(),
                    repaired_candidate_sha=None,
                    repaired_candidate_tree_sha=None,
                    repaired_pr_number=None,
                    repair_paths=(),
                    repaired_checks=(),
                    resolution_record_sha256=None,
                    reason="healthy rollback and protected revert completed",
                    resume_attempts=0,
                )
                pending_artifact = write_pending_reconstruction(
                    config.receipt_root, reconstruction_checkpoint
                )
                reconstruction_checkpoint_sha = pending_artifact.sha256
                if candidate_repair_used or remediator is None:
                    return finish(
                        AutonomousSyncResult.needs_human(
                            "protected rollback completed; bounded repair is exhausted",
                            candidate_sha=reviewed.candidate_sha,
                            pr_number=reviewed.pr_number,
                            merge_sha=merge_sha,
                            installed_sha=outcome.installed_sha,
                        )
                    )
                current_upstream = _refresh_current_upstream_sha(config, runner)
                if current_upstream != reconstruction_checkpoint.target_upstream_sha:
                    reconstruction_checkpoint = replace(
                        reconstruction_checkpoint,
                        target_upstream_sha=current_upstream,
                        reason=(
                            "official upstream advanced after protected recovery; "
                            "complete-tree reconstruction refreshed"
                        ),
                    )
                    pending_artifact = write_pending_reconstruction(
                        config.receipt_root, reconstruction_checkpoint
                    )
                    reconstruction_checkpoint_sha = pending_artifact.sha256
                refreshed = resume_failed_candidate_reconstruction(
                    config.sync,
                    failed=reviewed,
                    failed_merge_sha=merge_sha,
                    revert_main_sha=outcome.fork_main_sha,
                    expected_candidate_sha=(
                        reconstruction_checkpoint.expected_rolling_candidate_sha
                    ),
                    current_upstream_sha=(
                        reconstruction_checkpoint.target_upstream_sha
                    ),
                    github=github,
                    runner=runner,
                )
                if refreshed.state is not SyncState.PR_UPDATED:
                    raise AutonomousSyncError(
                        "post-rollback candidate refresh did not produce a repair target"
                    )
                reconstruction_checkpoint = _checkpoint_reconstructed(
                    reconstruction_checkpoint, refreshed
                )
                pending_artifact = write_pending_reconstruction(
                    config.receipt_root, reconstruction_checkpoint
                )
                reconstruction_checkpoint_sha = pending_artifact.sha256
                candidate_repair_used = True
                health_evidence = tuple(
                    f"{check.name}:{'passed' if check.passed else 'failed'}"
                    for check in deployment.checks
                ) or (f"deployment:{deployment.status}",)
                candidate = _valid_repair(
                    refreshed,
                    remediator.repair_candidate(
                        refreshed, health_evidence=health_evidence
                    ),
                    runner=runner,
                    repo=config.sync.repo,
                )
                reconstruction_checkpoint, candidate = _checkpoint_repaired(
                    config, reconstruction_checkpoint, candidate
                )
                pending_artifact = write_pending_reconstruction(
                    config.receipt_root, reconstruction_checkpoint
                )
                reconstruction_checkpoint_sha = pending_artifact.sha256
                post_rollback_repair = True
        except _OutcomePublicationError as exc:
            raise exc.cause
        except (
            AutonomousSyncError,
        ) as exc:
            return finish(
                AutonomousSyncResult.needs_human(
                    str(exc),
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except ConflictReviewError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "conflict review evidence is invalid",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except ResolutionRecordError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "conflict resolution evidence is invalid",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except SyncReceiptError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "sync eligibility evidence is invalid",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except SyncGitHubError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "protected GitHub evidence is invalid",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except PreflightError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "automated deployment authority failed",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except SyncRemediationError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "bounded sync remediation failed",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except ReconstructionError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "post-rollback candidate reconstruction failed",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except ReconstructionCheckpointError:
            return finish(
                AutonomousSyncResult.needs_human(
                    "pending reconstruction evidence is invalid",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
        except Exception as exc:
            _log_unexpected("Autonomous sync failed unexpectedly", exc)
            return finish(
                AutonomousSyncResult.needs_human(
                    "unexpected autonomous sync failure",
                    candidate_sha=candidate.candidate_sha if candidate else None,
                    pr_number=candidate.pr_number if candidate else None,
                    merge_sha=merge_sha,
                )
            )
