from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.deploy import DeploymentRecord
from ops.cloudadvisor.hermes_ops.sync import SyncClassification, SyncResult, SyncState
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_recovery import (
    ProtectedRevertState,
    failed_candidate_fingerprint,
    is_quarantined,
    quarantine_candidate,
    run_protected_revert,
)


SHA_PREVIOUS = "1" * 40
SHA_UPSTREAM = "2" * 40
SHA_CANDIDATE = "3" * 40
SHA_MERGE = "4" * 40
SHA_REVERT_HEAD = "5" * 40
SHA_REVERT_MERGE = "6" * 40
SHA_CANDIDATE_TREE = "7" * 40
SHA_PREVIOUS_TREE = "8" * 40


def candidate() -> SyncResult:
    return SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=SHA_PREVIOUS,
        upstream_sha=SHA_UPSTREAM,
        candidate_sha=SHA_CANDIDATE,
        candidate_tree_sha=SHA_CANDIDATE_TREE,
        pr_number=7,
        classification=SyncClassification.CLEAN,
    )


class Runner:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.main_reads = 0

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        call = tuple(argv)
        self.calls.append(call)
        if call[:3] == ("git", "rev-parse", "origin/main"):
            self.main_reads += 1
            stdout = (
                SHA_MERGE + "\n"
                if self.main_reads == 1
                else SHA_REVERT_MERGE + "\n"
            )
        elif call[:3] == ("git", "rev-parse", "HEAD"):
            stdout = SHA_REVERT_HEAD + "\n"
        elif call[:3] == ("git", "rev-list", "--parents"):
            stdout = f"{SHA_MERGE} {SHA_PREVIOUS} {SHA_CANDIDATE}\n"
        elif call[:3] == ("git", "rev-parse", f"{SHA_PREVIOUS}^{{tree}}"):
            stdout = SHA_PREVIOUS_TREE + "\n"
        elif call[:3] == ("git", "rev-parse", f"{SHA_REVERT_HEAD}^{{tree}}"):
            stdout = SHA_PREVIOUS_TREE + "\n"
        elif call[:3] == ("git", "rev-parse", f"{SHA_REVERT_MERGE}^{{tree}}"):
            stdout = SHA_PREVIOUS_TREE + "\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class GitHub:
    def __init__(self, conclusion: str = "success"):
        self.conclusion = conclusion
        self.created: list[tuple[str, str]] = []
        self.merged: list[tuple[int, str]] = []

    def find_open_pull_request(self, head: str, base: str):
        return None

    def create_pull_request(self, *, head: str, base: str, title: str, body: str):
        self.created.append((head, base))
        return 8

    def evidence(self, pr_number: int):
        return SyncPullRequestEvidence(
            number=8,
            state="open",
            base_sha=SHA_MERGE,
            head_sha=SHA_REVERT_HEAD,
            required_check="All required checks pass",
            required_check_conclusion=self.conclusion,
            workflow_run_id=101,
            required_check_run_id=202,
        )

    def merge_exact(self, pr_number: int, *, expected_head: str):
        self.merged.append((pr_number, expected_head))
        return SHA_REVERT_MERGE


def deployment(status: str) -> DeploymentRecord:
    return DeploymentRecord(
        id="record",
        requested_sha=SHA_MERGE,
        previous_sha=SHA_PREVIOUS,
        snapshot={},
        runtime_before={},
        runtime_after={},
        checks=(),
        status=status,
        rollback={"status": status},
    )


def test_quarantine_is_exact_to_upstream_and_candidate(tmp_path: Path):
    fingerprint = failed_candidate_fingerprint(candidate())
    artifact = quarantine_candidate(tmp_path, candidate(), merge_sha=SHA_MERGE)

    assert artifact.name == f"{fingerprint}.json"
    assert is_quarantined(tmp_path, candidate())
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["candidate_sha"] == SHA_CANDIDATE
    assert payload["upstream_sha"] == SHA_UPSTREAM
    assert payload["merge_sha"] == SHA_MERGE

    changed_commit_same_content = candidate().__class__(
        **{**candidate().__dict__, "candidate_sha": "9" * 40}
    )
    assert is_quarantined(tmp_path, changed_commit_same_content)

    changed_content = candidate().__class__(
        **{**candidate().__dict__, "candidate_tree_sha": "8" * 40}
    )
    assert not is_quarantined(tmp_path, changed_content)


def test_healthy_rollback_creates_green_exact_revert_without_direct_main_push(
    tmp_path: Path,
):
    runner = Runner()
    github = GitHub()
    result = run_protected_revert(
        repo=tmp_path / "repo",
        origin="origin",
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        candidate=candidate(),
        merge_sha=SHA_MERGE,
        deployment=deployment("rolled_back_healthy"),
        quarantine_root=tmp_path / "quarantine",
        runner=runner,
        github=github,
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
        timeout_seconds=10,
        poll_interval_seconds=1,
        verify_runtime_fn=lambda sha: sha == SHA_PREVIOUS,
    )

    assert result.state is ProtectedRevertState.REVERTED
    assert result.revert_merge_sha == SHA_REVERT_MERGE
    assert result.installed_sha == SHA_PREVIOUS
    assert github.created == [(f"auto-sync/revert-{SHA_MERGE[:12]}", "main")]
    assert github.merged == [(8, SHA_REVERT_HEAD)]
    assert ("git", "revert", "-m", "1", "--no-edit", SHA_MERGE) in runner.calls
    pushes = [call for call in runner.calls if call[:2] == ("git", "push")]
    assert pushes == [
        (
            "git",
            "push",
            "origin",
            f"HEAD:refs/heads/auto-sync/revert-{SHA_MERGE[:12]}",
        )
    ]
    assert all("refs/heads/main" not in call for call in pushes)


def test_failed_rollback_stops_without_revert_pr(tmp_path: Path):
    runner = Runner()
    github = GitHub()
    result = run_protected_revert(
        repo=tmp_path / "repo",
        origin="origin",
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        candidate=candidate(),
        merge_sha=SHA_MERGE,
        deployment=deployment("rollback_failed"),
        quarantine_root=tmp_path / "quarantine",
        runner=runner,
        github=github,
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
        timeout_seconds=10,
        poll_interval_seconds=1,
        verify_runtime_fn=lambda sha: sha == SHA_PREVIOUS,
    )
    assert result.state is ProtectedRevertState.NEEDS_OLE
    assert github.created == []
    assert runner.calls == []


def test_revert_check_failure_stops_before_merge(tmp_path: Path):
    github = GitHub(conclusion="failure")
    result = run_protected_revert(
        repo=tmp_path / "repo",
        origin="origin",
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        candidate=candidate(),
        merge_sha=SHA_MERGE,
        deployment=deployment("rolled_back_healthy"),
        quarantine_root=tmp_path / "quarantine",
        runner=Runner(),
        github=github,
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
        timeout_seconds=10,
        poll_interval_seconds=1,
        verify_runtime_fn=lambda sha: sha == SHA_PREVIOUS,
    )
    assert result.state is ProtectedRevertState.NEEDS_OLE
    assert github.merged == []


def test_mismatched_deployment_record_stops_before_recovery(tmp_path: Path):
    record = deployment("rolled_back_healthy")
    record = record.__class__(**{**record.__dict__, "requested_sha": "9" * 40})
    runner = Runner()
    github = GitHub()
    result = run_protected_revert(
        repo=tmp_path / "repo",
        origin="origin",
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        candidate=candidate(),
        merge_sha=SHA_MERGE,
        deployment=record,
        quarantine_root=tmp_path / "quarantine",
        runner=runner,
        github=github,
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
        timeout_seconds=10,
        poll_interval_seconds=1,
        verify_runtime_fn=lambda sha: True,
    )
    assert result.state is ProtectedRevertState.NEEDS_OLE
    assert runner.calls == []
    assert github.created == []


def _git(cwd: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def test_real_merge_revert_restores_previous_tree_and_runtime(tmp_path: Path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", origin], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", origin, repo], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "state.txt").write_text("healthy\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "candidate")
    (repo / "state.txt").write_text("failed candidate\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "candidate", "-m", "failed sync merge")
    merge_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")

    class RealGitHub:
        def __init__(self):
            self.branch: str | None = None
            self.head: str | None = None

        def find_open_pull_request(self, head: str, base_name: str):
            return None

        def create_pull_request(self, *, head: str, base: str, title: str, body: str):
            self.branch = head
            self.head = _git(repo, "ls-remote", "origin", f"refs/heads/{head}").split()[0]
            return 8

        def evidence(self, pr_number: int):
            _git(repo, "fetch", "origin", "main")
            return SyncPullRequestEvidence(
                number=8,
                state="open",
                base_sha=_git(repo, "rev-parse", "origin/main"),
                head_sha=self.head,
                required_check="All required checks pass",
                required_check_conclusion="success",
                workflow_run_id=101,
                required_check_run_id=202,
            )

        def merge_exact(self, pr_number: int, *, expected_head: str):
            admin = tmp_path / "admin"
            subprocess.run(["git", "clone", origin, admin], check=True, capture_output=True)
            _git(admin, "config", "user.name", "Hermes Test")
            _git(admin, "config", "user.email", "hermes@example.invalid")
            _git(admin, "switch", "main")
            _git(admin, "fetch", "origin", self.branch)
            assert _git(admin, "rev-parse", "FETCH_HEAD") == expected_head
            _git(admin, "merge", "--no-ff", "FETCH_HEAD", "-m", "protected revert")
            merged = _git(admin, "rev-parse", "HEAD")
            _git(admin, "push", "origin", "main")
            return merged

    runtime_checks: list[str] = []
    result = run_protected_revert(
        repo=repo,
        origin="origin",
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        candidate=SyncResult(
            state=SyncState.PR_UPDATED,
            base_sha=base,
            upstream_sha="a" * 40,
            candidate_sha=candidate_sha,
            candidate_tree_sha=candidate_tree,
            pr_number=7,
            classification=SyncClassification.CLEAN,
        ),
        merge_sha=merge_sha,
        deployment=DeploymentRecord(
            id="record",
            requested_sha=merge_sha,
            previous_sha=base,
            snapshot={},
            runtime_before={},
            runtime_after={},
            checks=(),
            status="rolled_back_healthy",
            rollback={"status": "rolled_back_healthy"},
        ),
        quarantine_root=tmp_path / "quarantine",
        runner=SubprocessCommandRunner(),
        github=RealGitHub(),
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
        timeout_seconds=10,
        poll_interval_seconds=1,
        verify_runtime_fn=lambda sha: runtime_checks.append(sha) is None,
    )
    assert result.state is ProtectedRevertState.REVERTED
    _git(repo, "fetch", "origin", "main")
    assert _git(repo, "rev-parse", "origin/main") == result.revert_merge_sha
    assert _git(repo, "rev-parse", "origin/main^{tree}") == _git(
        repo, "rev-parse", f"{base}^{{tree}}"
    )
    assert runtime_checks == [base]
