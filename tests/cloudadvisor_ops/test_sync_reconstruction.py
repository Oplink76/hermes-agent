from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

from ops.cloudadvisor.hermes_ops import sync_controller as sync_controller_module
from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.deploy import DeployConfig, DeploymentRecord, PreflightError
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
from ops.cloudadvisor.hermes_ops.sync_reconstruction import (
    reconstruct_failed_candidate,
    resume_failed_candidate_reconstruction,
)
from ops.cloudadvisor.hermes_ops.sync_reconstruction_checkpoint import (
    load_pending_reconstruction,
)
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_receipt import SyncReceiptArtifact
from ops.cloudadvisor.hermes_ops.sync_remediation import CodexCandidateRemediator
from ops.cloudadvisor.hermes_ops.sync_review import ConflictReviewReceipt


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _receipt(path: Path) -> SyncReceiptArtifact:
    return SyncReceiptArtifact(path=path, sha256="e" * 64)


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


def test_real_resume_reintroduces_failed_tree_then_merges_current_upstream(
    tmp_path: Path,
):
    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
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
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(upstream)],
        check=True,
        capture_output=True,
    )
    upstream_work = tmp_path / "upstream-work"
    subprocess.run(
        ["git", "clone", str(upstream), str(upstream_work)],
        check=True,
        capture_output=True,
    )
    _git(upstream_work, "config", "user.name", "Official Upstream")
    _git(upstream_work, "config", "user.email", "upstream@example.invalid")
    (upstream_work / "old-upstream.txt").write_text("old\n", encoding="utf-8")
    _git(upstream_work, "add", "old-upstream.txt")
    _git(upstream_work, "commit", "-m", "old upstream")
    _git(upstream_work, "push", "origin", "main")
    old_upstream = _git(upstream_work, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "upstream", str(upstream))
    _git(repo, "fetch", "upstream", "main")
    _git(repo, "switch", "-c", "auto-sync/upstream", base)
    _git(repo, "merge", "--no-ff", "upstream/main", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "push", "origin", "auto-sync/upstream")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "auto-sync/upstream", "-m", "failed merge")
    failed_merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "revert", "-m", "1", "--no-edit", failed_merge)
    revert_main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")
    _git(repo, "switch", "auto-sync/upstream")
    (repo / "state.txt").write_text("abandoned repair\n", encoding="utf-8")
    _git(repo, "commit", "-am", "abandoned repair")
    rolling_candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "auto-sync/upstream")
    (upstream_work / "new-upstream.txt").write_text("new\n", encoding="utf-8")
    _git(upstream_work, "add", "new-upstream.txt")
    _git(upstream_work, "commit", "-m", "new upstream")
    _git(upstream_work, "push", "origin", "main")
    current_upstream = _git(upstream_work, "rev-parse", "HEAD")

    class GitHub:
        def find_open_pull_request(self, head: str, base_name: str):
            return 9

        def create_pull_request(self, **kwargs):
            raise AssertionError("existing rolling PR must be updated")

        def update_pull_request(self, number: int, *, title: str, body: str):
            assert number == 9

    result = resume_failed_candidate_reconstruction(
        SyncConfig(
            repo=repo,
            worktree=tmp_path / "rolling-candidate",
            origin="origin",
            upstream="upstream",
            candidate_branch="auto-sync/upstream",
            repo_slug="Oplink76/hermes-agent",
            lock_path=tmp_path / "sync.lock",
        ),
        failed=SyncResult(
            state=SyncState.PR_UPDATED,
            base_sha=base,
            upstream_sha=old_upstream,
            candidate_sha=candidate_sha,
            candidate_tree_sha=candidate_tree,
            pr_number=7,
            classification=SyncClassification.CLEAN,
        ),
        failed_merge_sha=failed_merge,
        revert_main_sha=revert_main,
        expected_candidate_sha=rolling_candidate,
        current_upstream_sha=current_upstream,
        github=GitHub(),
        runner=SubprocessCommandRunner(),
    )

    assert result.upstream_sha == current_upstream
    assert _git(repo, "rev-parse", f"{result.candidate_sha}^1^{{tree}}") == candidate_tree
    assert _git(repo, "rev-parse", f"{result.candidate_sha}^1^") == revert_main
    assert _git(repo, "rev-parse", f"{result.candidate_sha}^2") == current_upstream
    assert _git(repo, "show", f"{result.candidate_sha}:old-upstream.txt") == "old"
    assert _git(repo, "show", f"{result.candidate_sha}:new-upstream.txt") == "new"


def test_controller_persists_then_resumes_complete_tree_across_invocations(
    tmp_path: Path,
    monkeypatch,
):
    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
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
    (repo / "feature.txt").write_text("healthy\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "-u", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(upstream)],
        check=True,
        capture_output=True,
    )
    upstream_work = tmp_path / "upstream-work"
    subprocess.run(
        ["git", "clone", str(upstream), str(upstream_work)],
        check=True,
        capture_output=True,
    )
    _git(upstream_work, "config", "user.name", "Official Upstream")
    _git(upstream_work, "config", "user.email", "upstream@example.invalid")
    (upstream_work / "feature.txt").write_text("failed upstream\n", encoding="utf-8")
    (upstream_work / "old-upstream.txt").write_text("old\n", encoding="utf-8")
    _git(upstream_work, "add", "feature.txt", "old-upstream.txt")
    _git(upstream_work, "commit", "-m", "old upstream")
    _git(upstream_work, "push", "origin", "main")
    old_upstream = _git(upstream_work, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "upstream", str(upstream))
    _git(repo, "fetch", "upstream", "main")
    _git(repo, "switch", "-c", "auto-sync/upstream", base)
    _git(repo, "merge", "--no-ff", "upstream/main", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "push", "origin", "auto-sync/upstream")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "auto-sync/upstream", "-m", "failed merge")
    failed_merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "revert", "-m", "1", "--no-edit", failed_merge)
    revert_main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")
    rolling = tmp_path / "rolling-candidate"
    _git(repo, "worktree", "add", str(rolling), "auto-sync/upstream")

    codex_driver = tmp_path / "codex_driver.py"
    codex_driver.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "Path('feature.txt').write_text('repaired behavior\\n', encoding='utf-8')\n"
        "Path('.hermes-sync-repair.json').write_text(json.dumps({"
        "'conflicts': [{'path': 'feature.txt', 'decision': 'repair runtime'}], "
        "'strategy': 'candidate_repair'}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        codex = tmp_path / "codex.cmd"
        codex.write_bytes(
            f'@"{sys.executable}" "{codex_driver}" %*\r\n'.encode("utf-8")
        )
    else:
        codex = tmp_path / "codex"
        codex.write_text(
            f"#!{sys.executable}\nexec(compile(open({str(codex_driver)!r}, 'r', encoding='utf-8').read(), "
            f"{str(codex_driver)!r}, 'exec'))\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
    checks = tuple(
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
        upstream_sha=old_upstream,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree,
        pr_number=7,
        checks=checks,
        changed_files=("feature.txt", "old-upstream.txt"),
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
    receipt_root = tmp_path / "receipts"
    install = tmp_path / "install"
    subprocess.run(
        ["git", "clone", str(origin), str(install)],
        check=True,
        capture_output=True,
    )
    _git(install, "switch", "--detach", base)
    config = AutonomousSyncConfig(
        sync=sync_config,
        deploy=DeployConfig(
            install_root=install,
            origin="origin",
            record_root=tmp_path / "deployments",
            repo_slug=sync_config.repo_slug,
            sync_receipt_root=receipt_root,
        ),
        receipt_root=receipt_root,
        required_check="All required checks pass",
        resolver_backend="codex",
    )

    class GitHub:
        expected_base_sha: str | None = None

        def __init__(self):
            self.evidence_calls = 0
            self.merge_calls = 0

        def find_open_pull_request(self, head: str, base_name: str):
            return 9

        def create_pull_request(self, **kwargs):
            raise AssertionError("rolling PR already exists")

        def update_pull_request(self, number: int, *, title: str, body: str):
            assert number == 9

        def evidence(self, pr_number: int):
            self.evidence_calls += 1
            if self.evidence_calls == 1:
                return SyncPullRequestEvidence(
                    7, "open", base, candidate_sha,
                    "All required checks pass", "success", 101, 201,
                )
            head = _git(
                repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream"
            ).split()[0]
            return SyncPullRequestEvidence(
                9, "open", revert_main, head,
                "All required checks pass", "success",
                100 + self.evidence_calls, 200 + self.evidence_calls,
            )

        def merge_exact(self, pr_number: int, *, expected_head: str):
            self.merge_calls += 1
            return failed_merge if self.merge_calls == 1 else "4" * 40

    github = GitHub()
    implementation = CodexCandidateRemediator(
        config=sync_config,
        runner=SubprocessCommandRunner(),
        executable=codex,
        prompt="Repair the exact failed candidate.",
        verify_fn=lambda worktree, runner: list(checks),
    )
    repair_calls = 0

    class Remediator:
        def retry_infrastructure(self, candidate, evidence):
            return False

        def repair_candidate(self, candidate, *, health_evidence=()):
            nonlocal repair_calls
            repair_calls += 1
            repaired = implementation.repair_candidate(
                candidate, health_evidence=health_evidence
            )
            if repair_calls == 1:
                (upstream_work / "new-upstream.txt").write_text(
                    "new\n", encoding="utf-8"
                )
                _git(upstream_work, "add", "new-upstream.txt")
                _git(upstream_work, "commit", "-m", "new upstream")
                _git(upstream_work, "push", "origin", "main")
            return repaired

    class Reviewer:
        def review(self, **kwargs):
            digest = Path(kwargs["resolution_record"]).stem.removeprefix("resolution-")
            return ConflictReviewReceipt(
                kwargs["candidate_sha"], "codex", "claude", "green", (),
                "2026-07-12T22:00:00Z", digest,
            )

    prepare_calls = 0

    def prepare(*args, **kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return failed

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.prepare_candidate", prepare
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        lambda *args, **kwargs: _receipt(tmp_path / "pre"),
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        lambda *args, **kwargs: _receipt(tmp_path / "merged"),
    )
    outcomes = [
        AutonomousSyncResult(
            AutonomousSyncState.ROLLED_BACK_REVERTED,
            fork_main_sha=revert_main,
            installed_sha=base,
        ),
        AutonomousSyncResult(
            AutonomousSyncState.DEPLOYED,
            deployed_sha="4" * 40,
        ),
    ]
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover",
        lambda *args, **kwargs: outcomes.pop(0),
    )

    first = run_autonomous_sync(
        config,
        runner=SubprocessCommandRunner(),
        github=github,
        resolver=None,
        reviewer=Reviewer(),
        remediator=Remediator(),
        deploy_fn=lambda path, sha, pr: DeploymentRecord(
            "deploy", sha, base, {}, {}, {},
            (HealthCheck("runtime", False),), "rolled_back_healthy", {},
        ),
        verify_runtime_fn=lambda sha: sha == base,
    )

    assert first.state is AutonomousSyncState.PENDING_REFRESH
    checkpoint = load_pending_reconstruction(receipt_root, repo_slug=sync_config.repo_slug)
    assert checkpoint is not None
    assert checkpoint.failed_candidate_sha == candidate_sha
    assert checkpoint.failed_candidate_tree_sha == candidate_tree
    assert checkpoint.failed_merge_sha == failed_merge
    assert checkpoint.revert_main_sha == revert_main
    assert checkpoint.previous_healthy_installed_sha == base
    assert checkpoint.target_upstream_sha == _git(upstream_work, "rev-parse", "HEAD")

    second = run_autonomous_sync(
        config,
        runner=SubprocessCommandRunner(),
        github=github,
        resolver=None,
        reviewer=Reviewer(),
        remediator=Remediator(),
        deploy_fn=lambda path, sha, pr: DeploymentRecord(
            "deploy", sha, base, {}, {}, {},
            (HealthCheck("runtime", True),), "deployed", None,
        ),
        verify_runtime_fn=lambda sha: True,
    )

    assert second.state is AutonomousSyncState.DEPLOYED
    assert prepare_calls == 1
    assert repair_calls == 2
    assert load_pending_reconstruction(receipt_root, repo_slug=sync_config.repo_slug) is None
    final_head = _git(
        repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream"
    ).split()[0]
    assert _git(repo, "show", f"{final_head}:old-upstream.txt") == "old"
    assert _git(repo, "show", f"{final_head}:new-upstream.txt") == "new"
    assert _git(repo, "show", f"{final_head}:feature.txt") == "repaired behavior"


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
    _git(repo, "push", "--force", "origin", f"{failed_merge}:main")
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
            f"#!{sys.executable}\nexec(compile(open({str(driver)!r}, encoding='utf-8').read(), "
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
    install = tmp_path / "install"
    subprocess.run(
        ["git", "clone", str(origin), str(install)],
        check=True,
        capture_output=True,
    )
    _git(install, "switch", "--detach", base)
    config = AutonomousSyncConfig(
        sync=sync_config,
        deploy=DeployConfig(
            install_root=install,
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
            if self.evidence_calls == 2:
                return SyncPullRequestEvidence(
                    7,
                    "merged",
                    base,
                    candidate_sha,
                    "All required checks pass",
                    "success",
                    101,
                    202,
                    failed_merge,
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
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller._refresh_current_upstream_sha",
        lambda *args, **kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller_execution._refresh_current_upstream_sha",
        lambda *args, **kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.resume_failed_candidate_reconstruction",
        lambda config, *, failed, failed_merge_sha, revert_main_sha, github, runner, **kwargs: reconstruct_failed_candidate(
            config,
            failed=failed,
            failed_merge_sha=failed_merge_sha,
            revert_main_sha=revert_main_sha,
            github=github,
            runner=runner,
        ),
    )
    reviews: list[object] = []
    real_write_receipt = sync_controller_module.write_sync_receipt
    real_finalize_receipt = sync_controller_module.finalize_sync_receipt

    def write_receipt(*args, **kwargs):
        reviews.append(kwargs.get("conflict_review"))
        return real_write_receipt(*args, **kwargs)

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.write_sync_receipt",
        write_receipt,
    )
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finalize_sync_receipt",
        real_finalize_receipt,
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

    def finish(*args, **kwargs):
        outcome = outcomes.pop(0)
        if outcome.state is AutonomousSyncState.ROLLED_BACK_REVERTED:
            _git(repo, "push", "--force", "origin", f"{revert_main}:main")
        return outcome

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_controller.finish_or_recover",
        finish,
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

    github = GitHub()
    interrupted = run_autonomous_sync(
        config,
        runner=SubprocessCommandRunner(),
        github=github,
        resolver=None,
        reviewer=Reviewer(),
        remediator=Remediator(),
        deploy_fn=lambda *args: (_ for _ in ()).throw(
            PreflightError("simulated crash before resumed deployment")
        ),
        verify_runtime_fn=lambda sha: True,
    )
    assert interrupted.state is AutonomousSyncState.NEEDS_OLE
    assert (
        _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
        == failed_merge
    )

    recovered = run_autonomous_sync(
        config,
        runner=SubprocessCommandRunner(),
        github=github,
        resolver=None,
        reviewer=Reviewer(),
        remediator=Remediator(),
        deploy_fn=lambda *args: deployments.pop(0),
        verify_runtime_fn=lambda sha: True,
    )
    assert recovered.state is AutonomousSyncState.ROLLED_BACK_REVERTED, (
        recovered.reason,
        recovered.reason_code,
        recovered.failed_gate,
    )

    result = run_autonomous_sync(
        config,
        runner=SubprocessCommandRunner(),
        github=github,
        resolver=None,
        reviewer=Reviewer(),
        remediator=Remediator(),
        deploy_fn=lambda *args: deployments.pop(0),
        verify_runtime_fn=lambda sha: True,
    )

    assert result.state is AutonomousSyncState.DEPLOYED, (
        result.reason,
        result.reason_code,
        result.failed_gate,
    )
    assert len(reviewed_heads) == 1
    assert reviews[0] is None
    assert reviews[1].candidate_sha == reviewed_heads[0]
    reconstructed = _git(repo, "rev-parse", f"{reviewed_heads[0]}^")
    assert reconstructed != revert_main
    assert _git(repo, "rev-parse", f"{reconstructed}^") == revert_main
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == revert_main
