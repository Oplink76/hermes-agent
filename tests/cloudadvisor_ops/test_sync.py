from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

if os.name != "nt":
    import fcntl

from ops.cloudadvisor.hermes_ops.sync import (
    CodexConflictResolver,
    SyncConfig,
    SyncState,
    run,
)


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path
    timeout: int


class FakeRunner:
    def __init__(
        self, responses: dict[tuple[str, ...], tuple[int, str, str]] | None = None
    ):
        self.responses = responses or {}
        self.calls: list[Call] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        key = tuple(argv)
        self.calls.append(Call(key, Path(cwd), timeout))
        returncode, stdout, stderr = self.responses.get(key, (0, "", ""))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeGitHub:
    def __init__(self, existing_pr: int | None = None):
        self.existing_pr = existing_pr
        self.open_pr_queries: list[tuple[str, str]] = []
        self.created_prs = 0
        self.updated_prs = 0

    def find_open_pull_request(self, head: str, base: str) -> int | None:
        self.open_pr_queries.append((head, base))
        return self.existing_pr

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str
    ) -> int:
        self.created_prs += 1
        return 41

    def update_pull_request(self, number: int, *, title: str, body: str) -> None:
        assert number == self.existing_pr
        self.updated_prs += 1


class FakeResolver:
    def __init__(self, resolved: bool):
        self.resolved = resolved
        self.calls = 0

    def resolve(self, worktree: Path, runner: FakeRunner) -> bool:
        self.calls += 1
        return self.resolved


def config(tmp_path: Path) -> SyncConfig:
    repo = tmp_path / "repo"
    worktree = tmp_path / "candidate"
    repo.mkdir()
    worktree.mkdir()
    return SyncConfig(
        repo=repo,
        worktree=worktree,
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "upstream-sync.lock",
    )


def base_responses(*, backlog: int = 3, merge_returncode: int = 0):
    return {
        ("git", "rev-parse", "origin/main"): (0, "base-sha\n", ""),
        ("git", "rev-parse", "upstream/main"): (0, "upstream-sha\n", ""),
        ("git", "rev-list", "--count", "origin/main..upstream/main"): (
            0,
            f"{backlog}\n",
            "",
        ),
        ("git", "rev-parse", "refs/remotes/origin/auto-sync/upstream"): (
            0,
            "old-candidate\n",
            "",
        ),
        ("git", "branch", "--show-current"): (0, "auto-sync/upstream\n", ""),
        ("git", "status", "--porcelain", "--untracked-files=all"): (0, "", ""),
        ("git", "merge", "--no-edit", "upstream/main"): (
            merge_returncode,
            "",
            "conflict",
        ),
        ("git", "rev-parse", "HEAD"): (0, "candidate-sha\n", ""),
        ("git", "diff", "--name-only", "origin/main...HEAD"): (
            0,
            "gateway/run.py\n",
            "",
        ),
        ("rg", "-n", "^(<<<<<<< |>>>>>>> )", "--glob", "!package-lock.json", "."): (
            1,
            "",
            "",
        ),
    }


def pushes(runner: FakeRunner) -> list[Call]:
    return [call for call in runner.calls if call.argv[:2] == ("git", "push")]


def test_no_backlog_does_not_push_or_touch_pull_requests(tmp_path: Path):
    runner = FakeRunner(base_responses(backlog=0))
    github = FakeGitHub()

    result = run(config(tmp_path), runner=runner, github=github)

    assert result.state is SyncState.NO_CHANGE
    assert pushes(runner) == []
    assert github.open_pr_queries == []


def test_clean_merge_updates_exactly_one_existing_pull_request(tmp_path: Path):
    runner = FakeRunner(base_responses())
    github = FakeGitHub(existing_pr=17)

    result = run(config(tmp_path), runner=runner, github=github)

    assert result.state is SyncState.PR_UPDATED
    assert result.pr_number == 17
    assert github.open_pr_queries == [("auto-sync/upstream", "main")]
    assert github.created_prs + github.updated_prs == 1
    assert github.updated_prs == 1
    push_calls = pushes(runner)
    assert len(push_calls) == 1
    assert "HEAD:refs/heads/auto-sync/upstream" in push_calls[0].argv
    assert (
        "--force-with-lease=refs/heads/auto-sync/upstream:old-candidate"
        in push_calls[0].argv
    )
    assert all(
        destination.split(":")[-1] != "main"
        for call in push_calls
        for destination in call.argv
    )


def test_clean_merge_creates_one_pull_request_when_none_exists(tmp_path: Path):
    runner = FakeRunner(base_responses())
    github = FakeGitHub()

    result = run(config(tmp_path), runner=runner, github=github)

    assert result.pr_number == 41
    assert github.open_pr_queries == [("auto-sync/upstream", "main")]
    assert github.created_prs + github.updated_prs == 1


def test_failed_local_gate_never_pushes_or_updates_pull_request(tmp_path: Path):
    responses = base_responses()
    responses[("git", "diff", "--check")] = (1, "", "whitespace error")
    runner = FakeRunner(responses)
    github = FakeGitHub(existing_pr=17)

    result = run(config(tmp_path), runner=runner, github=github)

    assert result.state is SyncState.VERIFY_FAILED
    assert pushes(runner) == []
    assert github.open_pr_queries == []


def test_conflict_marker_gate_reads_real_candidate_files_before_push(tmp_path: Path):
    sync_config = config(tmp_path)
    (sync_config.worktree / "conflicted.py").write_text(
        "<<<<<<< ours\nvalue = 1\n>>>>>>> theirs\n",
        encoding="utf-8",
    )
    responses = base_responses()
    responses[
        ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    ] = (0, "conflicted.py\0", "")
    runner = FakeRunner(responses)

    result = run(sync_config, runner=runner, github=FakeGitHub())

    assert result.state is SyncState.VERIFY_FAILED
    assert result.checks[-1].name == "conflict_markers"
    assert result.checks[-1].detail == "conflicted.py:1"
    assert pushes(runner) == []


def test_unresolved_merge_conflict_requires_decision_without_push(tmp_path: Path):
    runner = FakeRunner(base_responses(merge_returncode=1))
    github = FakeGitHub()
    resolver = FakeResolver(False)

    result = run(config(tmp_path), runner=runner, github=github, resolver=resolver)

    assert result.state is SyncState.NEEDS_DECISION
    assert resolver.calls == 1
    assert pushes(runner) == []


def test_command_conflict_resolver_runs_only_configured_command_in_worktree(
    tmp_path: Path,
):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    resolver = CodexConflictResolver(
        executable=Path("/usr/local/bin/codex"),
        prompt="resolve current merge conflicts",
    )
    command = (
        "/usr/local/bin/codex",
        "exec",
        "--ignore-user-config",
        "--sandbox",
        "workspace-write",
        "--ephemeral",
        "resolve current merge conflicts",
    )
    runner = FakeRunner({command: (0, "resolved", "")})

    resolved = resolver.resolve(worktree, runner)

    assert resolved is True
    assert runner.calls == [Call(command, worktree, 1800)]


def test_conflict_resolver_rejects_arbitrary_executable():
    with pytest.raises(ValueError, match="resolver"):
        CodexConflictResolver(
            executable=Path("/bin/bash"),
            prompt="git push origin HEAD:main",
        )


def test_resolved_merge_conflict_runs_gates_before_push(tmp_path: Path):
    responses = base_responses(merge_returncode=1)
    responses[("git", "rev-parse", "-q", "--verify", "MERGE_HEAD")] = (
        0,
        "upstream-sha\n",
        "",
    )
    runner = FakeRunner(responses)
    github = FakeGitHub(existing_pr=17)
    resolver = FakeResolver(True)

    result = run(config(tmp_path), runner=runner, github=github, resolver=resolver)

    assert result.state is SyncState.PR_UPDATED
    assert SyncState.CONFLICTED in result.transitions
    assert SyncState.AI_RESOLVED in result.transitions
    commit_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call.argv == ("git", "commit", "--no-edit")
    )
    head_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call.argv == ("git", "rev-parse", "HEAD")
    )
    assert commit_index < head_index
    assert len(pushes(runner)) == 1


@pytest.mark.skipif(os.name == "nt", reason="Contention fixture uses POSIX flock")
def test_concurrent_lock_contention_returns_locked_without_git_calls(tmp_path: Path):
    sync_config = config(tmp_path)
    sync_config.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_config.lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = FakeRunner(base_responses())

        result = run(sync_config, runner=runner, github=FakeGitHub())

    assert result.state is SyncState.LOCKED
    assert runner.calls == []


def test_resolver_cannot_bypass_missing_merge_head(tmp_path: Path):
    responses = base_responses(merge_returncode=1)
    responses[("git", "rev-parse", "-q", "--verify", "MERGE_HEAD")] = (
        1,
        "",
        "missing",
    )
    runner = FakeRunner(responses)

    result = run(
        config(tmp_path),
        runner=runner,
        github=FakeGitHub(),
        resolver=FakeResolver(True),
    )

    assert result.state is SyncState.NEEDS_DECISION
    assert not any(call.argv == ("git", "commit", "--no-edit") for call in runner.calls)
    assert pushes(runner) == []


def test_non_candidate_worktree_is_never_reset(tmp_path: Path):
    responses = base_responses()
    responses[("git", "branch", "--show-current")] = (0, "main\n", "")
    runner = FakeRunner(responses)

    result = run(config(tmp_path), runner=runner, github=FakeGitHub())

    assert result.state is SyncState.NEEDS_DECISION
    assert not any(call.argv[:3] == ("git", "reset", "--hard") for call in runner.calls)


def test_dirty_candidate_worktree_is_never_reset(tmp_path: Path):
    responses = base_responses()
    responses[("git", "status", "--porcelain", "--untracked-files=all")] = (
        0,
        "?? human-work.txt\n",
        "",
    )
    runner = FakeRunner(responses)

    result = run(config(tmp_path), runner=runner, github=FakeGitHub())

    assert result.state is SyncState.NEEDS_DECISION
    assert not any(call.argv[:3] == ("git", "reset", "--hard") for call in runner.calls)


def test_candidate_sha_is_fetched_before_force_with_lease(tmp_path: Path):
    runner = FakeRunner(base_responses())

    run(config(tmp_path), runner=runner, github=FakeGitHub(existing_pr=17))

    fetch = (
        "git",
        "fetch",
        "origin",
        "refs/heads/auto-sync/upstream:refs/remotes/origin/auto-sync/upstream",
    )
    fetch_index = next(
        index for index, call in enumerate(runner.calls) if call.argv == fetch
    )
    push_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call.argv[:2] == ("git", "push")
    )
    assert fetch_index < push_index
