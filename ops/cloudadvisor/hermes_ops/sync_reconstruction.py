"""Exact candidate-only reconstruction after a protected sync revert."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .command import CommandRunner
from .sync import (
    FIXED_CANDIDATE_BRANCH,
    GitHubPullRequests,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
)


_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")


class ReconstructionError(RuntimeError):
    """Recorded failed content could not be safely reconstructed."""


def _run(
    runner: CommandRunner,
    argv: list[str],
    cwd: Path,
    *,
    timeout: int = 600,
) -> str:
    completed = runner.run(argv, cwd=cwd, timeout=timeout)
    if completed.returncode != 0:
        raise ReconstructionError("candidate reconstruction Git operation failed")
    return (completed.stdout or "").strip()


def reconstruct_failed_candidate(
    config: SyncConfig,
    *,
    failed: SyncResult,
    failed_merge_sha: str,
    revert_main_sha: str,
    github: GitHubPullRequests,
    runner: CommandRunner,
) -> SyncResult:
    """Reintroduce one exact failed tree as a child of verified revert-main."""
    return _reconstruct_failed_candidate(
        config,
        failed=failed,
        failed_merge_sha=failed_merge_sha,
        revert_main_sha=revert_main_sha,
        expected_candidate_sha=failed.candidate_sha,
        current_upstream_sha=None,
        github=github,
        runner=runner,
    )


def resume_failed_candidate_reconstruction(
    config: SyncConfig,
    *,
    failed: SyncResult,
    failed_merge_sha: str,
    revert_main_sha: str,
    expected_candidate_sha: str,
    current_upstream_sha: str,
    github: GitHubPullRequests,
    runner: CommandRunner,
) -> SyncResult:
    """Reintroduce the failed tree, then merge the exact current upstream."""
    return _reconstruct_failed_candidate(
        config,
        failed=failed,
        failed_merge_sha=failed_merge_sha,
        revert_main_sha=revert_main_sha,
        expected_candidate_sha=expected_candidate_sha,
        current_upstream_sha=current_upstream_sha,
        github=github,
        runner=runner,
    )


def _reconstruct_failed_candidate(
    config: SyncConfig,
    *,
    failed: SyncResult,
    failed_merge_sha: str,
    revert_main_sha: str,
    expected_candidate_sha: str | None,
    current_upstream_sha: str | None,
    github: GitHubPullRequests,
    runner: CommandRunner,
) -> SyncResult:
    if (
        _FULL_SHA.fullmatch(failed_merge_sha) is None
        or _FULL_SHA.fullmatch(revert_main_sha) is None
        or _FULL_SHA.fullmatch(expected_candidate_sha or "") is None
        or not failed.base_sha
        or not failed.candidate_sha
        or not failed.candidate_tree_sha
        or failed.pr_number is None
        or not failed.upstream_sha
    ):
        raise ReconstructionError("candidate reconstruction evidence is incomplete")
    if current_upstream_sha is not None and _FULL_SHA.fullmatch(
        current_upstream_sha
    ) is None:
        raise ReconstructionError("current upstream reconstruction evidence is invalid")

    repo = Path(config.repo)
    _run(runner, ["git", "fetch", config.origin, "main"], repo)
    current_main = _run(
        runner, ["git", "rev-parse", f"{config.origin}/main"], repo
    )
    if current_main != revert_main_sha:
        raise ReconstructionError("protected revert main changed before reconstruction")
    parents = _run(
        runner,
        ["git", "rev-list", "--parents", "-n", "1", failed_merge_sha],
        repo,
    ).split()
    if parents != [failed_merge_sha, failed.base_sha, failed.candidate_sha]:
        raise ReconstructionError("failed merge lineage does not match reconstruction")
    recorded_tree = _run(
        runner, ["git", "rev-parse", f"{failed.candidate_sha}^{{tree}}"], repo
    )
    if recorded_tree != failed.candidate_tree_sha:
        raise ReconstructionError("failed candidate tree evidence changed")

    remote_ref = f"refs/remotes/{config.origin}/{FIXED_CANDIDATE_BRANCH}"
    _run(
        runner,
        [
            "git",
            "fetch",
            config.origin,
            f"+refs/heads/{FIXED_CANDIDATE_BRANCH}:{remote_ref}",
        ],
        repo,
    )
    remote_candidate = _run(runner, ["git", "rev-parse", remote_ref], repo)
    if remote_candidate != expected_candidate_sha:
        raise ReconstructionError("rolling candidate changed before reconstruction")

    if current_upstream_sha is not None:
        _run(runner, ["git", "fetch", config.upstream, "main"], repo)
        fetched_upstream = _run(
            runner, ["git", "rev-parse", f"{config.upstream}/main"], repo
        )
        if fetched_upstream != current_upstream_sha:
            raise ReconstructionError("official upstream changed before reconstruction")

    with tempfile.TemporaryDirectory(
        prefix="hermes-sync-reconstruct-", dir=repo.parent
    ) as temporary:
        worktree = Path(temporary)
        added = False
        try:
            _run(
                runner,
                ["git", "worktree", "add", "--detach", str(worktree), current_main],
                repo,
            )
            added = True
            _run(
                runner,
                ["git", "read-tree", "--reset", "-u", failed.candidate_tree_sha],
                worktree,
            )
            if not _run(
                runner,
                ["git", "status", "--porcelain", "--untracked-files=all"],
                worktree,
            ):
                raise ReconstructionError("failed candidate tree matches revert main")
            _run(runner, ["git", "diff", "--check"], worktree)
            if _run(runner, ["git", "ls-files", "-u"], worktree):
                raise ReconstructionError("candidate reconstruction left conflicts")
            _run(
                runner,
                ["git", "commit", "-m", "chore(sync): reconstruct failed candidate"],
                worktree,
            )
            reconstructed_sha = _run(runner, ["git", "rev-parse", "HEAD"], worktree)
            reconstructed_parent = _run(
                runner, ["git", "rev-parse", "HEAD^"], worktree
            )
            reconstructed_tree = _run(
                runner, ["git", "rev-parse", "HEAD^{tree}"], worktree
            )
            if current_upstream_sha is not None:
                _run(
                    runner,
                    [
                        "git",
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        current_upstream_sha,
                    ],
                    worktree,
                )
            candidate_sha = _run(runner, ["git", "rev-parse", "HEAD"], worktree)
            candidate_tree = _run(
                runner, ["git", "rev-parse", "HEAD^{tree}"], worktree
            )
            changed = tuple(
                line
                for line in _run(
                    runner,
                    ["git", "diff", "--name-only", f"{current_main}..HEAD"],
                    worktree,
                ).splitlines()
                if line
            )
            if (
                _FULL_SHA.fullmatch(reconstructed_sha) is None
                or reconstructed_parent != current_main
                or reconstructed_tree != failed.candidate_tree_sha
                or _FULL_SHA.fullmatch(candidate_sha) is None
                or not changed
            ):
                raise ReconstructionError("candidate reconstruction identity is invalid")
            destination = f"HEAD:refs/heads/{FIXED_CANDIDATE_BRANCH}"
            lease = (
                f"--force-with-lease=refs/heads/{FIXED_CANDIDATE_BRANCH}:"
                f"{expected_candidate_sha}"
            )
            _run(
                runner,
                ["git", "push", config.origin, destination, lease],
                worktree,
            )
        finally:
            if added:
                runner.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=repo,
                    timeout=600,
                )

    _run(runner, ["git", "fetch", config.origin, "main"], repo)
    if _run(runner, ["git", "rev-parse", f"{config.origin}/main"], repo) != current_main:
        raise ReconstructionError("protected main changed during reconstruction")

    title = "fix(sync): repair reverted upstream candidate"
    body = (
        "Automated candidate-only reconstruction after protected recovery.\n\n"
        f"- Protected base: `{current_main}`\n"
        f"- Failed merge: `{failed_merge_sha}`\n"
        f"- Reconstructed tree commit: `{reconstructed_sha}`\n"
        f"- Candidate head: `{candidate_sha}`\n"
        + (
            ""
            if current_upstream_sha is None
            else f"- Current upstream: `{current_upstream_sha}`\n"
        )
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
    return SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=current_main,
        upstream_sha=current_upstream_sha or failed.upstream_sha,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree,
        pr_number=pr_number,
        risk="post_revert_reconstruction",
        changed_files=changed,
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=changed,
        resolution_strategy="candidate_repair",
    )
