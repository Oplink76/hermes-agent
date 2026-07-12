"""Exact-head GitHub boundary for protected upstream sync pull requests."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .command import CommandRunner


DEFAULT_REQUIRED_CHECK = "All required checks pass"
_ALLOWED_PR_COMMANDS = frozenset(
    {"list", "create", "edit", "view", "checks", "merge"}
)
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")


class SyncGitHubError(RuntimeError):
    """A redacted GitHub sync boundary failure."""


class RequiredCheckEvidenceError(ValueError):
    """Required-check evidence is malformed or ambiguous."""


class AmbiguousRequiredCheckEvidenceError(RequiredCheckEvidenceError):
    """More than one row claims the configured required-check identity."""


def required_check_conclusion(checks: object, required_check: str) -> str:
    """Return one configured check's normalized state, rejecting duplicates."""
    required = _required_check_row(checks, required_check)
    if required is None:
        return "missing"
    state = (
        required.get("conclusion")
        or required.get("state")
        or required.get("status")
        or "pending"
    )
    if not isinstance(state, str):
        raise RequiredCheckEvidenceError("required check evidence is invalid")
    return state.lower()


def _required_check_row(
    checks: object, required_check: str
) -> dict[str, object] | None:
    if not isinstance(checks, list):
        raise RequiredCheckEvidenceError("required check evidence is invalid")
    matches: list[dict[str, object]] = []
    for check in checks:
        if not isinstance(check, dict):
            raise RequiredCheckEvidenceError("required check evidence is invalid")
        name = check.get("name")
        context = check.get("context")
        if name is not None and not isinstance(name, str):
            raise RequiredCheckEvidenceError("required check evidence is invalid")
        if context is not None and not isinstance(context, str):
            raise RequiredCheckEvidenceError("required check evidence is invalid")
        if required_check in {name, context}:
            matches.append(check)
    if len(matches) > 1:
        raise AmbiguousRequiredCheckEvidenceError(
            "required check evidence is ambiguous"
        )
    if not matches:
        return None
    required = matches[0]
    for field in ("conclusion", "state", "status"):
        value = required.get(field)
        if value is not None and not isinstance(value, str):
            raise RequiredCheckEvidenceError("required check evidence is invalid")
    return required


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


def _full_sha(value: object) -> str:
    if not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None:
        raise ValueError
    return value


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
        resolved_gh = (
            shutil.which("gh") if gh_executable is None else str(gh_executable)
        )
        if not resolved_gh:
            raise SyncGitHubError("GitHub CLI executable was not found")
        self.repo_slug = repo_slug
        self.required_check = required_check
        self.expected_base_sha = expected_base_sha
        self.runner = runner
        self.cwd = Path(cwd)
        self.gh_executable = resolved_gh

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
            return int(rows[0]["number"]) if rows else None
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
        payload = self._json([
            self.gh_executable,
            "pr",
            "view",
            str(pr_number),
            "--repo",
            self.repo_slug,
            "--json",
            "number,state,baseRefOid,headRefOid,mergeCommit,statusCheckRollup",
        ])
        try:
            if not isinstance(payload, dict):
                raise TypeError
            number = payload["number"]
            state = payload["state"]
            if type(number) is not int or not isinstance(state, str) or not state:
                raise TypeError
            base_sha = _full_sha(payload["baseRefOid"])
            head_sha = _full_sha(payload["headRefOid"])

            try:
                required_row = _required_check_row(
                    payload["statusCheckRollup"], self.required_check
                )
                conclusion = required_check_conclusion(
                    payload["statusCheckRollup"], self.required_check
                )
            except AmbiguousRequiredCheckEvidenceError as exc:
                raise SyncGitHubError(str(exc)) from exc
            except RequiredCheckEvidenceError as exc:
                raise TypeError from exc
            if required_row is None:
                raise TypeError
            details_url = required_row.get("detailsUrl")
            if not isinstance(details_url, str):
                raise TypeError
            identity = re.fullmatch(
                rf"https://github\.com/{re.escape(self.repo_slug)}"
                r"/actions/runs/(?P<workflow>[1-9][0-9]*)"
                r"/job/(?P<check>[1-9][0-9]*)(?:\?.*)?",
                details_url,
            )
            if identity is None:
                raise TypeError
            workflow_run_id = int(identity.group("workflow"))
            required_check_run_id = int(identity.group("check"))

            merge_commit = payload["mergeCommit"]
            if merge_commit is None:
                merge_sha = None
            else:
                if not isinstance(merge_commit, dict):
                    raise TypeError
                merge_sha = _full_sha(merge_commit.get("oid"))
            evidence = SyncPullRequestEvidence(
                number=number,
                state=state.lower(),
                base_sha=base_sha,
                head_sha=head_sha,
                required_check=self.required_check,
                required_check_conclusion=conclusion,
                workflow_run_id=workflow_run_id,
                required_check_run_id=required_check_run_id,
                merge_sha=merge_sha,
            )
            if evidence.number != pr_number:
                raise ValueError
            return evidence
        except (KeyError, TypeError, ValueError) as exc:
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
