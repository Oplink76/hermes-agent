from __future__ import annotations

import json
import subprocess
from pathlib import Path

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

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        call = tuple(argv)
        self.calls.append(call)
        if call[:3] == ("git", "rev-parse", "origin/main"):
            stdout = SHA_MERGE + "\n"
        elif call[:3] == ("git", "rev-parse", "HEAD"):
            stdout = SHA_REVERT_HEAD + "\n"
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
    )

    assert result.state is ProtectedRevertState.REVERTED
    assert result.revert_merge_sha == SHA_REVERT_MERGE
    assert result.installed_sha == SHA_PREVIOUS
    assert github.created == [(f"auto-sync/revert-{SHA_MERGE[:12]}", "main")]
    assert github.merged == [(8, SHA_REVERT_HEAD)]
    assert ("git", "revert", "--no-edit", SHA_MERGE) in runner.calls
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
    )
    assert result.state is ProtectedRevertState.NEEDS_OLE
    assert github.merged == []
