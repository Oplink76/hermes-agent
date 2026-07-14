"""Explicit stage operations for the autonomous upstream-sync controller."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import sync_controller as controller
from .command import CommandRunner
from .deploy import DeploymentRecord
from .sync import (
    ConflictResolver,
    SyncResult,
    SyncState,
    prepare_candidate,
)
from .sync_controller import (
    AutonomousSyncConfig,
    AutonomousSyncError,
    AutonomousSyncResult,
    AutonomousSyncState,
    ControllerRunState,
    ControllerStage,
    ReconstructionRunContext,
    _checkpoint_repaired,
    _checkpoint_reconstructed,
    _current_upstream_sha,
    _failed_from_checkpoint,
    _reconstructed_from_checkpoint,
    _refresh_current_upstream_sha,
    _repaired_from_checkpoint,
    _resume_pending_deploy,
    _review_candidate,
    _upstream_is_current,
    _valid_repair,
    attest_candidate,
    finish_or_recover,
    wait_for_green_exact_head,
)
from .sync_deployment_checkpoint import (
    clear_pending_deployment,
    load_pending_deployment,
    terminalize_pending_deployment,
)
from .sync_github import bind_expected_base
from .sync_poll import RequiredCheckRedError
from .sync_receipt import finalize_sync_receipt
from .sync_reconstruction import resume_failed_candidate_reconstruction
from .sync_reconstruction_checkpoint import (
    MAX_RESUME_ATTEMPTS,
    PendingReconstructionCheckpoint,
    clear_pending_reconstruction,
    load_pending_reconstruction,
    write_pending_reconstruction,
)
from .sync_recovery import ProtectedRevertGitHubPort, is_quarantined
from .sync_remediation import SyncRemediationPort
from .sync_review import IndependentConflictReviewer


@dataclass(frozen=True)
class ControllerDependencies:
    runner: CommandRunner
    github: ProtectedRevertGitHubPort
    resolver: ConflictResolver | None
    reviewer: IndependentConflictReviewer | None
    remediator: SyncRemediationPort | None
    deploy_fn: Callable[[Path, str, int], DeploymentRecord]
    verify_runtime_fn: Callable[[str], bool] | None
    clock: Callable[[], float]
    sleeper: Callable[[float], None]


class ControllerExecution:
    """Advance one typed controller state through explicit safety stages."""

    def __init__(
        self,
        config: AutonomousSyncConfig,
        dependencies: ControllerDependencies,
    ):
        self.config = config
        self.deps = dependencies
        self.state = ControllerRunState()

    @property
    def candidate(self) -> SyncResult:
        candidate = self.state.candidate
        if candidate is None:
            raise AutonomousSyncError("controller candidate evidence is unavailable")
        return candidate

    def execute(self) -> AutonomousSyncResult:
        resumed = self._resume_pending_deployment()
        if resumed is not None:
            return resumed
        initial = self._start_candidate_stage()
        if initial is not None:
            return initial
        while True:
            result = self._advance_candidate_stage()
            if result is not None:
                return result

    def _resume_pending_deployment(self) -> AutonomousSyncResult | None:
        reconstruction = load_pending_reconstruction(
            self.config.receipt_root,
            repo_slug=self.config.sync.repo_slug,
        )
        pending = load_pending_deployment(
            self.config.receipt_root,
            repo_slug=self.config.sync.repo_slug,
        )
        if pending is None or reconstruction is not None:
            return None
        return _resume_pending_deploy(
            self.config,
            pending,
            runner=self.deps.runner,
            github=self.deps.github,
            deploy_fn=self.deps.deploy_fn,
            verify_runtime_fn=self.deps.verify_runtime_fn,
            clock=self.deps.clock,
            sleeper=self.deps.sleeper,
        )

    def _start_candidate_stage(self) -> AutonomousSyncResult | None:
        pending = load_pending_reconstruction(
            self.config.receipt_root,
            repo_slug=self.config.sync.repo_slug,
        )
        if pending is not None:
            self._resume_reconstruction(pending)
            return None
        candidate = controller.prepare_candidate(
            self.config.sync,
            github=self.deps.github,
            runner=self.deps.runner,
            resolver=self.deps.resolver,
        )
        if candidate.state is SyncState.NO_CHANGE:
            return AutonomousSyncResult.no_change(candidate)
        self.state = ControllerRunState(
            stage=ControllerStage.CANDIDATE,
            candidate=candidate,
        )
        return None

    def _validate_reconstruction_resume(
        self, checkpoint: PendingReconstructionCheckpoint
    ) -> None:
        if checkpoint.resume_attempts >= MAX_RESUME_ATTEMPTS:
            raise AutonomousSyncError(
                "pending reconstruction retry budget is exhausted"
            )
        if self.deps.verify_runtime_fn is None:
            raise AutonomousSyncError(
                "pending reconstruction dependencies are unavailable"
            )
        if checkpoint.stage != "repaired" and self.deps.remediator is None:
            raise AutonomousSyncError(
                "pending reconstruction remediator is unavailable"
            )
        if not self.deps.verify_runtime_fn(
            checkpoint.previous_healthy_installed_sha
        ):
            raise AutonomousSyncError(
                "pending reconstruction previous install is not healthy"
            )

    def _write_reconstruction(
        self,
        checkpoint: PendingReconstructionCheckpoint,
        *,
        source: SyncResult,
    ) -> ReconstructionRunContext:
        artifact = write_pending_reconstruction(
            self.config.receipt_root, checkpoint
        )
        return ReconstructionRunContext(
            source=source,
            failed_merge_sha=checkpoint.failed_merge_sha,
            revert_main_sha=checkpoint.revert_main_sha,
            installed_sha=checkpoint.previous_healthy_installed_sha,
            checkpoint=checkpoint,
            checkpoint_sha256=artifact.sha256,
        )

    def _resume_reconstruction_candidate(
        self,
        checkpoint: PendingReconstructionCheckpoint,
        source: SyncResult,
    ) -> tuple[PendingReconstructionCheckpoint, SyncResult | None]:
        if checkpoint.stage == "repaired":
            return checkpoint, _repaired_from_checkpoint(self.config, checkpoint)
        if checkpoint.stage == "reconstructed":
            return checkpoint, _reconstructed_from_checkpoint(checkpoint)
        current_upstream = _refresh_current_upstream_sha(
            self.config, self.deps.runner
        )
        if current_upstream != checkpoint.target_upstream_sha:
            checkpoint = replace(
                checkpoint,
                target_upstream_sha=current_upstream,
                reason=(
                    "official upstream advanced while recovery was pending; "
                    "complete-tree reconstruction refreshed"
                ),
            )
            write_pending_reconstruction(self.config.receipt_root, checkpoint)
        refreshed = controller.resume_failed_candidate_reconstruction(
            self.config.sync,
            failed=source,
            failed_merge_sha=checkpoint.failed_merge_sha,
            revert_main_sha=checkpoint.revert_main_sha,
            expected_candidate_sha=checkpoint.expected_rolling_candidate_sha,
            current_upstream_sha=checkpoint.target_upstream_sha,
            github=self.deps.github,
            runner=self.deps.runner,
        )
        return _checkpoint_reconstructed(checkpoint, refreshed), refreshed

    def _repair_reconstruction_candidate(
        self,
        checkpoint: PendingReconstructionCheckpoint,
        candidate: SyncResult,
    ) -> tuple[PendingReconstructionCheckpoint, SyncResult]:
        if checkpoint.stage == "repaired":
            return checkpoint, candidate
        remediator = self.deps.remediator
        if remediator is None:
            raise AutonomousSyncError(
                "pending reconstruction remediator is unavailable"
            )
        repaired = _valid_repair(
            candidate,
            remediator.repair_candidate(
                candidate,
                health_evidence=(checkpoint.reason,),
            ),
            runner=self.deps.runner,
            repo=self.config.sync.repo,
        )
        return _checkpoint_repaired(self.config, checkpoint, repaired)

    def _resume_reconstruction(
        self, checkpoint: PendingReconstructionCheckpoint
    ) -> None:
        self._validate_reconstruction_resume(checkpoint)
        checkpoint = replace(
            checkpoint,
            resume_attempts=checkpoint.resume_attempts + 1,
        )
        source = _failed_from_checkpoint(checkpoint)
        write_pending_reconstruction(self.config.receipt_root, checkpoint)
        checkpoint, candidate = self._resume_reconstruction_candidate(
            checkpoint, source
        )
        if candidate is None:
            raise AutonomousSyncError("pending reconstruction candidate is missing")
        checkpoint, candidate = self._repair_reconstruction_candidate(
            checkpoint, candidate
        )
        context = self._write_reconstruction(checkpoint, source=source)
        self.state = ControllerRunState(
            stage=ControllerStage.CANDIDATE,
            candidate=candidate,
            merge_sha=context.failed_merge_sha,
            candidate_repair_used=True,
            reconstruction=context,
        )

    def _poll_candidate(
        self, reviewed: SyncResult
    ) -> AutonomousSyncResult | object | None:
        try:
            return wait_for_green_exact_head(
                self.deps.github,
                reviewed,
                required_check=self.config.required_check,
                timeout_seconds=self.config.check_timeout_seconds,
                poll_interval_seconds=self.config.poll_interval_seconds,
                clock=self.deps.clock,
                sleeper=self.deps.sleeper,
            )
        except RequiredCheckRedError as failure:
            return self._handle_required_check_failure(reviewed, failure)

    def _handle_required_check_failure(
        self,
        reviewed: SyncResult,
        failure: RequiredCheckRedError,
    ) -> AutonomousSyncResult | None:
        conclusion = failure.evidence.required_check_conclusion
        if conclusion != "failure":
            return AutonomousSyncResult.pending(
                reviewed,
                reason=(
                    "exact required check ended "
                    f"{conclusion}; awaiting a new exact run"
                ),
            )
        remediator = self.deps.remediator
        if remediator is None:
            raise AutonomousSyncError(str(failure)) from failure
        if not self.state.infrastructure_retry_used and remediator.retry_infrastructure(
            reviewed, failure.evidence
        ):
            self.state = replace(self.state, infrastructure_retry_used=True)
            return None
        if self.state.candidate_repair_used:
            raise AutonomousSyncError(
                "bounded candidate repair budget is exhausted"
            )
        repaired = _valid_repair(
            reviewed,
            remediator.repair_candidate(
                reviewed,
                health_evidence=(
                    f"required_check:{failure.evidence.required_check}",
                    "required_check_conclusion:failure",
                    f"workflow_run_id:{failure.evidence.workflow_run_id}",
                    "required_check_run_id:"
                    f"{failure.evidence.required_check_run_id}",
                    f"head_sha:{failure.evidence.head_sha}",
                ),
            ),
            runner=self.deps.runner,
            repo=self.config.sync.repo,
        )
        self.state = replace(
            self.state,
            candidate=repaired,
            candidate_repair_used=True,
        )
        return None

    def _pending_reconstruction_refresh(
        self,
        reviewed: SyncResult,
    ) -> AutonomousSyncResult:
        context = self.state.reconstruction
        if context is None or reviewed.candidate_sha is None:
            raise AutonomousSyncError(
                "post-revert reconstruction context is incomplete"
            )
        reason = (
            "official upstream advanced during post-revert repair; "
            "complete-tree reconstruction must restart"
        )
        pending = replace(
            context.checkpoint,
            stage="recovered",
            target_upstream_sha=controller._current_upstream_sha(
                self.config, self.deps.runner
            ),
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
        self._write_reconstruction(pending, source=context.source)
        return AutonomousSyncResult.pending(reviewed, reason=reason)

    def _refresh_stale_candidate(
        self,
        reviewed: SyncResult,
    ) -> AutonomousSyncResult | None:
        if self.state.post_rollback_repair:
            return self._pending_reconstruction_refresh(reviewed)
        if self.state.upstream_refreshes >= self.config.max_upstream_refreshes:
            return AutonomousSyncResult.refresh_required(reviewed)
        previous_head = self.candidate.candidate_sha
        refreshed = controller.prepare_candidate(
            self.config.sync,
            github=self.deps.github,
            runner=self.deps.runner,
            resolver=self.deps.resolver,
        )
        if refreshed.state is SyncState.NO_CHANGE:
            return AutonomousSyncResult.no_change(refreshed)
        changed = refreshed.candidate_sha != previous_head
        self.state = replace(
            self.state,
            candidate=refreshed,
            upstream_refreshes=self.state.upstream_refreshes + 1,
            infrastructure_retry_used=(
                False if changed else self.state.infrastructure_retry_used
            ),
            candidate_repair_used=(
                False if changed else self.state.candidate_repair_used
            ),
            reconstruction=None,
        )
        return None

    def _deploy_reviewed_candidate(
        self,
        reviewed: SyncResult,
        evidence,
        receipt,
    ) -> tuple[AutonomousSyncResult, DeploymentRecord]:
        intent = controller._write_merge_intent(
            self.config,
            candidate=reviewed,
            evidence=evidence,
            premerge_receipt=receipt,
            runner=self.deps.runner,
        )
        self.state = replace(
            self.state,
            stage=ControllerStage.MERGE_INTENT,
            candidate=reviewed,
            merge_sha=None,
            deployment_checkpoint_sha256=(
                controller.deployment_checkpoint_sha256(intent)
            ),
        )
        bind_expected_base(self.deps.github, evidence.base_sha)
        merge_sha = self.deps.github.merge_exact(
            evidence.number, expected_head=reviewed.candidate_sha
        )
        final_receipt = controller.finalize_sync_receipt(
            receipt.path, merge_sha=merge_sha
        )
        checkpoint, checkpoint_sha = controller._advance_pending_deploy(
            self.config,
            intent,
            merge_sha=merge_sha,
            final_receipt=final_receipt,
        )
        self.state = replace(
            self.state,
            stage=ControllerStage.MERGED,
            candidate=reviewed,
            merge_sha=merge_sha,
            deployment_checkpoint_sha256=checkpoint_sha,
        )
        deployment = self.deps.deploy_fn(
            final_receipt.path, merge_sha, evidence.number
        )
        if deployment.status != "deployed" and self.deps.verify_runtime_fn is None:
            raise AutonomousSyncError(
                "runtime health verifier is unavailable for recovery"
            )
        outcome = controller.finish_or_recover(
            self.config,
            reviewed,
            deployment,
            merge_sha=merge_sha,
            runner=self.deps.runner,
            github=self.deps.github,
            clock=self.deps.clock,
            sleeper=self.deps.sleeper,
            verify_runtime_fn=self.deps.verify_runtime_fn or (lambda sha: False),
        )
        if outcome.state is AutonomousSyncState.NEEDS_OLE:
            terminal = terminalize_pending_deployment(
                self.config.receipt_root,
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
        return outcome, deployment

    def _clear_healthy_deployment(self) -> None:
        reconstruction = self.state.reconstruction
        if reconstruction is not None:
            clear_pending_reconstruction(
                self.config.receipt_root,
                sha256=reconstruction.checkpoint_sha256,
            )
        checkpoint_sha = self.state.deployment_checkpoint_sha256
        if checkpoint_sha is not None:
            clear_pending_deployment(
                self.config.receipt_root,
                sha256=checkpoint_sha,
            )

    def _new_recovery_context(
        self,
        reviewed: SyncResult,
        outcome: AutonomousSyncResult,
    ) -> ReconstructionRunContext:
        if not outcome.fork_main_sha:
            raise AutonomousSyncError(
                "protected rollback did not report reconstructed base"
            )
        if not outcome.installed_sha:
            raise AutonomousSyncError(
                "protected rollback did not report healthy install identity"
            )
        merge_sha = self.state.merge_sha
        if merge_sha is None:
            raise AutonomousSyncError("protected rollback merge identity is missing")
        checkpoint = PendingReconstructionCheckpoint(
            schema_version=2,
            repo_slug=self.config.sync.repo_slug,
            stage="recovered",
            failed_base_sha=reviewed.base_sha or "",
            failed_upstream_sha=reviewed.upstream_sha or "",
            failed_candidate_sha=reviewed.candidate_sha or "",
            failed_candidate_tree_sha=reviewed.candidate_tree_sha or "",
            failed_pr_number=reviewed.pr_number or 0,
            failed_merge_sha=merge_sha,
            revert_main_sha=outcome.fork_main_sha,
            previous_healthy_installed_sha=outcome.installed_sha,
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
        return self._write_reconstruction(checkpoint, source=reviewed)

    def _reconstruct_after_rollback(
        self,
        context: ReconstructionRunContext,
    ) -> tuple[ReconstructionRunContext, SyncResult]:
        checkpoint = context.checkpoint
        current_upstream = _refresh_current_upstream_sha(
            self.config, self.deps.runner
        )
        if current_upstream != checkpoint.target_upstream_sha:
            checkpoint = replace(
                checkpoint,
                target_upstream_sha=current_upstream,
                reason=(
                    "official upstream advanced after protected recovery; "
                    "complete-tree reconstruction refreshed"
                ),
            )
            context = self._write_reconstruction(
                checkpoint, source=context.source
            )
        refreshed = controller.resume_failed_candidate_reconstruction(
            self.config.sync,
            failed=context.source,
            failed_merge_sha=context.failed_merge_sha,
            revert_main_sha=context.revert_main_sha,
            expected_candidate_sha=checkpoint.expected_rolling_candidate_sha,
            current_upstream_sha=checkpoint.target_upstream_sha,
            github=self.deps.github,
            runner=self.deps.runner,
        )
        if refreshed.state is not SyncState.PR_UPDATED:
            raise AutonomousSyncError(
                "post-rollback candidate refresh did not produce a repair target"
            )
        checkpoint = _checkpoint_reconstructed(checkpoint, refreshed)
        return self._write_reconstruction(
            checkpoint, source=context.source
        ), refreshed

    def _repair_after_rollback(
        self,
        context: ReconstructionRunContext,
        refreshed: SyncResult,
        deployment: DeploymentRecord,
    ) -> None:
        remediator = self.deps.remediator
        if remediator is None:
            raise AutonomousSyncError("bounded sync remediator is unavailable")
        health_evidence = tuple(
            f"{check.name}:{'passed' if check.passed else 'failed'}"
            for check in deployment.checks
        ) or (f"deployment:{deployment.status}",)
        repaired = _valid_repair(
            refreshed,
            remediator.repair_candidate(
                refreshed, health_evidence=health_evidence
            ),
            runner=self.deps.runner,
            repo=self.config.sync.repo,
        )
        checkpoint, repaired = _checkpoint_repaired(
            self.config, context.checkpoint, repaired
        )
        context = self._write_reconstruction(
            checkpoint, source=context.source
        )
        self.state = ControllerRunState(
            stage=ControllerStage.CANDIDATE,
            candidate=repaired,
            merge_sha=self.state.merge_sha,
            infrastructure_retry_used=self.state.infrastructure_retry_used,
            candidate_repair_used=True,
            upstream_refreshes=self.state.upstream_refreshes,
            reconstruction=context,
        )

    def _handle_deployment_outcome(
        self,
        reviewed: SyncResult,
        deployment: DeploymentRecord,
        outcome: AutonomousSyncResult,
    ) -> AutonomousSyncResult | None:
        if outcome.state is AutonomousSyncState.DEPLOYED:
            self._clear_healthy_deployment()
            return outcome
        if outcome.state is not AutonomousSyncState.ROLLED_BACK_REVERTED:
            return outcome
        context = self._new_recovery_context(reviewed, outcome)
        self.state = replace(
            self.state,
            stage=ControllerStage.RECOVERY,
            reconstruction=context,
        )
        if self.state.candidate_repair_used or self.deps.remediator is None:
            return AutonomousSyncResult.needs_human(
                "protected rollback completed; bounded repair is exhausted",
                candidate_sha=reviewed.candidate_sha,
                pr_number=reviewed.pr_number,
                merge_sha=self.state.merge_sha,
                installed_sha=outcome.installed_sha,
            )
        context, refreshed = self._reconstruct_after_rollback(context)
        self._repair_after_rollback(context, refreshed, deployment)
        return None

    def _advance_candidate_stage(self) -> AutonomousSyncResult | None:
        candidate = self.candidate
        if candidate.state is not SyncState.PR_UPDATED:
            raise AutonomousSyncError(
                f"candidate preparation stopped in {candidate.state.value}"
            )
        if not self.state.post_rollback_repair and is_quarantined(
            self.config.quarantine_root, candidate
        ):
            raise AutonomousSyncError("unchanged failed candidate is quarantined")
        reviewed, conflict_review = _review_candidate(
            self.config, candidate, self.deps.reviewer, self.deps.runner
        )
        polled = self._poll_candidate(reviewed)
        if polled is None or isinstance(polled, AutonomousSyncResult):
            return polled
        evidence = polled
        receipt = attest_candidate(
            self.config,
            reviewed,
            evidence,
            conflict_review=conflict_review,
        )
        if not controller._upstream_is_current(
            self.config, reviewed, self.deps.runner
        ):
            return self._refresh_stale_candidate(reviewed)
        outcome, deployment = self._deploy_reviewed_candidate(
            reviewed, evidence, receipt
        )
        return self._handle_deployment_outcome(reviewed, deployment, outcome)
