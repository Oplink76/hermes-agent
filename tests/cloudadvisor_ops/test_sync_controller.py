from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ops.cloudadvisor.hermes_ops.deploy import DeployConfig, DeploymentRecord
from ops.cloudadvisor.hermes_ops.locking import try_exclusive_file_lock
from ops.cloudadvisor.hermes_ops.sync import (
    CheckResult,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
)
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncConfig,
    AutonomousSyncState,
    run_autonomous_sync,
)
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_review import ConflictReviewReceipt


SHA_BASE = "1" * 40
SHA_UPSTREAM = "2" * 40
SHA_CANDIDATE = "3" * 40
SHA_MERGE = "4" * 40
SHA_CANDIDATE_TREE = "5" * 40


class Runner:
    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
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
) -> SyncResult:
    return SyncResult(
        state=state,
        base_sha=SHA_BASE,
        upstream_sha=SHA_UPSTREAM,
        candidate_sha=SHA_CANDIDATE,
        candidate_tree_sha=SHA_CANDIDATE_TREE,
        pr_number=7,
        checks=checks(),
        classification=classification,
        conflicted_files=conflicted_files,
        resolution_record=resolution_record,
    )


def evidence(
    *,
    head: str = SHA_CANDIDATE,
    conclusion: str = "success",
) -> SyncPullRequestEvidence:
    return SyncPullRequestEvidence(
        number=7,
        state="open",
        base_sha=SHA_BASE,
        head_sha=head,
        required_check="All required checks pass",
        required_check_conclusion=conclusion,
    )


def config(tmp_path: Path, *, timeout: int = 30, interval: int = 5):
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
    )


def deployed_record(status: str = "deployed") -> DeploymentRecord:
    return DeploymentRecord(
        id="record",
        requested_sha=SHA_MERGE,
        previous_sha=SHA_BASE,
        snapshot={},
        runtime_before={},
        runtime_after={},
        checks=(),
        status=status,
        rollback=None,
    )


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
        lambda *args, **kwargs: type(
            "Artifact", (), {"path": tmp_path / "merged.json"}
        )(),
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


def test_changed_pr_head_stops_before_merge(tmp_path: Path, monkeypatch):
    github = GitHub([evidence(head="9" * 40)])
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
        lambda *args, **kwargs: type(
            "Artifact", (), {"path": tmp_path / "merged"}
        )(),
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
    record = tmp_path / "candidate" / ".hermes-sync-resolution.json"
    record.parent.mkdir()
    record.write_text(
        '{"conflicts":[{"path":"gateway/run.py",'
        '"decision":"preserve fork behavior"}]}',
        encoding="utf-8",
    )
    minor = candidate(
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=("gateway/run.py",),
        resolution_record=record,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: minor,
    )
    captured: dict[str, object] = {}

    class Reviewer:
        def review(self, **kwargs):
            return ConflictReviewReceipt(
                candidate_sha=SHA_CANDIDATE,
                resolver_backend="codex",
                reviewer_backend="claude",
                verdict="green",
                findings=(),
                reviewed_at="2026-07-12T16:00:00Z",
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
        lambda *args, **kwargs: type(
            "Artifact", (), {"path": tmp_path / "merged"}
        )(),
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
