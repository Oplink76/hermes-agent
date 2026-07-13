"""Exact-head GitHub boundary for protected upstream sync pull requests."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .command import CommandRunner
from .github_authority import (
    AmbiguousRequiredCheckEvidenceError,
    GitHubAuthorityError,
    GitHubAuthorityReader,
    RequiredCheckEvidenceError,
    required_check_conclusion,
    resolve_gh_executable,
)


DEFAULT_REQUIRED_CHECK = "All required checks pass"
_ALLOWED_PR_COMMANDS = frozenset(
    {"list", "create", "edit", "view", "checks", "merge"}
)


class SyncGitHubError(RuntimeError):
    """A redacted GitHub sync boundary failure."""


@dataclass(frozen=True)
class SyncPullRequestEvidence:
    number: int
    state: str
    base_sha: str
    head_sha: str
    required_check: str
    required_check_conclusion: str
    workflow_run_id: int
    required_check_run_id: int
    merge_sha: str | None = None


class SyncGitHubPort(Protocol):
    def evidence(self, pr_number: int) -> SyncPullRequestEvidence: ...

    def merge_exact(self, pr_number: int, *, expected_head: str) -> str: ...


def bind_expected_base(github: SyncGitHubPort, base_sha: str) -> None:
    """Bind concrete GitHub adapters while keeping strict test ports generic."""
    if hasattr(github, "expected_base_sha"):
        setattr(github, "expected_base_sha", base_sha)


class GhSyncGitHub:
    """Run normalized ``gh pr`` operations through an injected runner."""

    def __init__(
        self,
        repo_slug: str,
        runner: CommandRunner,
        cwd: Path,
        *,
        required_check: str = DEFAULT_REQUIRED_CHECK,
        expected_base_sha: str | None = None,
        gh_executable: str | Path | None = None,
    ):
        try:
            resolved_gh = resolve_gh_executable(gh_executable)
        except GitHubAuthorityError as exc:
            raise SyncGitHubError(str(exc)) from exc
        self.repo_slug = repo_slug
        self.required_check = required_check
        self.expected_base_sha = expected_base_sha
        self.runner = runner
        self.cwd = Path(cwd)
        self.gh_executable = resolved_gh
        self._authority = GitHubAuthorityReader(
            repo_slug=repo_slug,
            required_check=required_check,
            runner=runner,
            cwd=cwd,
            gh_executable=resolved_gh,
        )

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if (
            len(argv) < 3
            or argv[:2] != [self.gh_executable, "pr"]
            or argv[2] not in _ALLOWED_PR_COMMANDS
        ):
            raise SyncGitHubError("refusing non-normalized GitHub CLI command")
        completed = self.runner.run(argv, cwd=self.cwd, timeout=300)
        if completed.returncode != 0:
            raise SyncGitHubError(f"GitHub CLI pr {argv[2]} failed")
        return completed

    def _json(self, argv: list[str]) -> object:
        completed = self._run(argv)
        try:
            return json.loads(completed.stdout or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise SyncGitHubError("GitHub CLI returned invalid JSON") from exc

    def find_open_pull_request(self, head: str, base: str) -> int | None:
        payload = self._json([
            self.gh_executable,
            "pr",
            "list",
            "--repo",
            self.repo_slug,
            "--head",
            head,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number",
            "--limit",
            "2",
        ])
        try:
            if not isinstance(payload, list):
                raise TypeError
            rows = payload
            if len(rows) > 1:
                raise SyncGitHubError("more than one open upstream sync PR")
            if not rows:
                return None
            number = rows[0]["number"]
            if type(number) is not int or number < 1:
                raise TypeError
            return number
        except SyncGitHubError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise SyncGitHubError("GitHub pull request list was incomplete") from exc

    def create_pull_request(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> int:
        completed = self._run([
            self.gh_executable,
            "pr",
            "create",
            "--repo",
            self.repo_slug,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ])
        url = (completed.stdout or "").strip().rstrip("/")
        try:
            return int(url.rsplit("/", maxsplit=1)[1])
        except (IndexError, ValueError) as exc:
            raise SyncGitHubError("created pull request number was missing") from exc

    def update_pull_request(self, number: int, *, title: str, body: str) -> None:
        self._run([
            self.gh_executable,
            "pr",
            "edit",
            str(number),
            "--repo",
            self.repo_slug,
            "--title",
            title,
            "--body",
            body,
        ])

    def evidence(self, pr_number: int) -> SyncPullRequestEvidence:
        try:
            authority = self._authority.read(pr_number)
            return SyncPullRequestEvidence(
                number=authority.number,
                state=authority.state,
                base_sha=authority.base_sha,
                head_sha=authority.head_sha,
                required_check=self.required_check,
                required_check_conclusion=authority.required_check_conclusion,
                workflow_run_id=authority.workflow_run_id,
                required_check_run_id=authority.required_check_run_id,
                merge_sha=authority.merge_sha,
            )
        except AmbiguousRequiredCheckEvidenceError as exc:
            raise SyncGitHubError(str(exc)) from exc
        except GitHubAuthorityError as exc:
            if str(exc) == "GitHub CLI pull request view failed":
                raise SyncGitHubError("GitHub CLI pr view failed") from exc
            raise SyncGitHubError("GitHub pull request evidence was incomplete") from exc

    def merge_exact(self, pr_number: int, *, expected_head: str) -> str:
        if self.expected_base_sha is None:
            raise SyncGitHubError("expected base SHA is required for merge")
        evidence = self.evidence(pr_number)
        if evidence.state != "open":
            raise SyncGitHubError("pull request is not open")
        if evidence.head_sha != expected_head:
            raise SyncGitHubError("pull request head changed")
        if evidence.base_sha != self.expected_base_sha:
            raise SyncGitHubError("pull request base changed")
        if evidence.required_check_conclusion != "success":
            raise SyncGitHubError("required check is not green")

        self._run([
            self.gh_executable,
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            self.repo_slug,
            "--merge",
            "--match-head-commit",
            expected_head,
        ])
        merged = self.evidence(pr_number)
        if merged.head_sha != expected_head:
            raise SyncGitHubError("pull request head changed")
        if merged.base_sha != self.expected_base_sha:
            raise SyncGitHubError("pull request base changed")
        if merged.state != "merged":
            raise SyncGitHubError("pull request did not report merged")
        if merged.merge_sha is None:
            raise SyncGitHubError("merge SHA is missing")
        return merged.merge_sha
