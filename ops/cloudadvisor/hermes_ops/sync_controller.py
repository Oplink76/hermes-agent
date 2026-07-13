"""Single-lock controller for autonomous protected Hermes fork sync."""

from __future__ import annotations

import logging
import hashlib
import re
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
from .sync_deployment_checkpoint import (
    PendingDeploymentCheckpoint,
    SyncDeploymentCheckpointError,
    clear_pending_deployment,
    deployment_checkpoint_sha256,
    load_pending_deployment,
    terminalize_pending_deployment,
    write_pending_deployment,
)
from .sync_receipt import (
    SyncEligibilityReceipt,
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
    PENDING_REFRESH = "PENDING_REFRESH"


class ControllerStage(str, Enum):
    INITIAL = "initial"
    CANDIDATE = "candidate"
    MERGE_INTENT = "merge_intent"
    MERGED = "merged"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class ReconstructionRunContext:
    source: SyncResult
    failed_merge_sha: str
    revert_main_sha: str
    installed_sha: str
    checkpoint: PendingReconstructionCheckpoint
    checkpoint_sha256: str


@dataclass(frozen=True)
class ControllerRunState:
    stage: ControllerStage = ControllerStage.INITIAL
    candidate: SyncResult | None = None
    merge_sha: str | None = None
    infrastructure_retry_used: bool = False
    candidate_repair_used: bool = False
    upstream_refreshes: int = 0
    reconstruction: ReconstructionRunContext | None = None
    deployment_checkpoint_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.stage is ControllerStage.INITIAL and any(
            (
                self.candidate is not None,
                self.merge_sha is not None,
                self.infrastructure_retry_used,
                self.candidate_repair_used,
                self.upstream_refreshes,
                self.reconstruction is not None,
                self.deployment_checkpoint_sha256 is not None,
            )
        ):
            raise ValueError("controller initial stage contains operation evidence")
        if self.stage is not ControllerStage.INITIAL and self.candidate is None:
            raise ValueError("controller candidate evidence is required")
        if self.stage in {ControllerStage.MERGED, ControllerStage.RECOVERY} and (
            self.merge_sha is None
        ):
            raise ValueError("controller merge evidence is required")
        if self.stage is ControllerStage.RECOVERY and self.reconstruction is None:
            raise ValueError("controller recovery evidence is required")
        if self.stage is ControllerStage.MERGED and (
            self.deployment_checkpoint_sha256 is None
        ):
            raise ValueError("controller deployment checkpoint is required")
        if self.stage is ControllerStage.CANDIDATE and (
            self.deployment_checkpoint_sha256 is not None
            or (self.reconstruction is None) != (self.merge_sha is None)
        ):
            raise ValueError("controller candidate recovery evidence is crossed")
        if self.stage is ControllerStage.MERGE_INTENT and (
            self.merge_sha is not None
            or self.deployment_checkpoint_sha256 is None
        ):
            raise ValueError("controller merge intent evidence is crossed")
        if self.upstream_refreshes < 0:
            raise ValueError("controller refresh count is invalid")

    @property
    def post_rollback_repair(self) -> bool:
        return self.reconstruction is not None


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
    reason_code: str | None = None
    failed_gate: str | None = None
    affected_files: tuple[str, ...] = ()
    rollback_state: str | None = None
    rollback_sha: str | None = None
    revert_state: str | None = None
    revert_sha: str | None = None
    details_artifact: str | None = None

    def __post_init__(self) -> None:
        if self.reason_code is not None and re.fullmatch(
            r"[A-Z][A-Z0-9_]*", self.reason_code
        ) is None:
            raise ValueError("sync result reason code is invalid")
        if self.failed_gate is not None and re.fullmatch(
            r"[a-z][a-z0-9_]*", self.failed_gate
        ) is None:
            raise ValueError("sync result failed gate is invalid")
        if len(self.affected_files) != len(set(self.affected_files)) or any(
            not path
            or "\0" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in self.affected_files
        ):
            raise ValueError("sync result affected files are invalid")
        if self.details_artifact is not None and (
            not self.details_artifact
            or Path(self.details_artifact).is_absolute()
            or ".." in Path(self.details_artifact).parts
        ):
            raise ValueError("sync result details artifact is invalid")

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
        reason_code: str | None = None,
        failed_gate: str | None = None,
        affected_files: tuple[str, ...] = (),
        rollback_state: str | None = None,
        rollback_sha: str | None = None,
        revert_state: str | None = None,
        revert_sha: str | None = None,
        details_artifact: str | None = None,
    ) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.NEEDS_OLE,
            candidate_sha=candidate_sha,
            pr_number=pr_number,
            merge_sha=merge_sha,
            installed_sha=installed_sha,
            needs_ole=True,
            reason=reason,
            reason_code=reason_code,
            failed_gate=failed_gate,
            affected_files=affected_files,
            rollback_state=rollback_state,
            rollback_sha=rollback_sha,
            revert_state=revert_state,
            revert_sha=revert_sha,
            details_artifact=details_artifact,
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
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _log_unexpected(message: str, error: Exception) -> None:
    sanitized = RuntimeError("redacted unexpected failure")
    logger.error(
        message,
        exc_info=(type(sanitized), sanitized, error.__traceback__),
    )


def _git_sha(
    runner: CommandRunner,
    cwd: Path,
    ref: str,
) -> str:
    completed = runner.run(["git", "rev-parse", ref], cwd=cwd, timeout=30)
    value = (completed.stdout or "").strip()
    if completed.returncode != 0 or _FULL_SHA.fullmatch(value) is None:
        raise AutonomousSyncError(f"could not verify exact Git identity for {ref}")
    return value


def _write_merge_intent(
    config: AutonomousSyncConfig,
    *,
    candidate: SyncResult,
    evidence: SyncPullRequestEvidence,
    premerge_receipt: SyncReceiptArtifact,
    runner: CommandRunner,
) -> PendingDeploymentCheckpoint:
    if not isinstance(premerge_receipt.sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", premerge_receipt.sha256
    ) is None:
        raise AutonomousSyncError("premerge sync receipt digest is unavailable")
    checkpoint = PendingDeploymentCheckpoint(
        schema_version=1,
        repo_slug=config.sync.repo_slug,
        stage="merge_intent",
        candidate_sha=candidate.candidate_sha or "",
        candidate_tree_sha=candidate.candidate_tree_sha or "",
        pr_number=evidence.number,
        pr_head_sha=evidence.head_sha,
        base_sha=evidence.base_sha,
        upstream_sha=candidate.upstream_sha or "",
        premerge_receipt_path=str(
            Path(premerge_receipt.path).resolve(strict=False)
        ),
        premerge_receipt_sha256=premerge_receipt.sha256,
        merge_sha=None,
        final_receipt_path=None,
        final_receipt_sha256=None,
        install_root=str(Path(config.deploy.install_root).resolve(strict=False)),
        previous_installed_sha=_git_sha(
            runner, config.deploy.install_root, "HEAD"
        ),
        terminal_reason=None,
        terminal_reason_code=None,
        terminal_failed_gate=None,
        rollback_state=None,
        rollback_sha=None,
        revert_state=None,
        revert_sha=None,
    )
    write_pending_deployment(config.receipt_root, checkpoint)
    return checkpoint


def _advance_pending_deploy(
    config: AutonomousSyncConfig,
    intent: PendingDeploymentCheckpoint,
    *,
    merge_sha: str,
    final_receipt: SyncReceiptArtifact,
) -> tuple[PendingDeploymentCheckpoint, str]:
    if intent.stage != "merge_intent":
        raise AutonomousSyncError("pending merge intent stage changed")
    checkpoint = replace(
        intent,
        stage="merged_pending_deploy",
        merge_sha=merge_sha,
        final_receipt_path=str(Path(final_receipt.path).resolve(strict=False)),
        final_receipt_sha256=final_receipt.sha256,
    )
    artifact = write_pending_deployment(config.receipt_root, checkpoint)
    return checkpoint, artifact.sha256


def _candidate_from_pending_receipt(
    checkpoint: PendingDeploymentCheckpoint,
    receipt: SyncEligibilityReceipt,
) -> SyncResult:
    try:
        classification = SyncClassification(receipt.classification)
    except ValueError as exc:
        raise AutonomousSyncError(
            "pending deployment classification is invalid"
        ) from exc
    return SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=checkpoint.base_sha,
        upstream_sha=checkpoint.upstream_sha,
        candidate_sha=checkpoint.candidate_sha,
        candidate_tree_sha=checkpoint.candidate_tree_sha,
        pr_number=checkpoint.pr_number,
        checks=receipt.local_checks,
        classification=classification,
    )


def _load_trusted_sync_receipt(
    config: AutonomousSyncConfig,
    *,
    path_value: str | None,
    expected_digest: str | None,
) -> tuple[Path, SyncEligibilityReceipt]:
    if path_value is None or expected_digest is None:
        raise AutonomousSyncError("pending deployment receipt identity is missing")
    receipt_root = Path(config.receipt_root).resolve(strict=False)
    try:
        receipt_path = Path(path_value).resolve(strict=True)
        content = receipt_path.read_bytes()
    except OSError as exc:
        raise AutonomousSyncError(
            "pending deployment receipt is unreadable"
        ) from exc
    if not receipt_path.is_relative_to(receipt_root):
        raise AutonomousSyncError("pending deployment receipt escaped trusted root")
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise AutonomousSyncError("pending deployment receipt digest changed")
    return receipt_path, SyncEligibilityReceipt.load(receipt_path)


def _receipt_matches_checkpoint(
    receipt: SyncEligibilityReceipt,
    checkpoint: PendingDeploymentCheckpoint,
    *,
    required_check: str,
    merge_sha: str | None,
) -> bool:
    return (
        receipt.repo_slug == checkpoint.repo_slug
        and receipt.candidate_sha == checkpoint.candidate_sha
        and receipt.pr_head_sha == checkpoint.pr_head_sha
        and receipt.pr_number == checkpoint.pr_number
        and receipt.base_sha == checkpoint.base_sha
        and receipt.upstream_sha == checkpoint.upstream_sha
        and receipt.merge_sha == merge_sha
        and receipt.required_check == required_check
    )


def _resolve_merge_intent(
    config: AutonomousSyncConfig,
    checkpoint: PendingDeploymentCheckpoint,
    *,
    runner: CommandRunner,
    github: ProtectedRevertGitHubPort,
) -> PendingDeploymentCheckpoint:
    premerge_path, receipt = _load_trusted_sync_receipt(
        config,
        path_value=checkpoint.premerge_receipt_path,
        expected_digest=checkpoint.premerge_receipt_sha256,
    )
    if not _receipt_matches_checkpoint(
        receipt, checkpoint, required_check=config.required_check, merge_sha=None
    ):
        raise AutonomousSyncError("pending merge intent receipt identity changed")
    evidence = github.evidence(checkpoint.pr_number)
    exact = (
        evidence.number == checkpoint.pr_number
        and evidence.base_sha == checkpoint.base_sha
        and evidence.head_sha == checkpoint.pr_head_sha
        and evidence.required_check == receipt.required_check
        and evidence.required_check_conclusion == "success"
        and evidence.workflow_run_id == receipt.workflow_run_id
        and evidence.required_check_run_id == receipt.required_check_run_id
    )
    if not exact:
        raise AutonomousSyncError("pending merge intent GitHub authority changed")
    if evidence.state == "open" and evidence.merge_sha is None:
        bind_expected_base(github, checkpoint.base_sha)
        merge_sha = github.merge_exact(
            checkpoint.pr_number, expected_head=checkpoint.pr_head_sha
        )
    elif evidence.state == "merged" and evidence.merge_sha is not None:
        merge_sha = evidence.merge_sha
    else:
        raise AutonomousSyncError("pending merge intent state is ambiguous")
    fetched = runner.run(
        ["git", "fetch", "--no-tags", config.sync.origin, "main"],
        cwd=config.sync.repo,
        timeout=300,
    )
    if fetched.returncode != 0 or _git_sha(
        runner,
        config.sync.repo,
        f"refs/remotes/{config.sync.origin}/main",
    ) != merge_sha:
        raise AutonomousSyncError("pending merge intent fork main changed")
    final_receipt = finalize_sync_receipt(premerge_path, merge_sha=merge_sha)
    merged = replace(
        checkpoint,
        stage="merged_pending_deploy",
        merge_sha=merge_sha,
        final_receipt_path=str(final_receipt.path.resolve(strict=False)),
        final_receipt_sha256=final_receipt.sha256,
    )
    write_pending_deployment(config.receipt_root, merged)
    return merged


def _validate_pending_deploy(
    config: AutonomousSyncConfig,
    checkpoint: PendingDeploymentCheckpoint,
    *,
    runner: CommandRunner,
    github: SyncGitHubPort,
) -> tuple[SyncResult, Path, str]:
    if checkpoint.stage != "merged_pending_deploy":
        raise AutonomousSyncError("pending deployment stage is not deployable")
    expected_install = Path(config.deploy.install_root).resolve(strict=False)
    if Path(checkpoint.install_root).resolve(strict=False) != expected_install:
        raise AutonomousSyncError("pending deployment install scope changed")
    receipt_path, receipt = _load_trusted_sync_receipt(
        config,
        path_value=checkpoint.final_receipt_path,
        expected_digest=checkpoint.final_receipt_sha256,
    )
    if not _receipt_matches_checkpoint(
        receipt,
        checkpoint,
        required_check=config.required_check,
        merge_sha=checkpoint.merge_sha,
    ):
        raise AutonomousSyncError("pending deployment receipt identity changed")

    fetched = runner.run(
        ["git", "fetch", "--no-tags", config.sync.origin, "main"],
        cwd=config.sync.repo,
        timeout=300,
    )
    if fetched.returncode != 0:
        raise AutonomousSyncError("pending deployment fork main could not be fetched")
    fork_main = _git_sha(
        runner,
        config.sync.repo,
        f"refs/remotes/{config.sync.origin}/main",
    )
    if fork_main != checkpoint.merge_sha:
        raise AutonomousSyncError("pending deployment fork main identity changed")

    evidence = github.evidence(checkpoint.pr_number)
    exact_github = (
        evidence.number == checkpoint.pr_number
        and evidence.state == "merged"
        and evidence.base_sha == checkpoint.base_sha
        and evidence.head_sha == checkpoint.pr_head_sha
        and evidence.merge_sha == checkpoint.merge_sha
        and evidence.required_check == receipt.required_check
        and evidence.required_check_conclusion == "success"
        and evidence.workflow_run_id == receipt.workflow_run_id
        and evidence.required_check_run_id == receipt.required_check_run_id
    )
    if not exact_github:
        raise AutonomousSyncError("pending deployment GitHub authority changed")
    installed_sha = _git_sha(runner, config.deploy.install_root, "HEAD")
    if installed_sha not in {
        checkpoint.previous_installed_sha,
        checkpoint.merge_sha,
    }:
        raise AutonomousSyncError("pending deployment install identity changed")
    return (
        _candidate_from_pending_receipt(checkpoint, receipt),
        receipt_path,
        installed_sha,
    )


def _resume_pending_deploy(
    config: AutonomousSyncConfig,
    checkpoint: PendingDeploymentCheckpoint,
    *,
    runner: CommandRunner,
    github: ProtectedRevertGitHubPort,
    deploy_fn: Callable[[Path, str, int], DeploymentRecord],
    verify_runtime_fn: Callable[[str], bool] | None,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> AutonomousSyncResult:
    if checkpoint.stage == "failed_terminal":
        digest = deployment_checkpoint_sha256(checkpoint)
        return AutonomousSyncResult.needs_human(
            checkpoint.terminal_reason or "terminal deployment recovery failure",
            candidate_sha=checkpoint.candidate_sha,
            pr_number=checkpoint.pr_number,
            merge_sha=checkpoint.merge_sha,
            installed_sha=checkpoint.rollback_sha,
            reason_code=checkpoint.terminal_reason_code,
            failed_gate=checkpoint.terminal_failed_gate,
            rollback_state=checkpoint.rollback_state,
            rollback_sha=checkpoint.rollback_sha,
            revert_state=checkpoint.revert_state,
            revert_sha=checkpoint.revert_sha,
            details_artifact=f"deployment/pending-deployment-{digest}.json",
        )
    if checkpoint.stage == "merge_intent":
        checkpoint = _resolve_merge_intent(
            config, checkpoint, runner=runner, github=github
        )
    candidate, receipt_path, installed_sha = _validate_pending_deploy(
        config, checkpoint, runner=runner, github=github
    )
    checkpoint_sha = deployment_checkpoint_sha256(checkpoint)
    if installed_sha == checkpoint.merge_sha:
        if verify_runtime_fn is not None and verify_runtime_fn(checkpoint.merge_sha):
            clear_pending_deployment(config.receipt_root, sha256=checkpoint_sha)
            return AutonomousSyncResult(
                state=AutonomousSyncState.DEPLOYED,
                candidate_sha=checkpoint.candidate_sha,
                pr_number=checkpoint.pr_number,
                merge_sha=checkpoint.merge_sha,
                deployed_sha=checkpoint.merge_sha,
                fork_main_sha=checkpoint.merge_sha,
                installed_sha=checkpoint.merge_sha,
            )
    deployment = deploy_fn(
        receipt_path, checkpoint.merge_sha, checkpoint.pr_number
    )
    if deployment.status != "deployed" and verify_runtime_fn is None:
        raise AutonomousSyncError(
            "runtime health verifier is unavailable for recovery"
        )
    outcome = finish_or_recover(
        config,
        candidate,
        deployment,
        merge_sha=checkpoint.merge_sha,
        runner=runner,
        github=github,
        clock=clock,
        sleeper=sleeper,
        verify_runtime_fn=verify_runtime_fn or (lambda sha: False),
    )
    if outcome.state is AutonomousSyncState.DEPLOYED:
        clear_pending_deployment(config.receipt_root, sha256=checkpoint_sha)
    elif outcome.state is AutonomousSyncState.ROLLED_BACK_REVERTED:
        if not outcome.fork_main_sha or not outcome.installed_sha:
            raise AutonomousSyncError(
                "resumed deployment recovery identity is incomplete"
            )
        artifact = write_pending_reconstruction(
            config.receipt_root,
            PendingReconstructionCheckpoint(
                schema_version=2,
                repo_slug=config.sync.repo_slug,
                stage="recovered",
                failed_base_sha=checkpoint.base_sha,
                failed_upstream_sha=checkpoint.upstream_sha,
                failed_candidate_sha=checkpoint.candidate_sha,
                failed_candidate_tree_sha=checkpoint.candidate_tree_sha,
                failed_pr_number=checkpoint.pr_number,
                failed_merge_sha=checkpoint.merge_sha,
                revert_main_sha=outcome.fork_main_sha,
                previous_healthy_installed_sha=outcome.installed_sha,
                target_upstream_sha=checkpoint.upstream_sha,
                expected_rolling_candidate_sha=checkpoint.candidate_sha,
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
                reason="resumed deployment rolled back and protected revert completed",
                resume_attempts=0,
            ),
        )
        outcome = replace(
            outcome,
            details_artifact=(
                f"reconstruction/pending-reconstruction-{artifact.sha256}.json"
            ),
        )
    elif outcome.state is AutonomousSyncState.NEEDS_OLE:
        terminal = terminalize_pending_deployment(
            config.receipt_root,
            checkpoint,
            reason=outcome.reason or "automatic recovery failed",
            reason_code=outcome.reason_code or "AUTOMATIC_RECOVERY_FAILED",
            failed_gate=outcome.failed_gate or "protected_recovery",
            rollback_state=outcome.rollback_state or deployment.status,
            rollback_sha=outcome.rollback_sha or deployment.previous_sha,
            revert_state=outcome.revert_state or "NEEDS_OLE",
            revert_sha=outcome.revert_sha,
        )
        outcome = replace(
            outcome,
            details_artifact=(
                f"deployment/pending-deployment-{terminal.sha256}.json"
            ),
        )
    return outcome


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
                reason_code="DEPLOYMENT_RECORD_MISMATCH",
                failed_gate="deployment_record",
                affected_files=(
                    candidate.conflicted_files or candidate.changed_files
                ),
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
            affected_files=(candidate.conflicted_files or candidate.changed_files),
            rollback_state=deployment.status,
            rollback_sha=deployment.previous_sha,
            revert_state=recovery.state.value,
            revert_sha=recovery.revert_merge_sha,
        )
    return AutonomousSyncResult.needs_human(
        recovery.reason or "automatic recovery failed",
        candidate_sha=candidate.candidate_sha,
        pr_number=candidate.pr_number,
        merge_sha=merge_sha,
        installed_sha=recovery.installed_sha,
        reason_code="AUTOMATIC_RECOVERY_FAILED",
        failed_gate="protected_recovery",
        affected_files=(candidate.conflicted_files or candidate.changed_files),
        rollback_state=deployment.status,
        rollback_sha=deployment.previous_sha,
        revert_state=recovery.state.value,
        revert_sha=recovery.revert_merge_sha or recovery.revert_head_sha,
    )


_ERROR_REASONS: tuple[tuple[type[Exception], str, str, str], ...] = (
    (ConflictReviewError, "conflict review evidence is invalid", "CONFLICT_REVIEW_INVALID", "conflict_review"),
    (ResolutionRecordError, "conflict resolution evidence is invalid", "CONFLICT_RESOLUTION_INVALID", "conflict_resolution"),
    (SyncReceiptError, "sync eligibility evidence is invalid", "SYNC_RECEIPT_INVALID", "sync_eligibility"),
    (SyncGitHubError, "protected GitHub evidence is invalid", "GITHUB_AUTHORITY_INVALID", "github_authority"),
    (PreflightError, "automated deployment authority failed", "DEPLOYMENT_AUTHORITY_FAILED", "deployment_authority"),
    (SyncRemediationError, "bounded sync remediation failed", "SYNC_REMEDIATION_FAILED", "sync_remediation"),
    (ReconstructionError, "post-rollback candidate reconstruction failed", "RECONSTRUCTION_FAILED", "reconstruction"),
    (ReconstructionCheckpointError, "pending reconstruction evidence is invalid", "RECONSTRUCTION_CHECKPOINT_INVALID", "reconstruction_checkpoint"),
    (SyncDeploymentCheckpointError, "pending deployment evidence is invalid", "DEPLOYMENT_CHECKPOINT_INVALID", "deployment_checkpoint"),
)


def _error_result(
    error: Exception,
    state: ControllerRunState,
) -> AutonomousSyncResult:
    candidate = state.candidate
    if isinstance(error, AutonomousSyncError):
        reason = str(error)
        reason_code = "AUTONOMOUS_GATE_FAILED"
        failed_gate = "controller"
    else:
        reason = "unexpected autonomous sync failure"
        reason_code = "UNEXPECTED_AUTONOMOUS_FAILURE"
        failed_gate = "controller"
        for error_type, message, code, gate in _ERROR_REASONS:
            if isinstance(error, error_type):
                reason = message
                reason_code = code
                failed_gate = gate
                break
        if reason == "unexpected autonomous sync failure":
            _log_unexpected("Autonomous sync failed unexpectedly", error)
    return AutonomousSyncResult.needs_human(
        reason,
        candidate_sha=candidate.candidate_sha if candidate else None,
        pr_number=candidate.pr_number if candidate else None,
        merge_sha=state.merge_sha,
        reason_code=reason_code,
        failed_gate=failed_gate,
        affected_files=(
            candidate.conflicted_files or candidate.changed_files
            if candidate
            else ()
        ),
        details_artifact=(
            f"reconstruction/pending-reconstruction-"
            f"{state.reconstruction.checkpoint_sha256}.json"
            if state.reconstruction is not None
            else (
                f"deployment/pending-deployment-"
                f"{state.deployment_checkpoint_sha256}.json"
                if state.deployment_checkpoint_sha256 is not None
                else None
            )
        ),
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
    """Converge one exact candidate through explicit operations stages."""
    from .sync_controller_execution import (
        ControllerDependencies,
        ControllerExecution,
    )

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

        execution = ControllerExecution(
            config,
            ControllerDependencies(
                runner=runner,
                github=github,
                resolver=resolver,
                reviewer=reviewer,
                remediator=remediator,
                deploy_fn=deploy_fn,
                verify_runtime_fn=verify_runtime_fn,
                clock=clock,
                sleeper=sleeper,
            ),
        )
        try:
            result = execution.execute()
        except _OutcomePublicationError as exc:
            raise exc.cause
        except Exception as exc:
            result = _error_result(exc, execution.state)
        try:
            return finish(result)
        except _OutcomePublicationError as exc:
            raise exc.cause
