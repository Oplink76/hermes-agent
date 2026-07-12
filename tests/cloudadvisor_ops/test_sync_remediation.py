from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.sync import (
    CheckResult,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
)
from ops.cloudadvisor.hermes_ops.sync_remediation import (
    CodexCandidateRemediator,
    GhActionsRemediator,
)


HEAD = "a" * 40


class Runner:
    def __init__(self, *, failed_step: str):
        self.failed_step = failed_step
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        call = tuple(argv)
        self.calls.append(call)
        if call[1:3] == ("run", "list"):
            payload = [
                {
                    "databaseId": 99,
                    "headSha": HEAD,
                    "status": "completed",
                    "conclusion": "failure",
                    "workflowName": "CI",
                }
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if call[1:3] == ("run", "view"):
            payload = {
                "databaseId": 99,
                "headSha": HEAD,
                "status": "completed",
                "conclusion": "failure",
                "jobs": [
                    {
                        "name": "All required checks pass",
                        "conclusion": "failure",
                        "steps": [
                            {
                                "name": self.failed_step,
                                "conclusion": "failure",
                            }
                        ],
                    }
                ],
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def candidate() -> SyncResult:
    return SyncResult(state=SyncState.PR_UPDATED, candidate_sha=HEAD, pr_number=7)


def test_exact_infrastructure_failure_is_retried_once(tmp_path: Path):
    runner = Runner(failed_step="Set up job")
    remediator = GhActionsRemediator(
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        runner=runner,
        cwd=tmp_path,
        gh_executable=Path("C:/Program Files/GitHub CLI/gh.exe"),
    )

    assert remediator.retry_infrastructure(candidate()) is True
    reruns = [call for call in runner.calls if call[1:3] == ("run", "rerun")]
    assert reruns == [
        (
            "C:/Program Files/GitHub CLI/gh.exe",
            "run",
            "rerun",
            "99",
            "--repo",
            "Oplink76/hermes-agent",
            "--failed",
        )
    ]


def test_candidate_failure_is_never_retried_as_infrastructure(tmp_path: Path):
    runner = Runner(failed_step="Run tests")
    remediator = GhActionsRemediator(
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        runner=runner,
        cwd=tmp_path,
        gh_executable=Path("/usr/local/bin/gh"),
    )

    assert remediator.retry_infrastructure(candidate()) is False
    assert not any(call[1:3] == ("run", "rerun") for call in runner.calls)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def test_real_candidate_repair_uses_detached_worktree_and_exact_branch_lease(
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
    _git(repo, "config", "user.email", "sync@example.invalid")
    _git(repo, "config", "user.name", "Hermes Sync")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "main")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "auto-sync/upstream")
    (repo / "upstream.txt").write_text("upstream\n", encoding="utf-8")
    _git(repo, "add", "upstream.txt")
    _git(repo, "commit", "-m", "candidate")
    old_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "auto-sync/upstream")

    codex = tmp_path / "codex"
    codex.write_text(
        "#!/bin/sh\nprintf 'repaired\\n' > repaired.txt\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    verify_calls: list[Path] = []

    def verify(worktree: Path, runner) -> list[CheckResult]:
        verify_calls.append(worktree)
        assert worktree != repo
        return [
            CheckResult(name, "passed")
            for name in (
                "diff_check",
                "unmerged_index",
                "conflict_markers",
                "compileall",
                "tests",
            )
        ]

    sync_config = SyncConfig(
        repo=repo,
        worktree=tmp_path / "rolling-candidate",
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "sync.lock",
    )
    remediator = CodexCandidateRemediator(
        config=sync_config,
        runner=SubprocessCommandRunner(),
        executable=codex,
        prompt="Repair the failing exact candidate.",
        verify_fn=verify,
    )
    result = remediator.repair_candidate(
        SyncResult(
            state=SyncState.PR_UPDATED,
            base_sha=base_sha,
            upstream_sha="b" * 40,
            candidate_sha=old_head,
            candidate_tree_sha=_git(repo, "rev-parse", "HEAD^{tree}"),
            pr_number=7,
            classification=SyncClassification.CLEAN,
        )
    )

    assert result is not None
    assert result.candidate_sha != old_head
    assert result.checks[-1].name == "tests"
    assert len(verify_calls) == 1
    assert (
        _git(repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream").split()[0]
        == result.candidate_sha
    )
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == base_sha
    assert "hermes-sync-repair-" not in _git(repo, "worktree", "list")
