"""Single-lock controller for autonomous protected Hermes fork sync."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from .command import CommandRunner
from .deploy import DeployConfig, DeploymentRecord
from .locking import try_exclusive_file_lock
from .sync import (
    ConflictResolver,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
    prepare_candidate,
)
from .sync_github import SyncGitHubPort, SyncPullRequestEvidence
from .sync_receipt import (
    SyncReceiptArtifact,
    finalize_sync_receipt,
    write_sync_receipt,
)
from .sync_recovery import (
    ProtectedRevertGitHubPort,
    ProtectedRevertState,
    is_quarantined,
    run_protected_revert,
)
from .sync_review import (
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


@dataclass(frozen=True)
class AutonomousSyncResult:
    state: AutonomousSyncState
    candidate_sha: str | None = None
    merge_sha: str | None = None
    deployed_sha: str | None = None
    fork_main_sha: str | None = None
    installed_sha: str | None = None
    needs_ole: bool = False
    reason: str | None = None

    @classmethod
    def locked(cls) -> "AutonomousSyncResult":
        return cls(state=AutonomousSyncState.LOCKED, reason="sync lock held")

    @classmethod
    def no_change(cls, candidate: SyncResult) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.NO_CHANGE,
            candidate_sha=candidate.candidate_sha,
        )

    @classmethod
    def needs_human(
        cls,
        reason: str,
        *,
        candidate_sha: str | None = None,
        merge_sha: str | None = None,
        installed_sha: str | None = None,
    ) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.NEEDS_OLE,
            candidate_sha=candidate_sha,
            merge_sha=merge_sha,
            installed_sha=installed_sha,
            needs_ole=True,
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

    def __post_init__(self) -> None:
        if self.check_timeout_seconds <= 0 or self.poll_interval_seconds <= 0:
            raise ValueError("sync polling settings must be positive")
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
    deadline = clock() + timeout_seconds
    max_polls = math.ceil(timeout_seconds / poll_interval_seconds) + 1
    transient_failures = 0
    for poll in range(max_polls):
        try:
            evidence = github.evidence(candidate.pr_number)
        except Exception as exc:
            transient_failures += 1
            if transient_failures > 1:
                raise AutonomousSyncError(
                    f"GitHub evidence failed repeatedly: {exc}"
                ) from exc
            sleeper(poll_interval_seconds)
            continue
        if evidence.number != candidate.pr_number:
            raise AutonomousSyncError("pull request number changed")
        if evidence.state != "open":
            raise AutonomousSyncError("pull request is not open")
        if evidence.base_sha != candidate.base_sha:
            raise AutonomousSyncError("pull request base changed")
        if evidence.head_sha != candidate.candidate_sha:
            raise AutonomousSyncError("pull request head changed")
        if evidence.required_check != required_check:
            raise AutonomousSyncError("required check identity changed")
        conclusion = evidence.required_check_conclusion.lower()
        if conclusion == "success":
            return evidence
        if conclusion not in {"pending", "queued", "in_progress"}:
            raise AutonomousSyncError("required check is not green")
        if clock() >= deadline or poll + 1 == max_polls:
            raise AutonomousSyncError("required check timed out")
        sleeper(poll_interval_seconds)
    raise AutonomousSyncError("required check timed out")


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


def _bind_expected_base(github: SyncGitHubPort, base_sha: str) -> None:
    if hasattr(github, "expected_base_sha"):
        setattr(github, "expected_base_sha", base_sha)


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
) -> AutonomousSyncResult:
    if deployment.status == "deployed":
        if deployment.requested_sha != merge_sha:
            return AutonomousSyncResult.needs_human(
                "deployment record does not match exact merge SHA",
                candidate_sha=candidate.candidate_sha,
                merge_sha=merge_sha,
            )
        return AutonomousSyncResult(
            state=AutonomousSyncState.DEPLOYED,
            candidate_sha=candidate.candidate_sha,
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
    )
    if recovery.state is ProtectedRevertState.REVERTED:
        return AutonomousSyncResult(
            state=AutonomousSyncState.ROLLED_BACK_REVERTED,
            candidate_sha=candidate.candidate_sha,
            merge_sha=merge_sha,
            fork_main_sha=recovery.revert_merge_sha,
            installed_sha=recovery.installed_sha,
            reason="runtime rolled back and protected revert merged",
        )
    return AutonomousSyncResult.needs_human(
        recovery.reason or "automatic recovery failed",
        candidate_sha=candidate.candidate_sha,
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
    deploy_fn: Callable[[Path, str, int], DeploymentRecord],
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> AutonomousSyncResult:
    """Converge one exact candidate or return one bounded terminal state."""
    with try_exclusive_file_lock(config.sync.lock_path) as acquired:
        if not acquired:
            return AutonomousSyncResult.locked()
        candidate: SyncResult | None = None
        merge_sha: str | None = None
        try:
            candidate = prepare_candidate(
                config.sync,
                github=github,
                runner=runner,
                resolver=resolver,
            )
            if candidate.state is SyncState.NO_CHANGE:
                return AutonomousSyncResult.no_change(candidate)
            if candidate.state is not SyncState.PR_UPDATED:
                raise AutonomousSyncError(
                    f"candidate preparation stopped in {candidate.state.value}"
                )
            if is_quarantined(config.quarantine_root, candidate):
                raise AutonomousSyncError("unchanged failed candidate is quarantined")
            reviewed, conflict_review = require_conflict_review(
                candidate,
                reviewer=reviewer,
                worktree=config.sync.worktree,
                resolution_record=candidate.resolution_record
                or config.resolution_record,
                resolver_backend=config.resolver_backend,
            )
            evidence = wait_for_green_exact_head(
                github,
                reviewed,
                required_check=config.required_check,
                timeout_seconds=config.check_timeout_seconds,
                poll_interval_seconds=config.poll_interval_seconds,
                clock=clock,
                sleeper=sleeper,
            )
            receipt = attest_candidate(
                config,
                reviewed,
                evidence,
                conflict_review=conflict_review,
            )
            _bind_expected_base(github, evidence.base_sha)
            merge_sha = github.merge_exact(
                evidence.number, expected_head=reviewed.candidate_sha
            )
            final_receipt = finalize_sync_receipt(receipt.path, merge_sha=merge_sha)
            deployment = deploy_fn(final_receipt.path, merge_sha, evidence.number)
            return finish_or_recover(
                config,
                reviewed,
                deployment,
                merge_sha=merge_sha,
                runner=runner,
                github=github,
                clock=clock,
                sleeper=sleeper,
            )
        except Exception as exc:
            return AutonomousSyncResult.needs_human(
                str(exc),
                candidate_sha=candidate.candidate_sha if candidate else None,
                merge_sha=merge_sha,
            )
