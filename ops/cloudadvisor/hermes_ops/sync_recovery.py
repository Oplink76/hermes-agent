"""Bounded protected-PR recovery for failed autonomous sync deployments."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from .command import CommandRunner
from .deploy import DeploymentRecord
from .sync import SyncResult
from .sync_github import (
    SyncGitHubError,
    SyncGitHubPort,
    SyncPullRequestEvidence,
    bind_expected_base,
)
from .sync_poll import ExactHeadExpectation, ExactHeadPollError, poll_exact_head
from utils import atomic_json_write


_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
logger = logging.getLogger(__name__)


def _log_unexpected(error: Exception) -> None:
    sanitized = RuntimeError("redacted unexpected recovery failure")
    logger.error(
        "Protected sync recovery failed unexpectedly",
        exc_info=(type(sanitized), sanitized, error.__traceback__),
    )


class ProtectedRevertState(str, Enum):
    REVERTED = "REVERTED"
    NEEDS_OLE = "NEEDS_OLE"


class ProtectedRevertError(RuntimeError):
    """Expected protected-recovery evidence failure."""


@dataclass(frozen=True)
class ProtectedRevertResult:
    state: ProtectedRevertState
    previous_sha: str
    installed_sha: str
    revert_head_sha: str | None = None
    revert_merge_sha: str | None = None
    reason: str | None = None


class ProtectedRevertGitHubPort(SyncGitHubPort, Protocol):
    def find_open_pull_request(self, head: str, base: str) -> int | None: ...

    def create_pull_request(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> int: ...


def failed_candidate_fingerprint(candidate: SyncResult) -> str:
    """Identify only one exact upstream/candidate pair."""
    if not candidate.upstream_sha or not candidate.candidate_tree_sha:
        raise ValueError("candidate fingerprint requires upstream and content identities")
    payload = f"{candidate.upstream_sha}\0{candidate.candidate_tree_sha}".encode()
    return hashlib.sha256(payload).hexdigest()


def _quarantine_path(root: Path, candidate: SyncResult) -> Path:
    return Path(root) / f"{failed_candidate_fingerprint(candidate)}.json"


def is_quarantined(root: Path, candidate: SyncResult) -> bool:
    return _quarantine_path(root, candidate).is_file()


def quarantine_candidate(
    root: Path,
    candidate: SyncResult,
    *,
    merge_sha: str,
) -> Path:
    """Durably prevent an unchanged failed candidate from looping."""
    if _FULL_SHA.fullmatch(merge_sha) is None:
        raise ValueError("quarantine requires an exact merge SHA")
    path = _quarantine_path(root, candidate)
    payload = {
        "candidate_sha": candidate.candidate_sha,
        "candidate_tree_sha": candidate.candidate_tree_sha,
        "fingerprint": path.stem,
        "merge_sha": merge_sha,
        "quarantined_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "upstream_sha": candidate.upstream_sha,
    }
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            # The timestamp is intentionally not identity-bearing.
            comparable = dict(existing)
            comparable["quarantined_at"] = payload["quarantined_at"]
            if comparable != payload:
                raise ValueError("quarantine artifact does not match candidate")
        return path
    atomic_json_write(path, payload, mode=0o600, sort_keys=True)
    return path


def _run_required(
    runner: CommandRunner,
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 600,
) -> str:
    completed = runner.run(argv, cwd=cwd, timeout=timeout)
    if completed.returncode != 0:
        raise ProtectedRevertError("recovery Git operation failed")
    return (completed.stdout or "").strip()


def _wait_for_revert_green(
    github: SyncGitHubPort,
    *,
    pr_number: int,
    expected_base: str,
    expected_head: str,
    required_check: str,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> SyncPullRequestEvidence:
    return poll_exact_head(
        github,
        ExactHeadExpectation(
            pr_number,
            expected_base,
            expected_head,
            required_check,
        ),
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
        sleeper=sleeper,
    )


def run_protected_revert(
    *,
    repo: Path,
    origin: str,
    repo_slug: str,
    required_check: str,
    candidate: SyncResult,
    merge_sha: str,
    deployment: DeploymentRecord,
    quarantine_root: Path,
    runner: CommandRunner,
    github: ProtectedRevertGitHubPort,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    timeout_seconds: int,
    poll_interval_seconds: int,
    verify_runtime_fn: Callable[[str], bool],
) -> ProtectedRevertResult:
    """Quarantine a healthy rollback and revert its merge through protection."""
    del repo_slug  # The GitHub port is already scoped to the configured repository.
    if deployment.requested_sha != merge_sha:
        return ProtectedRevertResult(
            state=ProtectedRevertState.NEEDS_OLE,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            reason="deployment record does not match failed merge",
        )
    if deployment.status != "rolled_back_healthy":
        return ProtectedRevertResult(
            state=ProtectedRevertState.NEEDS_OLE,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            reason="deployment rollback is not healthy",
        )
    try:
        quarantine_candidate(quarantine_root, candidate, merge_sha=merge_sha)
        repo = Path(repo)
        _run_required(runner, ["git", "fetch", origin, "main"], cwd=repo)
        current_main = _run_required(
            runner, ["git", "rev-parse", f"{origin}/main"], cwd=repo
        )
        if current_main != merge_sha:
            raise ProtectedRevertError("fork main changed before protected revert")
        if not candidate.base_sha or not candidate.candidate_sha:
            raise ProtectedRevertError("candidate lineage evidence is incomplete")
        parents = _run_required(
            runner,
            ["git", "rev-list", "--parents", "-n", "1", merge_sha],
            cwd=repo,
        ).split()
        if parents != [merge_sha, candidate.base_sha, candidate.candidate_sha]:
            raise ProtectedRevertError("failed merge parents do not match candidate")
        previous_tree = _run_required(
            runner,
            ["git", "rev-parse", f"{deployment.previous_sha}^{{tree}}"],
            cwd=repo,
        )
        base_tree = _run_required(
            runner,
            ["git", "rev-parse", f"{candidate.base_sha}^{{tree}}"],
            cwd=repo,
        )
        if deployment.previous_sha != candidate.base_sha and previous_tree != base_tree:
            raise ProtectedRevertError(
                "previous runtime is not equivalent to candidate base"
            )

        branch = f"auto-sync/revert-{merge_sha[:12]}"
        with tempfile.TemporaryDirectory(prefix="hermes-sync-revert-") as temporary:
            worktree = Path(temporary)
            added = False
            try:
                _run_required(
                    runner,
                    ["git", "worktree", "add", "--detach", str(worktree), f"{origin}/main"],
                    cwd=repo,
                )
                added = True
                _run_required(runner, ["git", "switch", "-c", branch], cwd=worktree)
                _run_required(
                    runner,
                    ["git", "revert", "-m", "1", "--no-edit", merge_sha],
                    cwd=worktree,
                )
                _run_required(
                    runner, ["git", "diff", "--check", "HEAD^", "HEAD"], cwd=worktree
                )
                unmerged = _run_required(
                    runner, ["git", "ls-files", "-u"], cwd=worktree
                )
                if unmerged:
                    raise RuntimeError("protected revert left unmerged entries")
                revert_head = _run_required(
                    runner, ["git", "rev-parse", "HEAD"], cwd=worktree
                )
                if _FULL_SHA.fullmatch(revert_head) is None:
                    raise ProtectedRevertError("protected revert head SHA is invalid")
                revert_tree = _run_required(
                    runner,
                    ["git", "rev-parse", f"{revert_head}^{{tree}}"],
                    cwd=worktree,
                )
                if revert_tree != previous_tree:
                    raise ProtectedRevertError(
                        "protected revert did not restore previous tree"
                    )
                destination = f"HEAD:refs/heads/{branch}"
                if destination.endswith("refs/heads/main"):
                    raise RuntimeError("refusing to push recovery directly to main")
                _run_required(
                    runner, ["git", "push", origin, destination], cwd=worktree
                )
            finally:
                if added:
                    runner.run(
                        ["git", "worktree", "remove", "--force", str(worktree)],
                        cwd=repo,
                        timeout=600,
                    )

        if github.find_open_pull_request(branch, "main") is not None:
            raise RuntimeError("an unexpected open protected revert PR already exists")
        pr_number = github.create_pull_request(
            head=branch,
            base="main",
            title=f"revert(sync): restore pre-{merge_sha[:12]} lineage",
            body=(
                "Automated protected revert after a healthy runtime rollback.\n\n"
                f"- Failed merge: `{merge_sha}`\n"
                f"- Restored runtime: `{deployment.previous_sha}`\n"
            ),
        )
        _wait_for_revert_green(
            github,
            pr_number=pr_number,
            expected_base=merge_sha,
            expected_head=revert_head,
            required_check=required_check,
            clock=clock,
            sleeper=sleeper,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        bind_expected_base(github, merge_sha)
        revert_merge_sha = github.merge_exact(pr_number, expected_head=revert_head)
        if _FULL_SHA.fullmatch(revert_merge_sha) is None:
            raise ProtectedRevertError("protected revert merge SHA is invalid")
        _run_required(runner, ["git", "fetch", origin, "main"], cwd=repo)
        restored_main = _run_required(
            runner, ["git", "rev-parse", f"{origin}/main"], cwd=repo
        )
        if restored_main != revert_merge_sha:
            raise ProtectedRevertError("fork main changed after protected revert")
        restored_tree = _run_required(
            runner,
            ["git", "rev-parse", f"{revert_merge_sha}^{{tree}}"],
            cwd=repo,
        )
        if restored_tree != previous_tree:
            raise ProtectedRevertError("protected revert merge tree is not restored")
        if not verify_runtime_fn(deployment.previous_sha):
            raise ProtectedRevertError("installed runtime health is not restored")
        return ProtectedRevertResult(
            state=ProtectedRevertState.REVERTED,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            revert_head_sha=revert_head,
            revert_merge_sha=revert_merge_sha,
        )
    except (ExactHeadPollError, ProtectedRevertError) as exc:
        return ProtectedRevertResult(
            state=ProtectedRevertState.NEEDS_OLE,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            reason=str(exc),
        )
    except SyncGitHubError:
        return ProtectedRevertResult(
            state=ProtectedRevertState.NEEDS_OLE,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            reason="protected GitHub recovery evidence is invalid",
        )
    except ValueError:
        return ProtectedRevertResult(
            state=ProtectedRevertState.NEEDS_OLE,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            reason="protected recovery evidence is invalid",
        )
    except Exception as exc:
        _log_unexpected(exc)
        return ProtectedRevertResult(
            state=ProtectedRevertState.NEEDS_OLE,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            reason="unexpected protected recovery failure",
        )
