"""Bounded protected-PR recovery for failed autonomous sync deployments."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from .command import CommandRunner
from .deploy import DeploymentRecord
from .sync import SyncResult
from .sync_github import SyncGitHubPort, SyncPullRequestEvidence
from utils import atomic_json_write


_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")


class ProtectedRevertState(str, Enum):
    REVERTED = "REVERTED"
    NEEDS_OLE = "NEEDS_OLE"


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
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"recovery command failed ({' '.join(argv)}): {detail}")
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
    deadline = clock() + timeout_seconds
    max_polls = timeout_seconds // poll_interval_seconds + 2
    for poll in range(max_polls):
        evidence = github.evidence(pr_number)
        if evidence.number != pr_number:
            raise RuntimeError("revert PR number changed")
        if evidence.state != "open":
            raise RuntimeError("revert PR is not open")
        if evidence.base_sha != expected_base:
            raise RuntimeError("revert PR base changed")
        if evidence.head_sha != expected_head:
            raise RuntimeError("revert PR head changed")
        if evidence.required_check != required_check:
            raise RuntimeError("revert required check identity changed")
        conclusion = evidence.required_check_conclusion.lower()
        if conclusion == "success":
            return evidence
        if conclusion not in {"pending", "queued", "in_progress"}:
            raise RuntimeError("revert required check is not green")
        if clock() >= deadline or poll + 1 == max_polls:
            raise RuntimeError("revert required check timed out")
        sleeper(poll_interval_seconds)
    raise RuntimeError("revert required check timed out")


def _bind_expected_base(github: SyncGitHubPort, base_sha: str) -> None:
    """Bind the concrete gh adapter while leaving strict test ports generic."""
    if hasattr(github, "expected_base_sha"):
        setattr(github, "expected_base_sha", base_sha)


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
) -> ProtectedRevertResult:
    """Quarantine a healthy rollback and revert its merge through protection."""
    del repo_slug  # The GitHub port is already scoped to the configured repository.
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
            raise RuntimeError("fork main changed before protected revert")

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
                    runner, ["git", "revert", "--no-edit", merge_sha], cwd=worktree
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
                    raise RuntimeError("protected revert head SHA is invalid")
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
        _bind_expected_base(github, merge_sha)
        revert_merge_sha = github.merge_exact(pr_number, expected_head=revert_head)
        if _FULL_SHA.fullmatch(revert_merge_sha) is None:
            raise RuntimeError("protected revert merge SHA is invalid")
        return ProtectedRevertResult(
            state=ProtectedRevertState.REVERTED,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            revert_head_sha=revert_head,
            revert_merge_sha=revert_merge_sha,
        )
    except Exception as exc:
        return ProtectedRevertResult(
            state=ProtectedRevertState.NEEDS_OLE,
            previous_sha=deployment.previous_sha,
            installed_sha=deployment.previous_sha,
            reason=str(exc),
        )
