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
    SyncClassification,
    SyncConfig,
    SyncState,
    prepare_candidate,
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
    def __init__(self, resolved: bool | list[bool]):
        self.results = [resolved] if isinstance(resolved, bool) else list(resolved)
        self.calls = 0
        self.strategies: list[object] = []
        self.last_runner_call: tuple[str, ...] | None = None

    def resolve(self, worktree: Path, runner: FakeRunner, *, strategy=None) -> bool:
        self.calls += 1
        self.strategies.append(strategy)
        self.last_runner_call = runner.calls[-1].argv if runner.calls else None
        return self.results[min(self.calls - 1, len(self.results) - 1)]


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
    responses = {
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
        ("git", "rev-parse", "HEAD^{tree}"): (0, "candidate-tree-sha\n", ""),
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
    if merge_returncode != 0:
        responses[("git", "diff", "--name-only", "--diff-filter=U", "-z")] = (
            0,
            "gateway/run.py\0",
            "",
        )
    return responses


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


def test_clean_merge_classifies_clean(tmp_path: Path):
    runner = FakeRunner(base_responses())

    result = prepare_candidate(
        config(tmp_path), runner=runner, github=FakeGitHub(existing_pr=17)
    )

    assert result.classification is SyncClassification.CLEAN


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
    assert result.conflicted_files == ("gateway/run.py",)
    assert resolver.calls == 2
    assert pushes(runner) == []


def test_command_conflict_resolver_runs_only_configured_command_in_worktree(
    tmp_path: Path,
):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    git_common_dir = tmp_path / "repo" / ".git"
    git_common_dir.mkdir(parents=True)
    resolver = CodexConflictResolver(
        executable=Path("/usr/local/bin/codex"),
        prompt="resolve current merge conflicts",
    )
    resolution_record = (
        git_common_dir / "hermes-sync-evidence" / ".hermes-sync-resolution.json"
    )
    common_dir_command = (
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    class ResolutionRunner(FakeRunner):
        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            completed = super().run(argv, cwd, timeout)
            if tuple(argv[:2]) == ("/usr/local/bin/codex", "exec"):
                resolution_record.parent.mkdir(parents=True, exist_ok=True)
                resolution_record.write_text(
                    '{"conflicts":[{"path":"gateway/run.py",'
                    '"decision":"preserve fork behavior"}],'
                    '"strategy":"preserve_fork_behavior"}',
                    encoding="utf-8",
                )
            return completed

    runner = ResolutionRunner({
        common_dir_command: (0, f"{git_common_dir}\n", ""),
    })

    resolved = resolver.resolve(worktree, runner)

    assert resolved is True
    assert runner.calls[0] == Call(common_dir_command, worktree, 300)
    command = runner.calls[1]
    assert command.argv[:7] == (
        "/usr/local/bin/codex",
        "exec",
        "--ignore-user-config",
        "--sandbox",
        "workspace-write",
        "--add-dir",
        str(git_common_dir),
    )
    assert command.argv[-2] == "--ephemeral"
    assert str(resolution_record) in command.argv[-1]
    assert "every conflicted file" in command.argv[-1]
    assert command.cwd == worktree
    assert resolver.resolution_record_path(worktree) == resolution_record


def test_command_conflict_resolver_fails_when_record_is_missing(tmp_path: Path):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    git_common_dir = tmp_path / "repo" / ".git"
    git_common_dir.mkdir(parents=True)
    resolver = CodexConflictResolver(
        executable=Path("/usr/local/bin/codex"),
        prompt="resolve current merge conflicts",
    )
    runner = FakeRunner({
        (
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ): (0, f"{git_common_dir}\n", ""),
    })

    assert resolver.resolve(worktree, runner) is False


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_command_conflict_resolver_rejects_stale_evidence_symlink(tmp_path: Path):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    git_common_dir = tmp_path / "repo" / ".git"
    evidence_dir = git_common_dir / "hermes-sync-evidence"
    evidence_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("do not delete", encoding="utf-8")
    (evidence_dir / ".hermes-sync-resolution.json").symlink_to(outside)
    resolver = CodexConflictResolver(
        executable=Path("/usr/local/bin/codex"),
        prompt="resolve current merge conflicts",
    )
    runner = FakeRunner({
        (
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ): (0, f"{git_common_dir}\n", ""),
    })

    assert resolver.resolve(worktree, runner) is False
    assert outside.read_text(encoding="utf-8") == "do not delete"
    assert len(runner.calls) == 1


def test_conflict_resolver_fails_closed_when_git_common_dir_is_unavailable(
    tmp_path: Path,
):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    resolver = CodexConflictResolver(
        executable=Path("/usr/local/bin/codex"),
        prompt="resolve current merge conflicts",
    )
    common_dir_command = (
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    runner = FakeRunner({common_dir_command: (1, "", "not a repository")})

    resolved = resolver.resolve(worktree, runner)

    assert resolved is False
    assert runner.calls == [Call(common_dir_command, worktree, 300)]


def test_conflict_resolver_fails_closed_when_git_common_dir_does_not_exist(
    tmp_path: Path,
):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    missing_common_dir = tmp_path / "missing" / ".git"
    resolver = CodexConflictResolver(
        executable=Path("/usr/local/bin/codex"),
        prompt="resolve current merge conflicts",
    )
    common_dir_command = (
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    runner = FakeRunner({common_dir_command: (0, f"{missing_common_dir}\n", "")})

    resolved = resolver.resolve(worktree, runner)

    assert resolved is False
    assert runner.calls == [Call(common_dir_command, worktree, 300)]


def test_conflict_resolver_rejects_arbitrary_executable():
    with pytest.raises(ValueError, match="resolver"):
        CodexConflictResolver(
            executable=Path("/bin/bash"),
            prompt="git push origin HEAD:main",
        )


def test_conflict_resolver_preserves_windows_executable_path():
    resolver = CodexConflictResolver(
        executable=Path("C:/Program Files/Codex/codex.exe"),
        prompt="resolve conflicts",
    )
    assert resolver.command[0] == "C:/Program Files/Codex/codex.exe"


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


def test_resolved_merge_conflict_requires_review(tmp_path: Path):
    responses = base_responses(merge_returncode=1)
    capture_command = ("git", "diff", "--name-only", "--diff-filter=U", "-z")
    responses[capture_command] = (
        0,
        "gateway/run.py\0ops/cloudadvisor/hermes_ops/sync.py\0",
        "",
    )
    responses[("git", "rev-parse", "-q", "--verify", "MERGE_HEAD")] = (
        0,
        "upstream-sha\n",
        "",
    )
    resolver = FakeResolver(True)

    result = prepare_candidate(
        config(tmp_path),
        runner=FakeRunner(responses),
        github=FakeGitHub(existing_pr=17),
        resolver=resolver,
    )

    assert (
        result.classification is SyncClassification.MINOR_REVIEW_REQUIRED
    )
    assert result.conflicted_files == (
        "gateway/run.py",
        "ops/cloudadvisor/hermes_ops/sync.py",
    )
    assert resolver.last_runner_call == capture_command


def test_second_materially_different_conflict_strategy_runs_after_isolated_reset(
    tmp_path: Path,
):
    responses = base_responses(merge_returncode=1)
    responses[("git", "rev-parse", "-q", "--verify", "MERGE_HEAD")] = (
        0,
        "upstream-sha\n",
        "",
    )
    resolver = FakeResolver([False, True])
    runner = FakeRunner(responses)

    result = prepare_candidate(
        config(tmp_path),
        runner=runner,
        github=FakeGitHub(existing_pr=17),
        resolver=resolver,
    )

    assert result.state is SyncState.PR_UPDATED
    assert result.classification is SyncClassification.MINOR_REVIEW_REQUIRED
    assert resolver.calls == 2
    assert resolver.strategies[0] != resolver.strategies[1]
    reset_calls = [call for call in runner.calls if call.argv[:3] == ("git", "reset", "--hard")]
    assert len(reset_calls) == 2
    assert any(call.argv == ("git", "clean", "-fd") for call in runner.calls)


def test_two_failed_conflict_strategies_exhaust_before_human(tmp_path: Path):
    resolver = FakeResolver([False, False])
    result = prepare_candidate(
        config(tmp_path),
        runner=FakeRunner(base_responses(merge_returncode=1)),
        github=FakeGitHub(),
        resolver=resolver,
    )
    assert result.state is SyncState.NEEDS_DECISION
    assert result.risk == "conflict_strategies_exhausted"
    assert resolver.calls == 2
    assert resolver.strategies[0] != resolver.strategies[1]


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, ""), (0, "")],
    ids=["command-failed", "empty-conflict-set"],
)
def test_conflicted_file_capture_fails_closed_before_resolver(
    tmp_path: Path,
    returncode: int,
    stdout: str,
):
    responses = base_responses(merge_returncode=1)
    responses[("git", "diff", "--name-only", "--diff-filter=U", "-z")] = (
        returncode,
        stdout,
        "capture failed",
    )
    resolver = FakeResolver(True)

    result = prepare_candidate(
        config(tmp_path),
        runner=FakeRunner(responses),
        github=FakeGitHub(),
        resolver=resolver,
    )

    assert result.state is SyncState.NEEDS_DECISION
    assert result.classification is SyncClassification.MAJOR
    assert result.risk == "conflicted_files_unavailable"
    assert resolver.calls == 0


def test_unresolved_merge_conflict_classifies_major(tmp_path: Path):
    result = prepare_candidate(
        config(tmp_path),
        runner=FakeRunner(base_responses(merge_returncode=1)),
        github=FakeGitHub(),
        resolver=FakeResolver(False),
    )

    assert result.classification is SyncClassification.MAJOR


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


@pytest.mark.skipif(os.name == "nt", reason="Contention fixture uses POSIX flock")
def test_prepare_candidate_does_not_acquire_file_lock(tmp_path: Path):
    sync_config = config(tmp_path)
    sync_config.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_config.lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = FakeRunner(base_responses())

        result = prepare_candidate(
            sync_config, runner=runner, github=FakeGitHub(existing_pr=17)
        )

    assert result.state is SyncState.PR_UPDATED
    assert runner.calls


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
