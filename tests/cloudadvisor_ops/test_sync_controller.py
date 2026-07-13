from __future__ import annotations

import subprocess
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.deploy import (
    DeployConfig,
    DeploymentRecord,
    PreflightError,
)
from ops.cloudadvisor.hermes_ops.locking import try_exclusive_file_lock
from ops.cloudadvisor.hermes_ops.health import HealthCheck
from ops.cloudadvisor.hermes_ops.sync import (
    CheckResult,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
)
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncConfig,
    AutonomousSyncResult,
    AutonomousSyncState,
    ControllerRunState,
    ControllerStage,
    run_autonomous_sync,
)
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_deployment_checkpoint import (
    load_pending_deployment,
)
from ops.cloudadvisor.hermes_ops.sync_reconstruction_checkpoint import (
    PendingReconstructionCheckpoint,
    load_pending_reconstruction,
    write_pending_reconstruction,
)
from ops.cloudadvisor.hermes_ops.sync_reconstruction import ReconstructionError
from ops.cloudadvisor.hermes_ops.sync_review import ConflictReviewReceipt
from ops.cloudadvisor.hermes_ops.sync_receipt import SyncReceiptArtifact


SHA_BASE = "1" * 40
SHA_UPSTREAM = "2" * 40
SHA_CANDIDATE = "3" * 40
SHA_MERGE = "4" * 40
SHA_CANDIDATE_TREE = "5" * 40
SHA_REPAIRED = "6" * 40
SHA_REPAIRED_TREE = "7" * 40
SHA_NEW_CANDIDATE = "8" * 40
SHA_NEW_CANDIDATE_TREE = "9" * 40
SHA_NEW_REPAIRED = "a" * 40
SHA_NEW_REPAIRED_TREE = "b" * 40


def test_controller_result_helpers_preserve_candidate_pr_number() -> None:
    candidate = SyncResult(
        state=SyncState.PR_UPDATED,
        candidate_sha=SHA_CANDIDATE,
        pr_number=7,
    )

    assert AutonomousSyncResult.no_change(candidate).pr_number == 7
    assert AutonomousSyncResult.pending(candidate, reason="waiting").pr_number == 7
    assert AutonomousSyncResult.refresh_required(candidate).pr_number == 7


def test_needs_ole_result_carries_only_structured_failure_evidence() -> None:
    result = AutonomousSyncResult.needs_human(
        "safe summary",
        candidate_sha=SHA_CANDIDATE,
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
        affected_files=("ops/sync.py",),
        rollback_state="rolled_back_healthy",
        rollback_sha=SHA_BASE,
        revert_state="NEEDS_OLE",
        revert_sha=SHA_MERGE,
        details_artifact="deployment/failed-abc.json",
    )

    assert result.reason_code == "GITHUB_AUTHORITY_INVALID"
    assert result.failed_gate == "github_authority"
    assert result.affected_files == ("ops/sync.py",)
    assert result.rollback_sha == SHA_BASE
    assert result.revert_sha == SHA_MERGE
    assert result.details_artifact == "deployment/failed-abc.json"


def test_controller_run_state_rejects_stage_without_required_evidence() -> None:
    with pytest.raises(ValueError, match="candidate"):
        ControllerRunState(stage=ControllerStage.CANDIDATE)

    with pytest.raises(ValueError, match="merge"):
        ControllerRunState(
            stage=ControllerStage.MERGED,
            candidate=candidate(),
        )

    with pytest.raises(ValueError, match="initial"):
        ControllerRunState(candidate=candidate())

    with pytest.raises(ValueError, match="checkpoint"):
        ControllerRunState(
            stage=ControllerStage.MERGED,
            candidate=candidate(),
            merge_sha=SHA_MERGE,
        )


class Runner:
    def __init__(self, repair_paths: tuple[str, ...] = ("upstream.txt",)):
        self.review_head = SHA_CANDIDATE
        self.repair_paths = repair_paths

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        if argv == ["git", "rev-parse", "upstream/main"]:
            return subprocess.CompletedProcess(argv, 0, SHA_UPSTREAM + "\n", "")
        if argv == ["git", "merge-base", "--is-ancestor", SHA_CANDIDATE, SHA_REPAIRED]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv == [
            "git",
            "merge-base",
            "--is-ancestor",
            SHA_NEW_CANDIDATE,
            SHA_NEW_REPAIRED,
        ]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv == ["git", "rev-parse", f"{SHA_REPAIRED}^"]:
            return subprocess.CompletedProcess(argv, 0, SHA_CANDIDATE + "\n", "")
        if argv == ["git", "rev-parse", f"{SHA_NEW_REPAIRED}^"]:
            return subprocess.CompletedProcess(
                argv, 0, SHA_NEW_CANDIDATE + "\n", ""
            )
        if len(argv) == 5 and argv[:4] == [
            "git", "diff", "--name-only", "-z"
        ] and argv[4] in {
            f"{SHA_CANDIDATE}..{SHA_REPAIRED}",
            f"{SHA_NEW_CANDIDATE}..{SHA_NEW_REPAIRED}",
        }:
            return subprocess.CompletedProcess(
                argv, 0, "\0".join(self.repair_paths) + "\0", ""
            )
        if argv == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(
                argv, 0, "auto-sync/upstream\n", ""
            )
        if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["git", "reset", "--hard"]:
            self.review_head = argv[3]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, self.review_head + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class GitHub:
    def __init__(self, evidence: list[SyncPullRequestEvidence | Exception]):
        self._evidence = list(evidence)
        self.merge_calls: list[tuple[int, str]] = []

    def evidence(self, pr_number: int) -> SyncPullRequestEvidence:
        item = self._evidence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def merge_exact(self, pr_number: int, *, expected_head: str) -> str:
        self.merge_calls.append((pr_number, expected_head))
        return SHA_MERGE


@dataclass
class Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class AdvancingGitHub(GitHub):
    def __init__(self, clock: Clock, advance: float):
        super().__init__([evidence()])
        self.clock = clock
        self.advance = advance

    def evidence(self, pr_number: int) -> SyncPullRequestEvidence:
        self.clock.value += self.advance
        return super().evidence(pr_number)


def checks() -> tuple[CheckResult, ...]:
    return tuple(
        CheckResult(name, "passed")
        for name in (
            "diff_check",
            "unmerged_index",
            "conflict_markers",
            "compileall",
            "tests",
        )
    )


def candidate(
    *,
    classification: SyncClassification = SyncClassification.CLEAN,
    state: SyncState = SyncState.PR_UPDATED,
    conflicted_files: tuple[str, ...] = (),
    resolution_record: Path | None = None,
    resolution_evidence_dir: Path | None = None,
    resolution_strategy: str | None = None,
    candidate_sha: str = SHA_CANDIDATE,
    candidate_tree_sha: str = SHA_CANDIDATE_TREE,
    changed_files: tuple[str, ...] = (),
    base_sha: str = SHA_BASE,
    upstream_sha: str = SHA_UPSTREAM,
) -> SyncResult:
    return SyncResult(
        state=state,
        base_sha=base_sha,
        upstream_sha=upstream_sha,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree_sha,
        pr_number=7,
        checks=checks(),
        changed_files=changed_files,
        classification=classification,
        conflicted_files=conflicted_files,
        resolution_record=resolution_record,
        resolution_evidence_dir=resolution_evidence_dir,
        resolution_strategy=resolution_strategy,
    )


def evidence(
    *,
    head: str = SHA_CANDIDATE,
    conclusion: str = "success",
    base: str = SHA_BASE,
) -> SyncPullRequestEvidence:
    return SyncPullRequestEvidence(
        number=7,
        state="open",
        base_sha=base,
        head_sha=head,
        required_check="All required checks pass",
        required_check_conclusion=conclusion,
        workflow_run_id=101,
        required_check_run_id=202,
    )


class Remediator:
    def __init__(
        self,
        *,
        retry: bool | list[bool] = False,
        repaired: SyncResult | list[SyncResult] | None = None,
    ):
        self.retry = retry
        self.repaired = repaired
        self.retry_calls = 0
        self.repair_calls: list[tuple[str, ...]] = []

    def retry_infrastructure(self, value: SyncResult, evidence) -> bool:
        self.retry_calls += 1
        if isinstance(self.retry, list):
            return self.retry.pop(0)
        return self.retry

    def repair_candidate(
        self, value: SyncResult, *, health_evidence: tuple[str, ...] = ()
    ) -> SyncResult | None:
        self.repair_calls.append(health_evidence)
        if isinstance(self.repaired, list):
            return self.repaired.pop(0)
        return self.repaired


class GreenReviewer:
    def review(self, **kwargs):
        digest = Path(kwargs["resolution_record"]).stem.removeprefix("resolution-")
        return ConflictReviewReceipt(
            candidate_sha=kwargs["candidate_sha"],
            resolver_backend="codex",
            reviewer_backend="claude",
            verdict="green",
            findings=(),
            reviewed_at="2026-07-12T16:00:00Z",
            resolution_record_sha256=digest,
        )


def reviewed_repair(
    tmp_path: Path,
    *,
    candidate_sha: str = SHA_REPAIRED,
    candidate_tree_sha: str = SHA_REPAIRED_TREE,
    path: str = "upstream.txt",
    base_sha: str = SHA_BASE,
    upstream_sha: str = SHA_UPSTREAM,
) -> SyncResult:
    evidence_dir = tmp_path / ".git" / "hermes-sync-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    raw = evidence_dir / f"repair-{candidate_sha}.json"
    raw.write_text(
        '{"conflicts":[{"path":'
        f'"{path}","decision":"repair exact candidate"}}],'
        '"strategy":"candidate_repair"}',
        encoding="utf-8",
    )
    return candidate(
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=(path,),
        resolution_record=raw,
        resolution_evidence_dir=evidence_dir,
        resolution_strategy="candidate_repair",
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree_sha,
        changed_files=(path,),
        base_sha=base_sha,
        upstream_sha=upstream_sha,
    )


def review_config(tmp_path: Path) -> AutonomousSyncConfig:
    value = config(tmp_path)
    return AutonomousSyncConfig(**{**value.__dict__, "resolver_backend": "codex"})


def config(
    tmp_path: Path,
    *,
    timeout: int = 30,
    interval: int = 5,
    max_upstream_refreshes: int = 2,
):
    sync = SyncConfig(
        repo=tmp_path / "repo",
        worktree=tmp_path / "candidate",
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "sync.lock",
    )
    deploy = DeployConfig(
        install_root=tmp_path / "install",
        origin="origin",
        record_root=tmp_path / "deployments",
        repo_slug=sync.repo_slug,
        sync_receipt_root=tmp_path / "receipts",
    )
    return AutonomousSyncConfig(
        sync=sync,
        deploy=deploy,
        receipt_root=tmp_path / "receipts",
        required_check="All required checks pass",
        check_timeout_seconds=timeout,
        poll_interval_seconds=interval,
        max_upstream_refreshes=max_upstream_refreshes,
    )


def deployed_record(
    status: str = "deployed", checks_value: tuple[HealthCheck, ...] = ()
) -> DeploymentRecord:
    return DeploymentRecord(
        id="record",
        requested_sha=SHA_MERGE,
        previous_sha=SHA_BASE,
        snapshot={},
        runtime_before={},
        runtime_after={},
        checks=checks_value,
        status=status,
        rollback=None,
    )


def receipt_artifact(path: Path) -> SyncReceiptArtifact:
    return SyncReceiptArtifact(path=path, sha256="e" * 64)


def test_clean_candidate_merges_and_deploys_without_human_artifact(
    tmp_path: Path, monkeypatch
):
    events: list[str] = []
    github = GitHub([evidence()])
    cfg = config(tmp_path)

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: events.append("prepare") or candidate(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type(
            "Artifact", (), {"path": tmp_path / "pre.json"}
        )(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged.json"),
    )

    def deploy_fn(receipt: Path, sha: str, pr_number: int) -> DeploymentRecord:
        events.append("deploy")
        assert receipt == tmp_path / "merged.json"
        assert (sha, pr_number) == (SHA_MERGE, 7)
        return deployed_record()

    result = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=None,
        deploy_fn=deploy_fn,
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert result.merge_sha == result.deployed_sha == SHA_MERGE
    assert result.fork_main_sha == result.installed_sha == SHA_MERGE
    assert result.needs_ole is False
    assert github.merge_calls == [(7, SHA_CANDIDATE)]
    assert events == ["prepare", "deploy"]


def test_preflight_failure_after_merge_resumes_before_ordinary_prepare(
    tmp_path: Path, monkeypatch
):
    cfg = config(tmp_path)
    prepared: list[str] = []
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.append("first") or candidate(),
    )

    first = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence()]),
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: (_ for _ in ()).throw(
            PreflightError("simulated preflight interruption")
        ),
    )

    checkpoint = load_pending_deployment(
        cfg.receipt_root, repo_slug=cfg.sync.repo_slug
    )
    assert first.state is AutonomousSyncState.NEEDS_OLE
    assert checkpoint is not None
    assert checkpoint.merge_sha == SHA_MERGE

    class ResumeRunner(Runner):
        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            if argv == ["git", "fetch", "--no-tags", "origin", "main"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv == ["git", "rev-parse", "refs/remotes/origin/main"]:
                return subprocess.CompletedProcess(argv, 0, SHA_MERGE + "\n", "")
            return super().run(argv, cwd, timeout)

    merged_evidence = SyncPullRequestEvidence(
        **{
            **evidence().__dict__,
            "state": "merged",
            "merge_sha": SHA_MERGE,
        }
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary prepare must not run while deploy is pending")
        ),
    )

    resumed = run_autonomous_sync(
        cfg,
        runner=ResumeRunner(),
        github=GitHub([merged_evidence]),
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert resumed.state is AutonomousSyncState.DEPLOYED
    assert resumed.merge_sha == resumed.deployed_sha == SHA_MERGE
    assert prepared == ["first"]
    assert load_pending_deployment(
        cfg.receipt_root, repo_slug=cfg.sync.repo_slug
    ) is None


def test_changed_pr_head_stops_before_merge(tmp_path: Path, monkeypatch):
    github = GitHub([evidence(head="9" * 40)])
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )

    result = run_autonomous_sync(
        config(tmp_path, max_upstream_refreshes=0),
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
        sleeper=lambda seconds: None,
    )

    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert "head" in result.reason
    assert github.merge_calls == []


def test_pending_check_times_out_without_merge(tmp_path: Path, monkeypatch):
    clock = Clock()
    github = GitHub([evidence(conclusion="pending")] * 4)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )

    result = run_autonomous_sync(
        config(tmp_path, timeout=10, interval=5),
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
        clock=clock,
        sleeper=clock.sleep,
    )

    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert "timed out" in result.reason
    assert github.merge_calls == []


def test_green_evidence_returned_after_deadline_is_not_accepted(
    tmp_path: Path, monkeypatch
):
    clock = Clock()
    github = AdvancingGitHub(clock, advance=11)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    result = run_autonomous_sync(
        config(tmp_path, timeout=10, interval=5),
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
        clock=clock,
        sleeper=clock.sleep,
    )
    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert "timed out" in result.reason
    assert github.merge_calls == []


def test_upstream_change_before_merge_is_quiet_refresh(tmp_path: Path, monkeypatch):
    class ChangedUpstreamRunner(Runner):
        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            if argv == ["git", "rev-parse", "upstream/main"]:
                return subprocess.CompletedProcess(argv, 0, "9" * 40 + "\n", "")
            return super().run(argv, cwd, timeout)

    github = GitHub([evidence()])
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    result = run_autonomous_sync(
        config(tmp_path, max_upstream_refreshes=0),
        runner=ChangedUpstreamRunner(),
        github=github,
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
    )
    assert result.state is AutonomousSyncState.PENDING_REFRESH
    assert result.needs_ole is False
    assert github.merge_calls == []


def test_upstream_change_restarts_inside_lock_then_deploys_fresh_candidate(
    tmp_path: Path, monkeypatch
):
    prepared = [candidate(), candidate()]
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.pop(0),
    )
    current = iter((False, True))
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller._upstream_is_current",
        lambda *args, **kwargs: next(current),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )

    result = run_autonomous_sync(
        config(tmp_path, max_upstream_refreshes=1),
        runner=Runner(),
        github=GitHub([evidence(), evidence()]),
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert prepared == []


def test_upstream_churn_exhaustion_is_pending_without_ole(
    tmp_path: Path, monkeypatch
):
    prepared = [candidate(), candidate()]
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.pop(0),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller._upstream_is_current",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )

    result = run_autonomous_sync(
        config(tmp_path, max_upstream_refreshes=1),
        runner=Runner(),
        github=GitHub([evidence(), evidence()]),
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.PENDING_REFRESH
    assert result.needs_ole is False
    assert prepared == []


def test_one_transient_evidence_failure_is_retried(tmp_path: Path, monkeypatch):
    github = GitHub([RuntimeError("temporary"), evidence()])
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )

    result = run_autonomous_sync(
        config(tmp_path),
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
        sleeper=lambda seconds: None,
    )

    assert result.state is AutonomousSyncState.DEPLOYED


def test_red_check_stops_without_merge(tmp_path: Path, monkeypatch):
    github = GitHub([evidence(conclusion="failure")])
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    result = run_autonomous_sync(
        config(tmp_path),
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
    )
    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert "not green" in result.reason
    assert github.merge_calls == []


def test_exact_infrastructure_retry_is_used_once_before_merge(
    tmp_path: Path, monkeypatch
):
    github = GitHub(
        [evidence(conclusion="failure"), evidence(conclusion="success")]
    )
    remediator = Remediator(retry=True)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )

    result = run_autonomous_sync(
        config(tmp_path),
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=None,
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert remediator.retry_calls == 1
    assert remediator.repair_calls == []


def test_red_candidate_gets_one_changed_locally_green_repair(
    tmp_path: Path, monkeypatch
):
    repaired = reviewed_repair(tmp_path)
    remediator = Remediator(retry=False, repaired=repaired)
    github = GitHub(
        [
            evidence(conclusion="failure"),
            evidence(head=SHA_REPAIRED, conclusion="success"),
        ]
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    captured: dict[str, SyncResult] = {}

    def write_receipt(*args, **kwargs):
        captured["candidate"] = args[1]
        return type("Artifact", (), {"path": tmp_path / "pre"})()

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        write_receipt,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )

    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=github,
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert remediator.retry_calls == 1
    assert len(remediator.repair_calls) == 1
    assert captured["candidate"].candidate_sha == SHA_REPAIRED
    assert github.merge_calls == [(7, SHA_REPAIRED)]


def test_formerly_clean_ai_repair_requires_fresh_exact_independent_review(
    tmp_path: Path, monkeypatch
):
    evidence_dir = tmp_path / ".git" / "hermes-sync-evidence"
    evidence_dir.mkdir(parents=True)
    raw = evidence_dir / "repair.json"
    raw.write_text(
        '{"conflicts":[{"path":"upstream.txt",'
        '"decision":"repair the exact failed check"}],'
        '"strategy":"candidate_repair"}',
        encoding="utf-8",
    )
    repaired = candidate(
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=("upstream.txt",),
        resolution_record=raw,
        resolution_evidence_dir=evidence_dir,
        resolution_strategy="candidate_repair",
        candidate_sha=SHA_REPAIRED,
        candidate_tree_sha=SHA_REPAIRED_TREE,
        changed_files=("upstream.txt",),
    )
    remediator = Remediator(retry=False, repaired=repaired)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(changed_files=("upstream.txt",)),
    )
    reviewed: list[str] = []

    class Reviewer:
        def review(self, **kwargs):
            reviewed.append(kwargs["candidate_sha"])
            digest = Path(kwargs["resolution_record"]).stem.removeprefix(
                "resolution-"
            )
            return ConflictReviewReceipt(
                candidate_sha=kwargs["candidate_sha"],
                resolver_backend="codex",
                reviewer_backend="claude",
                verdict="green",
                findings=(),
                reviewed_at="2026-07-12T16:00:00Z",
                resolution_record_sha256=digest,
            )

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    cfg = config(tmp_path)
    cfg = AutonomousSyncConfig(**{**cfg.__dict__, "resolver_backend": "codex"})

    result = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub(
            [evidence(conclusion="failure"), evidence(head=SHA_REPAIRED)]
        ),
        resolver=None,
        reviewer=Reviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert reviewed == [SHA_REPAIRED]


def test_conflict_derived_repair_gets_fresh_independent_review(
    tmp_path: Path, monkeypatch
):
    evidence_dir = tmp_path / ".git" / "hermes-sync-evidence"
    evidence_dir.mkdir(parents=True)
    raw_one = evidence_dir / "initial.json"
    raw_two = evidence_dir / "repaired.json"
    raw = (
        '{"conflicts":[{"path":"gateway/run.py",'
        '"decision":"preserve the fork guard"}],'
        '"strategy":"preserve_fork_behavior"}'
    )
    raw_one.write_text(raw, encoding="utf-8")
    raw_two.write_text(
        '{"conflicts":[{"path":"gateway/run.py",'
        '"decision":"repair exact conflict-derived candidate"}],'
        '"strategy":"candidate_repair"}',
        encoding="utf-8",
    )
    initial = candidate(
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=("gateway/run.py",),
        resolution_record=raw_one,
        resolution_evidence_dir=evidence_dir,
        resolution_strategy="preserve_fork_behavior",
    )
    repaired = candidate(
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=("gateway/run.py",),
        resolution_record=raw_two,
        resolution_evidence_dir=evidence_dir,
        resolution_strategy="candidate_repair",
        candidate_sha=SHA_REPAIRED,
        candidate_tree_sha=SHA_REPAIRED_TREE,
    )
    remediator = Remediator(retry=False, repaired=repaired)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: initial,
    )
    reviewed_heads: list[str] = []

    class Reviewer:
        def review(self, **kwargs):
            reviewed_heads.append(kwargs["candidate_sha"])
            digest = Path(kwargs["resolution_record"]).stem.removeprefix(
                "resolution-"
            )
            return ConflictReviewReceipt(
                candidate_sha=kwargs["candidate_sha"],
                resolver_backend="codex",
                reviewer_backend="claude",
                verdict="green",
                findings=(),
                reviewed_at="2026-07-12T16:00:00Z",
                resolution_record_sha256=digest,
            )

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    cfg = config(tmp_path)
    cfg = AutonomousSyncConfig(**{**cfg.__dict__, "resolver_backend": "codex"})

    result = run_autonomous_sync(
        cfg,
        runner=Runner(repair_paths=("gateway/run.py",)),
        github=GitHub(
            [
                evidence(conclusion="failure"),
                evidence(head=SHA_REPAIRED),
            ]
        ),
        resolver=None,
        reviewer=Reviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert reviewed_heads == [SHA_CANDIDATE, SHA_REPAIRED]


def test_unchanged_candidate_repair_is_rejected(tmp_path: Path, monkeypatch):
    remediator = Remediator(retry=False, repaired=candidate())
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    result = run_autonomous_sync(
        config(tmp_path),
        runner=Runner(),
        github=GitHub([evidence(conclusion="failure")]),
        resolver=None,
        reviewer=None,
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )
    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert "repair evidence" in result.reason
    assert len(remediator.repair_calls) == 1


def test_changed_repair_commit_with_unchanged_tree_is_rejected(
    tmp_path: Path, monkeypatch
):
    remediator = Remediator(
        retry=False,
        repaired=candidate(candidate_sha=SHA_REPAIRED),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    result = run_autonomous_sync(
        config(tmp_path),
        runner=Runner(),
        github=GitHub([evidence(conclusion="failure")]),
        resolver=None,
        reviewer=None,
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert "repair evidence" in result.reason


@pytest.mark.parametrize(
    "reported_paths",
    [(), ("upstream.txt", "extra.py"), ("upstream.txt", "upstream.txt")],
)
def test_nonexact_repair_diff_paths_are_rejected_before_claude(
    tmp_path: Path, monkeypatch, reported_paths: tuple[str, ...]
):
    repaired = reviewed_repair(tmp_path)
    repaired = repaired.__class__(
        **{**repaired.__dict__, "conflicted_files": reported_paths}
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )

    class Reviewer:
        def review(self, **kwargs):
            raise AssertionError("Claude must not see invalid repair evidence")

    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=GitHub([evidence(conclusion="failure")]),
        resolver=None,
        reviewer=Reviewer(),
        remediator=Remediator(repaired=repaired),
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.NEEDS_OLE


def test_repaired_candidate_red_does_not_get_a_second_repair(
    tmp_path: Path, monkeypatch
):
    remediator = Remediator(retry=False, repaired=reviewed_repair(tmp_path))
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=GitHub(
            [
                evidence(conclusion="failure"),
                evidence(head=SHA_REPAIRED, conclusion="failure"),
            ]
        ),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )
    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert len(remediator.repair_calls) == 1


def test_code_repair_then_uses_remaining_exact_infrastructure_retry(
    tmp_path: Path, monkeypatch
):
    remediator = Remediator(
        retry=[False, True], repaired=reviewed_repair(tmp_path)
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )

    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=GitHub(
            [
                evidence(conclusion="failure"),
                evidence(head=SHA_REPAIRED, conclusion="failure"),
                evidence(head=SHA_REPAIRED),
            ]
        ),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert remediator.retry_calls == 2
    assert len(remediator.repair_calls) == 1


@pytest.mark.parametrize(
    "conclusion", ["cancelled", "timed_out", "action_required", "neutral"]
)
def test_exact_nonfailure_red_conclusion_is_nonalerting_pending(
    tmp_path: Path, monkeypatch, conclusion: str
):
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(),
    )
    result = run_autonomous_sync(
        config(tmp_path),
        runner=Runner(),
        github=GitHub([evidence(conclusion=conclusion)]),
        resolver=None,
        reviewer=None,
        remediator=Remediator(),
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.PENDING_REFRESH
    assert result.needs_ole is False
    assert conclusion in result.reason


def test_genuinely_new_upstream_head_resets_candidate_scoped_budgets(
    tmp_path: Path, monkeypatch
):
    new_upstream = "c" * 40
    first = candidate()
    second = candidate(
        candidate_sha=SHA_NEW_CANDIDATE,
        candidate_tree_sha=SHA_NEW_CANDIDATE_TREE,
        upstream_sha=new_upstream,
        changed_files=("upstream.txt",),
    )
    first_repair = reviewed_repair(tmp_path)
    second_repair = reviewed_repair(
        tmp_path,
        candidate_sha=SHA_NEW_REPAIRED,
        candidate_tree_sha=SHA_NEW_REPAIRED_TREE,
        upstream_sha=new_upstream,
    )
    prepared = [first, second]
    remediator = Remediator(
        retry=[False, False], repaired=[first_repair, second_repair]
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.pop(0),
    )
    current = iter((False, True))
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller._upstream_is_current",
        lambda *args, **kwargs: next(current),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )

    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=GitHub(
            [
                evidence(conclusion="failure"),
                evidence(head=SHA_REPAIRED),
                evidence(head=SHA_NEW_CANDIDATE, conclusion="failure"),
                evidence(head=SHA_NEW_REPAIRED),
            ]
        ),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert len(remediator.repair_calls) == 2
    assert prepared == []


def test_post_revert_upstream_advance_returns_pending_without_ordinary_prepare(
    tmp_path: Path, monkeypatch
):
    prepared = [candidate()]
    remediator = Remediator(repaired=reviewed_repair(tmp_path))
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.pop(0),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.resume_failed_candidate_reconstruction",
        lambda *args, **kwargs: candidate(changed_files=("upstream.txt",)),
    )
    current = iter((True, False))
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller._upstream_is_current",
        lambda *args, **kwargs: next(current),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller._current_upstream_sha",
        lambda *args, **kwargs: SHA_NEW_CANDIDATE,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover",
        lambda *args, **kwargs: AutonomousSyncResult(
            state=AutonomousSyncState.ROLLED_BACK_REVERTED,
            installed_sha=SHA_BASE,
            fork_main_sha=SHA_BASE,
        ),
    )

    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=GitHub([evidence(), evidence(head=SHA_REPAIRED)]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record("rolled_back"),
        verify_runtime_fn=lambda sha: True,
    )

    assert result.state is AutonomousSyncState.PENDING_REFRESH
    assert result.needs_ole is False
    assert prepared == []


def test_pending_reconstruction_stops_after_durable_retry_budget(
    tmp_path: Path, monkeypatch
):
    cfg = review_config(tmp_path)
    write_pending_reconstruction(
        cfg.receipt_root,
        PendingReconstructionCheckpoint(
            schema_version=2,
            repo_slug=cfg.sync.repo_slug,
            stage="recovered",
            failed_base_sha=SHA_BASE,
            failed_upstream_sha=SHA_UPSTREAM,
            failed_candidate_sha=SHA_CANDIDATE,
            failed_candidate_tree_sha=SHA_CANDIDATE_TREE,
            failed_pr_number=7,
            failed_merge_sha=SHA_MERGE,
            revert_main_sha=SHA_REPAIRED,
            previous_healthy_installed_sha=SHA_BASE,
            target_upstream_sha=SHA_NEW_REPAIRED,
            expected_rolling_candidate_sha=SHA_NEW_CANDIDATE,
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
            reason="upstream advanced during reconstruction",
            resume_attempts=2,
        ),
    )
    prepared: list[bool] = []
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.append(True) or candidate(),
    )

    result = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=Remediator(repaired=reviewed_repair(tmp_path)),
        deploy_fn=lambda *args: deployed_record(),
        verify_runtime_fn=lambda sha: True,
    )

    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert result.reason == "pending reconstruction retry budget is exhausted"
    assert prepared == []


def test_recovery_checkpoint_exists_before_reconstruction_starts(
    tmp_path: Path, monkeypatch
):
    cfg = review_config(tmp_path)
    prepared = [candidate()]
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.pop(0),
    )

    def interrupted(*args, **kwargs):
        checkpoint = load_pending_reconstruction(
            cfg.receipt_root, repo_slug=cfg.sync.repo_slug
        )
        assert checkpoint is not None
        assert checkpoint.stage == "recovered"
        assert checkpoint.target_upstream_sha == SHA_UPSTREAM
        assert checkpoint.expected_rolling_candidate_sha == SHA_CANDIDATE
        raise ReconstructionError("simulated process interruption")

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.resume_failed_candidate_reconstruction",
        interrupted,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover",
        lambda *args, **kwargs: AutonomousSyncResult(
            state=AutonomousSyncState.ROLLED_BACK_REVERTED,
            installed_sha=SHA_BASE,
            fork_main_sha=SHA_BASE,
        ),
    )

    result = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence()]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=Remediator(repaired=reviewed_repair(tmp_path)),
        deploy_fn=lambda *args: deployed_record("rolled_back"),
        verify_runtime_fn=lambda sha: True,
    )

    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert load_pending_reconstruction(
        cfg.receipt_root, repo_slug=cfg.sync.repo_slug
    ).stage == "recovered"


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out"])
def test_repaired_checkpoint_survives_nonterminal_check_and_next_run_deploys(
    tmp_path: Path, monkeypatch, conclusion: str
):
    cfg = review_config(tmp_path)
    prepared = [candidate()]
    prepare_calls = 0

    def prepare(*args, **kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return prepared.pop(0)

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate", prepare
    )
    interrupted = True
    resume_calls = 0
    reconstructed = candidate(
        candidate_sha=SHA_NEW_CANDIDATE,
        candidate_tree_sha=SHA_NEW_CANDIDATE_TREE,
        base_sha=SHA_BASE,
        upstream_sha=SHA_UPSTREAM,
        changed_files=("upstream.txt",),
    )

    def resume(*args, **kwargs):
        nonlocal interrupted, resume_calls
        if interrupted:
            interrupted = False
            raise ReconstructionError("simulated process interruption")
        resume_calls += 1
        return reconstructed

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.resume_failed_candidate_reconstruction",
        resume,
    )
    repaired = reviewed_repair(
        tmp_path,
        candidate_sha=SHA_NEW_REPAIRED,
        candidate_tree_sha=SHA_NEW_REPAIRED_TREE,
        base_sha=SHA_BASE,
        upstream_sha=SHA_UPSTREAM,
    )
    remediator = Remediator(repaired=repaired)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    outcomes = [
        AutonomousSyncResult(
            state=AutonomousSyncState.ROLLED_BACK_REVERTED,
            installed_sha=SHA_BASE,
            fork_main_sha=SHA_BASE,
        ),
        AutonomousSyncResult(
            state=AutonomousSyncState.DEPLOYED,
            deployed_sha=SHA_MERGE,
        ),
    ]
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover",
        lambda *args, **kwargs: outcomes.pop(0),
    )

    first = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence()]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record("rolled_back"),
        verify_runtime_fn=lambda sha: True,
    )
    assert first.state is AutonomousSyncState.NEEDS_OLE

    second = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([
            evidence(head=SHA_NEW_REPAIRED, conclusion=conclusion),
        ]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
        verify_runtime_fn=lambda sha: True,
    )
    assert second.state is AutonomousSyncState.PENDING_REFRESH
    checkpoint = load_pending_reconstruction(
        cfg.receipt_root, repo_slug=cfg.sync.repo_slug
    )
    assert checkpoint is not None
    assert checkpoint.stage == "repaired"
    assert checkpoint.repaired_candidate_sha == SHA_NEW_REPAIRED

    third = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence(head=SHA_NEW_REPAIRED)]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record(),
        verify_runtime_fn=lambda sha: True,
    )

    assert third.state is AutonomousSyncState.DEPLOYED
    assert prepare_calls == 1
    assert resume_calls == 1
    assert len(remediator.repair_calls) == 1
    assert load_pending_reconstruction(
        cfg.receipt_root, repo_slug=cfg.sync.repo_slug
    ) is None


def test_healthy_protected_rollback_gets_one_post_rollback_repair(
    tmp_path: Path, monkeypatch
):
    prepared = [candidate()]
    remediator = Remediator(repaired=reviewed_repair(tmp_path))
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.pop(0),
    )
    reconstruction_calls: list[str] = []
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.resume_failed_candidate_reconstruction",
        lambda *args, **kwargs: reconstruction_calls.append(
            kwargs["revert_main_sha"]
        )
        or candidate(changed_files=("upstream.txt",)),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    deployments = [
        deployed_record(
            "rolled_back", (HealthCheck("runtime:default", False),)
        ),
        deployed_record(),
    ]
    outcomes = [
        AutonomousSyncResult(
            state=AutonomousSyncState.ROLLED_BACK_REVERTED,
            installed_sha=SHA_BASE,
            fork_main_sha=SHA_BASE,
        ),
        AutonomousSyncResult(
            state=AutonomousSyncState.DEPLOYED,
            deployed_sha=SHA_MERGE,
        ),
    ]
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover",
        lambda *args, **kwargs: outcomes.pop(0),
    )

    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=GitHub([evidence(), evidence(head=SHA_REPAIRED)]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployments.pop(0),
        verify_runtime_fn=lambda sha: True,
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert remediator.retry_calls == 0
    assert remediator.repair_calls == [("runtime:default:failed",)]
    assert prepared == []
    assert reconstruction_calls == [SHA_BASE]


def test_second_deployment_failure_recovers_then_escalates_once(
    tmp_path: Path, monkeypatch
):
    prepared = [candidate()]
    remediator = Remediator(repaired=reviewed_repair(tmp_path))
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: prepared.pop(0),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.resume_failed_candidate_reconstruction",
        lambda *args, **kwargs: candidate(changed_files=("upstream.txt",)),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    recovery_calls: list[str] = []

    def recovered(*args, **kwargs):
        recovery_calls.append(args[1].candidate_sha)
        return AutonomousSyncResult(
            state=AutonomousSyncState.ROLLED_BACK_REVERTED,
            installed_sha=SHA_BASE,
            fork_main_sha=SHA_BASE,
        )

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover", recovered
    )

    result = run_autonomous_sync(
        review_config(tmp_path),
        runner=Runner(),
        github=GitHub([evidence(), evidence(head=SHA_REPAIRED)]),
        resolver=None,
        reviewer=GreenReviewer(),
        remediator=remediator,
        deploy_fn=lambda *args: deployed_record("rolled_back"),
        verify_runtime_fn=lambda sha: True,
    )

    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert "exhausted" in result.reason
    assert recovery_calls == [SHA_CANDIDATE, SHA_REPAIRED]
    assert len(remediator.repair_calls) == 1


def test_minor_without_review_and_major_candidate_stop(tmp_path: Path, monkeypatch):
    cfg = config(tmp_path)
    for classification in (
        SyncClassification.MINOR_REVIEW_REQUIRED,
        SyncClassification.MAJOR,
    ):
        monkeypatch.setattr(
            "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
            lambda *args, _classification=classification, **kwargs: candidate(
                classification=_classification
            ),
        )
        result = run_autonomous_sync(
            cfg,
            runner=Runner(),
            github=GitHub([evidence()]),
            resolver=None,
            reviewer=None,
            deploy_fn=lambda *args: deployed_record(),
        )
        assert result.state is AutonomousSyncState.NEEDS_OLE
        assert result.needs_ole is True


def test_independently_reviewed_minor_candidate_auto_merges(
    tmp_path: Path, monkeypatch
):
    evidence_dir = tmp_path / ".git" / "hermes-sync-evidence"
    record = evidence_dir / ".hermes-sync-resolution.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        '{"conflicts":[{"path":"gateway/run.py",'
        '"decision":"preserve fork behavior"}],'
        '"strategy":"preserve_fork_behavior"}',
        encoding="utf-8",
    )
    minor = candidate(
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=("gateway/run.py",),
        resolution_record=record,
        resolution_evidence_dir=evidence_dir,
        resolution_strategy="preserve_fork_behavior",
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: minor,
    )
    captured: dict[str, object] = {}

    class Reviewer:
        def review(self, **kwargs):
            digest = Path(kwargs["resolution_record"]).stem.removeprefix(
                "resolution-"
            )
            return ConflictReviewReceipt(
                candidate_sha=SHA_CANDIDATE,
                resolver_backend="codex",
                reviewer_backend="claude",
                verdict="green",
                findings=(),
                reviewed_at="2026-07-12T16:00:00Z",
                resolution_record_sha256=digest,
            )

    def write_receipt(*args, **kwargs):
        captured["candidate"] = args[1]
        captured["review"] = kwargs["conflict_review"]
        return type("Artifact", (), {"path": tmp_path / "pre"})()

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        write_receipt,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: receipt_artifact(tmp_path / "merged"),
    )
    cfg = config(tmp_path)
    cfg = AutonomousSyncConfig(
        **{**cfg.__dict__, "resolver_backend": "codex"}
    )
    result = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence()]),
        resolver=None,
        reviewer=Reviewer(),
        deploy_fn=lambda *args: deployed_record(),
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert captured["candidate"].classification is SyncClassification.MINOR_RESOLVED
    assert captured["review"].reviewer_backend == "claude"


def test_lock_contention_returns_locked(tmp_path: Path):
    cfg = config(tmp_path)
    with try_exclusive_file_lock(cfg.sync.lock_path) as acquired:
        assert acquired
        result = run_autonomous_sync(
            cfg,
            runner=Runner(),
            github=GitHub([evidence()]),
            resolver=None,
            reviewer=None,
            deploy_fn=lambda *args: deployed_record(),
        )
    assert result.state is AutonomousSyncState.LOCKED


def test_outcome_is_published_before_sync_lock_is_released(
    tmp_path: Path, monkeypatch
):
    cfg = config(tmp_path)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(state=SyncState.NO_CHANGE),
    )
    reacquired: list[bool] = []

    def publish_outcome(result: AutonomousSyncResult) -> bool:
        with try_exclusive_file_lock(cfg.sync.lock_path) as acquired:
            reacquired.append(acquired)
        return True

    result = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence()]),
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
        publish_outcome=publish_outcome,
    )

    assert reacquired == [False]
    assert result.notify_ole is True


def test_outcome_publication_failure_is_not_retried_or_reclassified(
    tmp_path: Path, monkeypatch
):
    cfg = config(tmp_path)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(state=SyncState.NO_CHANGE),
    )
    publication_calls = 0

    def failed_publication(result: AutonomousSyncResult) -> bool:
        nonlocal publication_calls
        publication_calls += 1
        raise RuntimeError("status publication failed")

    with pytest.raises(RuntimeError, match="status publication failed"):
        run_autonomous_sync(
            cfg,
            runner=Runner(),
            github=GitHub([evidence()]),
            resolver=None,
            reviewer=None,
            deploy_fn=lambda *args: deployed_record(),
            publish_outcome=failed_publication,
        )

    assert publication_calls == 1


def test_contending_run_cannot_overtake_blocked_outcome_publication(
    tmp_path: Path, monkeypatch
):
    cfg = config(tmp_path)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: candidate(state=SyncState.NO_CHANGE),
    )
    publisher_entered = threading.Event()
    release_publisher = threading.Event()
    publications: list[str] = []
    first_result: list[AutonomousSyncResult] = []

    def blocked_publisher(result: AutonomousSyncResult) -> bool:
        publisher_entered.set()
        assert release_publisher.wait(timeout=5)
        publications.append("first")
        return False

    def first_run() -> None:
        first_result.append(
            run_autonomous_sync(
                cfg,
                runner=Runner(),
                github=GitHub([evidence()]),
                resolver=None,
                reviewer=None,
                deploy_fn=lambda *args: deployed_record(),
                publish_outcome=blocked_publisher,
            )
        )

    thread = threading.Thread(target=first_run)
    thread.start()
    assert publisher_entered.wait(timeout=5)

    second = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence()]),
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
        publish_outcome=lambda result: publications.append("second") or False,
    )

    assert second.state is AutonomousSyncState.LOCKED
    assert publications == []
    release_publisher.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_result[0].state is AutonomousSyncState.NO_CHANGE

    third = run_autonomous_sync(
        cfg,
        runner=Runner(),
        github=GitHub([evidence()]),
        resolver=None,
        reviewer=None,
        deploy_fn=lambda *args: deployed_record(),
        publish_outcome=lambda result: publications.append("third") or False,
    )

    assert third.state is AutonomousSyncState.NO_CHANGE
    assert publications == ["first", "third"]


def test_unexpected_failure_is_logged_without_secret_terminal_or_log_leak(
    tmp_path: Path, monkeypatch, caplog
):
    secret = "https://token.example.invalid/path?secret=sk-live-value"
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    with caplog.at_level(logging.ERROR):
        result = run_autonomous_sync(
            config(tmp_path),
            runner=Runner(),
            github=GitHub([evidence()]),
            resolver=None,
            reviewer=None,
            deploy_fn=lambda *args: deployed_record(),
        )
    assert result.reason == "unexpected autonomous sync failure"
    assert secret not in caplog.text
    assert "sk-live-value" not in caplog.text
