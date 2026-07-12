from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.deploy import DeployConfig, DeploymentRecord
from ops.cloudadvisor.hermes_ops.health import HealthCheck
from ops.cloudadvisor.hermes_ops.sync import (
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
    CheckResult,
)
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncConfig,
    AutonomousSyncResult,
    AutonomousSyncState,
    run_autonomous_sync,
)
from ops.cloudadvisor.hermes_ops.sync_reconstruction import reconstruct_failed_candidate
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_remediation import CodexCandidateRemediator
from ops.cloudadvisor.hermes_ops.sync_review import ConflictReviewReceipt


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def test_real_reconstruction_reintroduces_failed_tree_on_verified_revert_main(
    tmp_path: Path,
):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "state.txt").write_text("healthy\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "-u", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "auto-sync/upstream")
    (repo / "state.txt").write_text("failed candidate\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "push", "origin", "auto-sync/upstream")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "auto-sync/upstream", "-m", "failed merge")
    failed_merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "revert", "-m", "1", "--no-edit", failed_merge)
    revert_main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")

    class GitHub:
        def __init__(self):
            self.created: list[tuple[str, str]] = []

        def find_open_pull_request(self, head: str, base_name: str):
            return None

        def create_pull_request(self, *, head: str, base: str, title: str, body: str):
            self.created.append((head, base))
            return 9

        def update_pull_request(self, number: int, *, title: str, body: str):
            raise AssertionError("unexpected existing reconstruction PR")

    config = SyncConfig(
        repo=repo,
        worktree=tmp_path / "rolling-candidate",
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "sync.lock",
    )
    result = reconstruct_failed_candidate(
        config,
        failed=SyncResult(
            state=SyncState.PR_UPDATED,
            base_sha=base,
            upstream_sha="a" * 40,
            candidate_sha=candidate_sha,
            candidate_tree_sha=candidate_tree,
            pr_number=7,
            classification=SyncClassification.CLEAN,
        ),
        failed_merge_sha=failed_merge,
        revert_main_sha=revert_main,
        github=GitHub(),
        runner=SubprocessCommandRunner(),
    )

    assert result.state is SyncState.PR_UPDATED
    assert result.base_sha == revert_main
    assert result.candidate_tree_sha == candidate_tree
    assert result.classification is SyncClassification.MINOR_REVIEW_REQUIRED
    assert result.resolution_strategy == "candidate_repair"
    assert _git(repo, "rev-parse", f"{result.candidate_sha}^") == revert_main
    assert _git(repo, "rev-parse", f"{result.candidate_sha}^{{tree}}") == candidate_tree
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == revert_main
    assert (
        _git(repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream").split()[0]
        == result.candidate_sha
    )


def test_real_controller_routes_rollback_through_reconstruction_and_reviewed_repair(
    tmp_path: Path, monkeypatch
):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "state.txt").write_text("healthy\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "-u", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "auto-sync/upstream")
    (repo / "state.txt").write_text("failed candidate\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "push", "origin", "auto-sync/upstream")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "auto-sync/upstream", "-m", "failed merge")
    failed_merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")
    _git(repo, "revert", "-m", "1", "--no-edit", failed_merge)
    revert_main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")
    rolling = tmp_path / "rolling-candidate"
    _git(repo, "worktree", "add", str(rolling), "auto-sync/upstream")

    driver = tmp_path / "fake_codex.py"
    driver.write_text(
        "\n".join([
            "import json",
            "from pathlib import Path",
            "Path('state.txt').write_text('repaired candidate\\n', encoding='utf-8')",
            "Path('.hermes-sync-repair.json').write_text(",
            "    json.dumps({'conflicts': [{'path': 'state.txt', "
            "'decision': 'repair failed runtime behavior'}], "
            "'strategy': 'candidate_repair'}),",
            "    encoding='utf-8',",
            ")",
        ])
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        codex = tmp_path / "codex.cmd"
        codex.write_bytes(
            f'@"{sys.executable}" "{driver}" %*\r\n'.encode("utf-8")
        )
    else:
        codex = tmp_path / "codex"
        codex.write_text(
            f"#!{sys.executable}\nexec(compile(open({str(driver)!r}).read(), "
            f"{str(driver)!r}, 'exec'))\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)

    local_checks = tuple(
        CheckResult(name, "passed")
        for name in (
            "diff_check",
            "unmerged_index",
            "conflict_markers",
            "compileall",
            "tests",
        )
    )
    failed = SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=base,
        upstream_sha="a" * 40,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree,
        pr_number=7,
        checks=local_checks,
        changed_files=("state.txt",),
        classification=SyncClassification.CLEAN,
    )
    sync_config = SyncConfig(
        repo=repo,
        worktree=rolling,
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "sync.lock",
    )
    config = AutonomousSyncConfig(
        sync=sync_config,
        deploy=DeployConfig(
            install_root=tmp_path / "install",
            origin="origin",
            record_root=tmp_path / "deployments",
            repo_slug=sync_config.repo_slug,
            sync_receipt_root=tmp_path / "receipts",
        ),
        receipt_root=tmp_path / "receipts",
        required_check="All required checks pass",
        resolver_backend="codex",
    )

    class GitHub:
        def __init__(self):
            self.evidence_calls = 0

        def find_open_pull_request(self, head: str, base_name: str):
            return None

        def create_pull_request(self, *, head: str, base: str, title: str, body: str):
            return 9

        def update_pull_request(self, number: int, *, title: str, body: str):
            raise AssertionError("unexpected existing repair PR")

        def evidence(self, pr_number: int):
            self.evidence_calls += 1
            if self.evidence_calls == 1:
                return SyncPullRequestEvidence(
                    7,
                    "open",
                    base,
                    candidate_sha,
                    "All required checks pass",
                    "success",
                    101,
                    202,
                )
            repaired_head = _git(
                repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream"
            ).split()[0]
            return SyncPullRequestEvidence(
                9,
                "open",
                revert_main,
                repaired_head,
                "All required checks pass",
                "success",
                303,
                404,
            )

        def merge_exact(self, pr_number: int, *, expected_head: str):
            return failed_merge if pr_number == 7 else "f" * 40

    remediator_impl = CodexCandidateRemediator(
        config=sync_config,
        runner=SubprocessCommandRunner(),
        executable=codex,
        prompt="Repair exact failed runtime behavior.",
        verify_fn=lambda worktree, runner: list(local_checks),
    )

    class Remediator:
        def retry_infrastructure(self, candidate, evidence):
            return False

        def repair_candidate(self, candidate, *, health_evidence=()):
            return remediator_impl.repair_candidate(
                candidate, health_evidence=health_evidence
            )

    reviewed_heads: list[str] = []

    class Reviewer:
        def review(self, **kwargs):
            assert _git(Path(kwargs["worktree"]), "rev-parse", "HEAD") == kwargs[
                "candidate_sha"
            ]
            reviewed_heads.append(kwargs["candidate_sha"])
            digest = Path(kwargs["resolution_record"]).stem.removeprefix(
                "resolution-"
            )
            return ConflictReviewReceipt(
                kwargs["candidate_sha"],
                "codex",
                "claude",
                "green",
                (),
                "2026-07-12T21:00:00Z",
                digest,
            )

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate",
        lambda *args, **kwargs: failed,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller._upstream_is_current",
        lambda *args, **kwargs: True,
    )
    reviews: list[object] = []
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: reviews.append(kwargs.get("conflict_review"))
        or type("Artifact", (), {"path": tmp_path / "pre"})(),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: type(
            "Artifact", (), {"path": tmp_path / "merged"}
        )(),
    )
    outcomes = [
        AutonomousSyncResult(
            AutonomousSyncState.ROLLED_BACK_REVERTED,
            fork_main_sha=revert_main,
            installed_sha=base,
        ),
        AutonomousSyncResult(
            AutonomousSyncState.DEPLOYED,
            deployed_sha="f" * 40,
        ),
    ]
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover",
        lambda *args, **kwargs: outcomes.pop(0),
    )
    deployments = [
        DeploymentRecord(
            "failed",
            failed_merge,
            base,
            {},
            {},
            {},
            (HealthCheck("runtime:default", False),),
            "rolled_back_healthy",
            {},
        ),
        DeploymentRecord("fixed", "f" * 40, base, {}, {}, {}, (), "deployed", None),
    ]

    result = run_autonomous_sync(
        config,
        runner=SubprocessCommandRunner(),
        github=GitHub(),
        resolver=None,
        reviewer=Reviewer(),
        remediator=Remediator(),
        deploy_fn=lambda *args: deployments.pop(0),
        verify_runtime_fn=lambda sha: True,
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert len(reviewed_heads) == 1
    assert reviews[0] is None
    assert reviews[1].candidate_sha == reviewed_heads[0]
    reconstructed = _git(repo, "rev-parse", f"{reviewed_heads[0]}^")
    assert reconstructed != revert_main
    assert _git(repo, "rev-parse", f"{reconstructed}^") == revert_main
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == revert_main
