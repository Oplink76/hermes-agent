"""One strict GitHub pull-request authority reader for sync and deployment."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .command import CommandRunner


AUTHORITY_FIELDS = (
    "number,state,mergedAt,mergeCommit,headRefOid,baseRefName,"
    "baseRefOid,statusCheckRollup"
)
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")


class GitHubAuthorityError(ValueError):
    """GitHub authority evidence is malformed, incomplete, or ambiguous."""


class RequiredCheckEvidenceError(GitHubAuthorityError):
    """Required-check evidence is malformed or ambiguous."""


class MissingRequiredCheckEvidenceError(RequiredCheckEvidenceError):
    """The configured aggregate check has not been published yet."""


class AmbiguousRequiredCheckEvidenceError(RequiredCheckEvidenceError):
    """More than one row claims the configured required-check identity."""


def resolve_gh_executable(value: str | Path | None = None) -> str:
    resolved = shutil.which("gh") if value is None else str(value)
    if not resolved:
        raise GitHubAuthorityError("GitHub CLI executable was not found")
    return resolved


def required_check_row(
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
    for field in ("conclusion", "state", "status", "detailsUrl"):
        value = required.get(field)
        if value is not None and not isinstance(value, str):
            raise RequiredCheckEvidenceError("required check evidence is invalid")
    return required


def required_check_conclusion(checks: object, required_check: str) -> str:
    required = required_check_row(checks, required_check)
    if required is None:
        return "missing"
    for field in ("conclusion", "state", "status"):
        value = required.get(field)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise RequiredCheckEvidenceError(
                    "required check evidence is invalid"
                )
            return value.lower()
    return "pending"


def _full_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None:
        raise GitHubAuthorityError(f"GitHub {field} is not a full commit SHA")
    return value


@dataclass(frozen=True)
class PullRequestAuthority:
    number: int
    state: str
    merged_at: str | None
    merge_sha: str | None
    head_sha: str
    base_ref_name: str
    base_sha: str
    required_check: str
    required_check_conclusion: str
    workflow_run_id: int
    required_check_run_id: int

    @property
    def merged(self) -> bool:
        return self.state == "merged"


def parse_pull_request_authority(
    payload: object,
    *,
    repo_slug: str,
    required_check: str,
) -> PullRequestAuthority:
    if not isinstance(payload, dict):
        raise GitHubAuthorityError("GitHub pull request evidence is incomplete")
    try:
        number = payload["number"]
        state_value = payload["state"]
        merged_at = payload["mergedAt"]
        base_ref_name = payload["baseRefName"]
        if type(number) is not int or number < 1:
            raise GitHubAuthorityError("GitHub PR number is invalid")
        if not isinstance(state_value, str) or state_value.lower() not in {
            "open",
            "closed",
            "merged",
        }:
            raise GitHubAuthorityError("GitHub PR state is invalid")
        state = state_value.lower()
        if merged_at is not None and (
            not isinstance(merged_at, str) or not merged_at
        ):
            raise GitHubAuthorityError("GitHub merge timestamp is invalid")
        if not isinstance(base_ref_name, str) or not base_ref_name:
            raise GitHubAuthorityError("GitHub base ref is invalid")
        head_sha = _full_sha(payload["headRefOid"], field="head")
        base_sha = _full_sha(payload["baseRefOid"], field="base")
        merge_commit = payload["mergeCommit"]
        if merge_commit is None:
            merge_sha = None
        elif isinstance(merge_commit, dict) and set(merge_commit) == {"oid"}:
            merge_sha = _full_sha(merge_commit["oid"], field="merge")
        else:
            raise GitHubAuthorityError("GitHub merge identity is invalid")
        if state == "merged":
            if merged_at is None:
                raise GitHubAuthorityError("GitHub merged evidence is incomplete")
        elif merged_at is not None or merge_sha is not None:
            raise GitHubAuthorityError("GitHub unmerged evidence is inconsistent")

        row = required_check_row(payload["statusCheckRollup"], required_check)
        if row is None:
            raise MissingRequiredCheckEvidenceError(
                "required check evidence is missing"
            )
        conclusion = required_check_conclusion(
            payload["statusCheckRollup"], required_check
        )
        details_url = row.get("detailsUrl")
        if not isinstance(details_url, str):
            raise GitHubAuthorityError("required check run identity is missing")
        identity = re.fullmatch(
            rf"https://github\.com/{re.escape(repo_slug)}"
            r"/actions/runs/(?P<workflow>[1-9][0-9]*)"
            r"/job/(?P<check>[1-9][0-9]*)(?:\?.*)?",
            details_url,
        )
        if identity is None:
            raise GitHubAuthorityError("required check run identity is invalid")
        return PullRequestAuthority(
            number=number,
            state=state,
            merged_at=merged_at,
            merge_sha=merge_sha,
            head_sha=head_sha,
            base_ref_name=base_ref_name,
            base_sha=base_sha,
            required_check=required_check,
            required_check_conclusion=conclusion,
            workflow_run_id=int(identity.group("workflow")),
            required_check_run_id=int(identity.group("check")),
        )
    except KeyError as exc:
        raise GitHubAuthorityError(
            "GitHub pull request evidence is incomplete"
        ) from exc


class GitHubAuthorityReader:
    """Read and strictly parse one ``gh pr view`` authority record."""

    def __init__(
        self,
        *,
        repo_slug: str,
        required_check: str,
        runner: CommandRunner,
        cwd: Path,
        gh_executable: str | Path | None = None,
    ):
        self.repo_slug = repo_slug
        self.required_check = required_check
        self.runner = runner
        self.cwd = Path(cwd)
        self.gh_executable = resolve_gh_executable(gh_executable)

    def read(self, pr_number: int) -> PullRequestAuthority:
        if type(pr_number) is not int or pr_number < 1:
            raise GitHubAuthorityError("GitHub PR number is invalid")
        argv = [
            self.gh_executable,
            "pr",
            "view",
            str(pr_number),
            "--repo",
            self.repo_slug,
            "--json",
            AUTHORITY_FIELDS,
        ]
        completed = self.runner.run(argv, cwd=self.cwd, timeout=300)
        if completed.returncode != 0:
            raise GitHubAuthorityError("GitHub CLI pull request view failed")
        try:
            payload = json.loads(completed.stdout or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise GitHubAuthorityError("GitHub CLI returned invalid JSON") from exc
        authority = parse_pull_request_authority(
            payload,
            repo_slug=self.repo_slug,
            required_check=self.required_check,
        )
        if authority.number != pr_number:
            raise GitHubAuthorityError("GitHub PR identity changed")
        return authority
