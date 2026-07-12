from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence


HEAD = "a" * 40
WORKFLOW_RUN_ID = 99
CHECK_RUN_ID = 123


class Runner:
    def __init__(self, *, failed_step: str):
        self.failed_step = failed_step
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        call = tuple(argv)
        self.calls.append(call)
        if call[1:3] == ("run", "view"):
            payload = {
                "databaseId": WORKFLOW_RUN_ID,
                "headSha": HEAD,
                "status": "completed",
                "conclusion": "failure",
                "jobs": [
                    {
                        "databaseId": CHECK_RUN_ID,
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


def failed_evidence() -> SyncPullRequestEvidence:
    return SyncPullRequestEvidence(
        number=7,
        state="open",
        base_sha="b" * 40,
        head_sha=HEAD,
        required_check="All required checks pass",
        required_check_conclusion="failure",
        workflow_run_id=WORKFLOW_RUN_ID,
        required_check_run_id=CHECK_RUN_ID,
    )


def test_exact_infrastructure_failure_is_retried_once(tmp_path: Path):
    runner = Runner(failed_step="Set up job")
    executable = tmp_path / "Program Files" / "GitHub CLI" / (
        "gh.exe" if os.name == "nt" else "gh"
    )
    remediator = GhActionsRemediator(
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        runner=runner,
        cwd=tmp_path,
        gh_executable=executable,
    )

    assert remediator.retry_infrastructure(candidate(), failed_evidence()) is True
    assert not any(call[1:3] == ("run", "list") for call in runner.calls)
    reruns = [call for call in runner.calls if call[1:3] == ("run", "rerun")]
    assert reruns == [
        (
            str(executable),
            "run",
            "rerun",
            str(WORKFLOW_RUN_ID),
            "--repo",
            "Oplink76/hermes-agent",
            "--failed",
        )
    ]


def test_candidate_failure_is_never_retried_as_infrastructure(tmp_path: Path):
    runner = Runner(failed_step="Run tests")
    executable = tmp_path / "bin" / ("gh.exe" if os.name == "nt" else "gh")
    remediator = GhActionsRemediator(
        repo_slug="Oplink76/hermes-agent",
        required_check="All required checks pass",
        runner=runner,
        cwd=tmp_path,
        gh_executable=executable,
    )

    assert remediator.retry_infrastructure(candidate(), failed_evidence()) is False
    assert not any(call[1:3] == ("run", "rerun") for call in runner.calls)


@pytest.mark.parametrize("name", ["codex", "codex.exe", "codex.cmd"])
def test_candidate_remediator_accepts_canonical_executable_names(
    tmp_path: Path, name: str
):
    config = SyncConfig(
        repo=tmp_path / "repo",
        worktree=tmp_path / "candidate",
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "sync.lock",
    )
    remediator = CodexCandidateRemediator(
        config=config,
        runner=SubprocessCommandRunner(),
        executable=tmp_path / "Program Files" / "Codex" / name,
        prompt="repair",
    )

    assert remediator.executable.name == name


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _repair_fixture(
    tmp_path: Path, *, record_paths: tuple[str, ...] = ("repaired.txt",)
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

    driver = tmp_path / "fake_codex.py"
    repair_payload = json.dumps(
        {
            "conflicts": [
                {"path": path, "decision": "repair exact candidate"}
                for path in record_paths
            ],
            "strategy": "candidate_repair",
        }
    )
    driver.write_text(
        "\n".join([
            "from pathlib import Path",
            "Path('repaired.txt').write_text('repaired\\n', encoding='utf-8')",
            "Path('.hermes-sync-repair.json').write_text("
            f"{repair_payload!r}, encoding='utf-8')",
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
    value = SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=base_sha,
        upstream_sha="b" * 40,
        candidate_sha=old_head,
        candidate_tree_sha=_git(repo, "rev-parse", "HEAD^{tree}"),
        pr_number=7,
        classification=SyncClassification.CLEAN,
        changed_files=("upstream.txt",),
    )
    return repo, base_sha, codex, sync_config, value, verify, verify_calls


def test_real_candidate_repair_uses_detached_worktree_and_exact_branch_lease(
    tmp_path: Path,
):
    repo, base_sha, codex, sync_config, value, verify, verify_calls = _repair_fixture(
        tmp_path
    )
    remediator = CodexCandidateRemediator(
        config=sync_config,
        runner=SubprocessCommandRunner(),
        executable=codex,
        prompt="Repair the failing exact candidate.",
        verify_fn=verify,
    )
    result = remediator.repair_candidate(value)

    assert result is not None
    assert result.candidate_sha != value.candidate_sha
    assert result.classification is SyncClassification.MINOR_REVIEW_REQUIRED
    assert result.resolution_record is not None
    assert result.conflicted_files == ("repaired.txt",)
    assert result.checks[-1].name == "tests"
    assert len(verify_calls) == 1
    assert (
        _git(repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream").split()[0]
        == result.candidate_sha
    )
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == base_sha
    assert "hermes-sync-repair-" not in _git(repo, "worktree", "list")


@pytest.mark.parametrize(
    "record_paths",
    [
        ("upstream.txt",),
        ("repaired.txt", "upstream.txt"),
        ("repaired.txt", "repaired.txt"),
    ],
)
def test_candidate_repair_rejects_nonexact_actual_diff_evidence(
    tmp_path: Path, record_paths: tuple[str, ...]
):
    repo, _, codex, sync_config, value, verify, _ = _repair_fixture(
        tmp_path, record_paths=record_paths
    )
    result = CodexCandidateRemediator(
        config=sync_config,
        runner=SubprocessCommandRunner(),
        executable=codex,
        prompt="Repair the failing exact candidate.",
        verify_fn=verify,
    ).repair_candidate(value)

    assert result is None
    assert (
        _git(repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream").split()[0]
        == value.candidate_sha
    )


@pytest.mark.parametrize(
    "failed_prefix",
    [
        ("git", "reset", "--"),
        ("git", "diff", "--name-only"),
    ],
)
def test_candidate_repair_fails_closed_when_evidence_git_command_fails(
    tmp_path: Path, failed_prefix: tuple[str, ...]
):
    repo, _, codex, sync_config, value, verify, _ = _repair_fixture(tmp_path)

    class FailingRunner(SubprocessCommandRunner):
        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            if tuple(argv[: len(failed_prefix)]) == failed_prefix:
                return subprocess.CompletedProcess(argv, 1, "", "injected")
            return super().run(argv, cwd, timeout)

    result = CodexCandidateRemediator(
        config=sync_config,
        runner=FailingRunner(),
        executable=codex,
        prompt="Repair the failing exact candidate.",
        verify_fn=verify,
    ).repair_candidate(value)

    assert result is None
    assert _git(repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream").split()[0] == value.candidate_sha
