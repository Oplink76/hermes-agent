"""Idempotent, PR-only upstream synchronization state machine."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from hermes_constants import get_hermes_home

from .command import CommandRunner, SubprocessCommandRunner
from .locking import try_exclusive_file_lock


FIXED_CANDIDATE_BRANCH = "auto-sync/upstream"


class SyncState(str, Enum):
    LOCKED = "LOCKED"
    FETCHED = "FETCHED"
    NO_CHANGE = "NO_CHANGE"
    CANDIDATE_RESET = "CANDIDATE_RESET"
    MERGED_CLEAN = "MERGED_CLEAN"
    CONFLICTED = "CONFLICTED"
    AI_RESOLVED = "AI_RESOLVED"
    NEEDS_DECISION = "NEEDS_DECISION"
    VERIFIED = "VERIFIED"
    VERIFY_FAILED = "VERIFY_FAILED"
    PUSHED = "PUSHED"
    PR_UPDATED = "PR_UPDATED"


class SyncClassification(str, Enum):
    CLEAN = "clean"
    MINOR_REVIEW_REQUIRED = "minor_review_required"
    MINOR_RESOLVED = "minor_resolved"
    MAJOR = "major"


@dataclass(frozen=True)
class SyncConfig:
    repo: Path
    worktree: Path
    origin: str
    upstream: str
    candidate_branch: str
    repo_slug: str
    lock_path: Path = field(
        default_factory=lambda: get_hermes_home() / "locks" / "upstream-sync.lock"
    )

    def __post_init__(self) -> None:
        if self.candidate_branch != FIXED_CANDIDATE_BRANCH:
            raise ValueError(
                f"candidate_branch must be {FIXED_CANDIDATE_BRANCH!r}, "
                f"got {self.candidate_branch!r}"
            )


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class SyncResult:
    state: SyncState
    base_sha: str | None = None
    upstream_sha: str | None = None
    candidate_sha: str | None = None
    pr_number: int | None = None
    checks: tuple[CheckResult, ...] = ()
    risk: str = "unknown"
    changed_files: tuple[str, ...] = ()
    transitions: tuple[SyncState, ...] = ()
    classification: SyncClassification = SyncClassification.MAJOR


class GitHubPullRequests(Protocol):
    def find_open_pull_request(self, head: str, base: str) -> int | None: ...

    def create_pull_request(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> int: ...

    def update_pull_request(self, number: int, *, title: str, body: str) -> None: ...


class ConflictResolver(Protocol):
    def resolve(self, worktree: Path, runner: CommandRunner) -> bool: ...


@dataclass(frozen=True)
class CodexConflictResolver:
    """Run Codex with fixed ephemeral, no-user-config workspace isolation."""

    executable: Path
    prompt: str

    def __post_init__(self) -> None:
        if self.executable.name not in {"codex", "codex.exe"}:
            raise ValueError("conflict resolver must use the Codex executable")
        if not self.prompt.strip():
            raise ValueError("conflict resolver prompt must not be empty")

    @property
    def command(self) -> tuple[str, ...]:
        return (
            str(self.executable),
            "exec",
            "--ignore-user-config",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            self.prompt,
        )

    def resolve(self, worktree: Path, runner: CommandRunner) -> bool:
        git_common_dir = runner.run(
            [
                "git",
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            cwd=worktree,
        )
        common_dir = _output(git_common_dir)
        common_dir_path = Path(common_dir)
        if (
            git_common_dir.returncode != 0
            or not common_dir
            or not common_dir_path.is_absolute()
            or not common_dir_path.is_dir()
        ):
            return False
        command = list(self.command)
        command[5:5] = ["--add-dir", str(common_dir_path)]
        completed = runner.run(command, cwd=worktree, timeout=1800)
        return completed.returncode == 0


def _run(
    runner: CommandRunner,
    argv: list[str],
    cwd: Path,
    *,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return runner.run(argv, cwd=cwd, timeout=timeout)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "").strip()


def _result(
    state: SyncState,
    transitions: list[SyncState],
    *,
    base_sha: str | None = None,
    upstream_sha: str | None = None,
    candidate_sha: str | None = None,
    pr_number: int | None = None,
    checks: list[CheckResult] | None = None,
    risk: str = "unknown",
    changed_files: tuple[str, ...] = (),
    classification: SyncClassification = SyncClassification.MAJOR,
) -> SyncResult:
    return SyncResult(
        state=state,
        base_sha=base_sha,
        upstream_sha=upstream_sha,
        candidate_sha=candidate_sha,
        pr_number=pr_number,
        checks=tuple(checks or ()),
        risk=risk,
        changed_files=changed_files,
        transitions=tuple(transitions),
        classification=classification,
    )


def _conflict_marker_check(worktree: Path, runner: CommandRunner) -> CheckResult:
    listed = _run(
        runner,
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        worktree,
    )
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()
        return CheckResult("conflict_markers", "failed", detail)
    for relative in (listed.stdout or "").split("\0"):
        if not relative or Path(relative).name == "package-lock.json":
            continue
        path = worktree / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if line.startswith(("<<<<<<< ", ">>>>>>> ")):
                        return CheckResult(
                            "conflict_markers",
                            "failed",
                            f"{relative}:{line_number}",
                        )
        except OSError as exc:
            return CheckResult("conflict_markers", "failed", str(exc))
    return CheckResult("conflict_markers", "passed")


def _verify(worktree: Path, runner: CommandRunner) -> list[CheckResult]:
    checks: list[CheckResult] = []
    commands = [
        ("diff_check", ["git", "diff", "--check"], 300),
        ("unmerged_index", ["git", "ls-files", "-u"], 300),
    ]
    for name, argv, timeout in commands:
        completed = _run(runner, argv, worktree, timeout=timeout)
        detail = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode != 0:
            checks.append(CheckResult(name=name, status="failed", detail=detail))
            return checks
        if name == "unmerged_index" and _output(completed):
            checks.append(
                CheckResult(
                    name=name, status="failed", detail="unmerged index entries remain"
                )
            )
            return checks
        checks.append(CheckResult(name=name, status="passed"))

    marker_check = _conflict_marker_check(worktree, runner)
    checks.append(marker_check)
    if marker_check.status != "passed":
        return checks

    bash = shutil.which("bash")
    if bash is None:
        checks.append(
            CheckResult(
                "tests",
                "failed",
                "bash is required to run the canonical scripts/run_tests.sh wrapper",
            )
        )
        return checks
    commands = [
        (
            "compileall",
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "hermes_cli",
                "gateway",
                "agent",
                "tools",
                "cron",
            ],
            600,
        ),
        (
            "tests",
            [
                bash,
                "scripts/run_tests.sh",
                "tests/hermes_cli/test_kanban_db.py",
                "tests/hermes_cli/test_update_autostash.py",
                "tests/hermes_cli/test_update_venv_health.py",
                "tests/cloudadvisor_ops",
                "-q",
            ],
            1800,
        ),
    ]
    for name, argv, timeout in commands:
        completed = _run(runner, argv, worktree, timeout=timeout)
        detail = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode != 0:
            checks.append(CheckResult(name=name, status="failed", detail=detail))
            return checks
        checks.append(CheckResult(name=name, status="passed"))
    return checks


def _push_candidate(
    config: SyncConfig,
    runner: CommandRunner,
    previous_candidate_sha: str,
) -> subprocess.CompletedProcess[str]:
    destination = f"HEAD:refs/heads/{FIXED_CANDIDATE_BRANCH}"
    if destination.split(":", maxsplit=1)[1] == "refs/heads/main":
        raise RuntimeError("refusing to push to main")
    lease = f"--force-with-lease=refs/heads/{FIXED_CANDIDATE_BRANCH}:{previous_candidate_sha}"
    return _run(
        runner,
        ["git", "push", config.origin, destination, lease],
        config.worktree,
        timeout=600,
    )


def prepare_candidate(
    config: SyncConfig,
    *,
    github: GitHubPullRequests,
    runner: CommandRunner,
    resolver: ConflictResolver | None = None,
) -> SyncResult:
    """Prepare and push the rolling PR; the caller owns ``config.lock_path``."""
    transitions: list[SyncState] = []

    for remote in (config.origin, config.upstream):
        fetched = _run(
            runner, ["git", "fetch", remote, "main"], config.repo, timeout=600
        )
        if fetched.returncode != 0:
            return _result(SyncState.NEEDS_DECISION, transitions)
    transitions.append(SyncState.FETCHED)

    base_result = _run(
        runner, ["git", "rev-parse", f"{config.origin}/main"], config.repo
    )
    upstream_result = _run(
        runner,
        ["git", "rev-parse", f"{config.upstream}/main"],
        config.repo,
    )
    backlog_result = _run(
        runner,
        [
            "git",
            "rev-list",
            "--count",
            f"{config.origin}/main..{config.upstream}/main",
        ],
        config.repo,
    )
    if any(
        result.returncode != 0
        for result in (base_result, upstream_result, backlog_result)
    ):
        return _result(SyncState.NEEDS_DECISION, transitions)
    base_sha = _output(base_result)
    upstream_sha = _output(upstream_result)
    backlog = int(_output(backlog_result))
    if backlog == 0:
        transitions.append(SyncState.NO_CHANGE)
        return _result(
            SyncState.NO_CHANGE,
            transitions,
            base_sha=base_sha,
            upstream_sha=upstream_sha,
            risk="none",
        )

    _run(
        runner,
        [
            "git",
            "fetch",
            config.origin,
            (
                f"refs/heads/{FIXED_CANDIDATE_BRANCH}:"
                f"refs/remotes/{config.origin}/{FIXED_CANDIDATE_BRANCH}"
            ),
        ],
        config.repo,
        timeout=600,
    )
    candidate_ref = f"refs/remotes/{config.origin}/{FIXED_CANDIDATE_BRANCH}"
    previous_result = _run(
        runner,
        ["git", "rev-parse", candidate_ref],
        config.repo,
    )
    previous_candidate_sha = (
        _output(previous_result) if previous_result.returncode == 0 else ""
    )

    branch_result = _run(
        runner,
        ["git", "branch", "--show-current"],
        config.worktree,
    )
    status_result = _run(
        runner,
        ["git", "status", "--porcelain", "--untracked-files=all"],
        config.worktree,
    )
    if (
        branch_result.returncode != 0
        or _output(branch_result) != FIXED_CANDIDATE_BRANCH
        or status_result.returncode != 0
        or _output(status_result)
    ):
        transitions.append(SyncState.NEEDS_DECISION)
        return _result(
            SyncState.NEEDS_DECISION,
            transitions,
            base_sha=base_sha,
            upstream_sha=upstream_sha,
            risk="candidate_worktree_not_disposable",
        )

    reset = _run(
        runner,
        ["git", "reset", "--hard", f"{config.origin}/main"],
        config.worktree,
    )
    if reset.returncode != 0:
        return _result(
            SyncState.NEEDS_DECISION,
            transitions,
            base_sha=base_sha,
            upstream_sha=upstream_sha,
        )
    transitions.append(SyncState.CANDIDATE_RESET)

    merged = _run(
        runner,
        ["git", "merge", "--no-edit", f"{config.upstream}/main"],
        config.worktree,
        timeout=900,
    )
    if merged.returncode == 0:
        transitions.append(SyncState.MERGED_CLEAN)
        classification = SyncClassification.CLEAN
    else:
        transitions.append(SyncState.CONFLICTED)
        if resolver is None or not resolver.resolve(config.worktree, runner):
            transitions.append(SyncState.NEEDS_DECISION)
            return _result(
                SyncState.NEEDS_DECISION,
                transitions,
                base_sha=base_sha,
                upstream_sha=upstream_sha,
                risk="conflict",
            )
        unmerged = _run(runner, ["git", "ls-files", "-u"], config.worktree)
        if unmerged.returncode != 0 or _output(unmerged):
            transitions.append(SyncState.NEEDS_DECISION)
            return _result(
                SyncState.NEEDS_DECISION,
                transitions,
                base_sha=base_sha,
                upstream_sha=upstream_sha,
                risk="conflict",
            )
        merge_head = _run(
            runner,
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            config.worktree,
        )
        if merge_head.returncode != 0 or _output(merge_head) != upstream_sha:
            transitions.append(SyncState.NEEDS_DECISION)
            return _result(
                SyncState.NEEDS_DECISION,
                transitions,
                base_sha=base_sha,
                upstream_sha=upstream_sha,
                risk="resolver_did_not_preserve_merge_state",
            )
        committed = _run(
            runner,
            ["git", "commit", "--no-edit"],
            config.worktree,
        )
        if committed.returncode != 0:
            transitions.append(SyncState.NEEDS_DECISION)
            return _result(
                SyncState.NEEDS_DECISION,
                transitions,
                base_sha=base_sha,
                upstream_sha=upstream_sha,
                risk="resolved_merge_commit_failed",
            )
        transitions.append(SyncState.AI_RESOLVED)
        classification = SyncClassification.MINOR_REVIEW_REQUIRED

    checks = _verify(config.worktree, runner)
    if any(check.status != "passed" for check in checks):
        transitions.append(SyncState.VERIFY_FAILED)
        return _result(
            SyncState.VERIFY_FAILED,
            transitions,
            base_sha=base_sha,
            upstream_sha=upstream_sha,
            checks=checks,
            risk="verification_failed",
        )
    transitions.append(SyncState.VERIFIED)

    candidate_result = _run(runner, ["git", "rev-parse", "HEAD"], config.worktree)
    changed_result = _run(
        runner,
        ["git", "diff", "--name-only", f"{config.origin}/main...HEAD"],
        config.worktree,
    )
    if candidate_result.returncode != 0 or changed_result.returncode != 0:
        transitions.append(SyncState.VERIFY_FAILED)
        return _result(
            SyncState.VERIFY_FAILED,
            transitions,
            base_sha=base_sha,
            upstream_sha=upstream_sha,
            checks=checks,
        )
    candidate_sha = _output(candidate_result)
    changed_files = tuple(
        line for line in _output(changed_result).splitlines() if line
    )

    pushed = _push_candidate(config, runner, previous_candidate_sha)
    if pushed.returncode != 0:
        transitions.append(SyncState.NEEDS_DECISION)
        return _result(
            SyncState.NEEDS_DECISION,
            transitions,
            base_sha=base_sha,
            upstream_sha=upstream_sha,
            candidate_sha=candidate_sha,
            checks=checks,
            changed_files=changed_files,
            risk="push_failed",
        )
    transitions.append(SyncState.PUSHED)

    title = "chore(sync): update fork from upstream"
    body = (
        f"Automated PR-only upstream candidate.\n\n"
        f"- Fork base: `{base_sha}`\n"
        f"- Upstream: `{upstream_sha}`\n"
        f"- Candidate: `{candidate_sha}`\n"
        f"- Upstream commits: {backlog}\n"
    )
    pr_number = github.find_open_pull_request(FIXED_CANDIDATE_BRANCH, "main")
    if pr_number is None:
        pr_number = github.create_pull_request(
            head=FIXED_CANDIDATE_BRANCH,
            base="main",
            title=title,
            body=body,
        )
    else:
        github.update_pull_request(pr_number, title=title, body=body)
    transitions.append(SyncState.PR_UPDATED)
    custom_prefixes = ("gateway/", "hermes_cli/kanban", "ops/cloudadvisor/")
    risk = (
        "fork_customizations_touched"
        if any(path.startswith(custom_prefixes) for path in changed_files)
        else "upstream_only"
    )
    return _result(
        SyncState.PR_UPDATED,
        transitions,
        base_sha=base_sha,
        upstream_sha=upstream_sha,
        candidate_sha=candidate_sha,
        pr_number=pr_number,
        checks=checks,
        risk=risk,
        changed_files=changed_files,
        classification=classification,
    )


def run(
    config: SyncConfig,
    *,
    github: GitHubPullRequests,
    runner: CommandRunner | None = None,
    resolver: ConflictResolver | None = None,
) -> SyncResult:
    runner = runner or SubprocessCommandRunner()
    transitions = (SyncState.LOCKED,)

    with try_exclusive_file_lock(config.lock_path) as acquired:
        if not acquired:
            return _result(SyncState.LOCKED, list(transitions))
        result = prepare_candidate(
            config,
            github=github,
            runner=runner,
            resolver=resolver,
        )
    return replace(result, transitions=transitions + result.transitions)
