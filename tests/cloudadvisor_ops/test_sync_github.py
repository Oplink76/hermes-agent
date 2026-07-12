from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync_github import GhSyncGitHub, SyncGitHubError


REPO = "Oplink76/hermes-agent"
CHECK = "All required checks pass"


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path


class ScriptedRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]):
        self.responses = list(responses)
        self.calls: list[Call] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        self.calls.append(Call(tuple(argv), Path(cwd)))
        return self.responses.pop(0)


def _completed(payload: object = None) -> subprocess.CompletedProcess[str]:
    stdout = "" if payload is None else json.dumps(payload)
    return subprocess.CompletedProcess([], 0, stdout, "")


def _pr(
    *,
    state: str = "OPEN",
    base: str = "base",
    head: str = "candidate",
    conclusion: str | None = "SUCCESS",
    merge_sha: str | None = None,
) -> dict[str, object]:
    checks = []
    if conclusion is not None:
        checks.append({"name": CHECK, "conclusion": conclusion})
    return {
        "number": 7,
        "state": state,
        "baseRefOid": base,
        "headRefOid": head,
        "mergeCommit": {"oid": merge_sha} if merge_sha else None,
        "statusCheckRollup": checks,
    }


def _github(
    tmp_path: Path,
    *responses: subprocess.CompletedProcess[str],
    expected_base: str | None = "base",
) -> tuple[GhSyncGitHub, ScriptedRunner]:
    runner = ScriptedRunner(list(responses))
    return (
        GhSyncGitHub(
            repo_slug=REPO,
            required_check=CHECK,
            expected_base_sha=expected_base,
            runner=runner,
            cwd=tmp_path,
        ),
        runner,
    )


def test_evidence_reads_exact_pull_request_identity_and_required_check(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr()))

    evidence = github.evidence(7)

    assert evidence.number == 7
    assert evidence.state == "open"
    assert evidence.base_sha == "base"
    assert evidence.head_sha == "candidate"
    assert evidence.required_check == CHECK
    assert evidence.required_check_conclusion == "success"
    assert runner.calls[0].argv[:3] == ("gh", "pr", "view")


def test_merge_exact_rejects_changed_head(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(head="different")))

    with pytest.raises(SyncGitHubError, match="head changed"):
        github.merge_exact(7, expected_head="candidate")

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_green_exact_head_merges_without_admin(tmp_path: Path):
    github, runner = _github(
        tmp_path,
        _completed(_pr()),
        _completed(),
        _completed(_pr(state="MERGED", merge_sha="merge-sha")),
    )

    assert github.merge_exact(7, expected_head="candidate") == "merge-sha"
    argv = tuple(item for call in runner.calls for item in call.argv)
    assert "--admin" not in argv
    assert "--match-head-commit" in argv
    assert runner.calls[1].argv == (
        "gh",
        "pr",
        "merge",
        "7",
        "--repo",
        REPO,
        "--merge",
        "--match-head-commit",
        "candidate",
    )


@pytest.mark.parametrize("conclusion", ["PENDING", "FAILURE"])
def test_merge_exact_rejects_non_green_aggregate_check(
    tmp_path: Path,
    conclusion: str,
):
    github, runner = _github(tmp_path, _completed(_pr(conclusion=conclusion)))

    with pytest.raises(SyncGitHubError, match="required check is not green"):
        github.merge_exact(7, expected_head="candidate")

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_missing_aggregate_check(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(conclusion=None)))

    with pytest.raises(SyncGitHubError, match="required check is not green"):
        github.merge_exact(7, expected_head="candidate")

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_closed_pull_request(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(state="CLOSED")))

    with pytest.raises(SyncGitHubError, match="pull request is not open"):
        github.merge_exact(7, expected_head="candidate")

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_stale_base(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(base="stale")))

    with pytest.raises(SyncGitHubError, match="base changed"):
        github.merge_exact(7, expected_head="candidate")

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_requires_expected_base_sha(tmp_path: Path):
    github, runner = _github(
        tmp_path,
        _completed(_pr()),
        expected_base=None,
    )

    with pytest.raises(SyncGitHubError, match="expected base SHA is required"):
        github.merge_exact(7, expected_head="candidate")

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_missing_merge_sha(tmp_path: Path):
    github, _ = _github(
        tmp_path,
        _completed(_pr()),
        _completed(),
        _completed(_pr(state="MERGED")),
    )

    with pytest.raises(SyncGitHubError, match="merge SHA is missing"):
        github.merge_exact(7, expected_head="candidate")


def test_find_open_pull_request_rejects_duplicates(tmp_path: Path):
    github, _ = _github(
        tmp_path,
        _completed([{"number": 2}, {"number": 7}]),
    )

    with pytest.raises(SyncGitHubError, match="more than one open upstream sync PR"):
        github.find_open_pull_request("auto-sync/upstream", "main")


def test_create_pull_request_returns_number_from_url(tmp_path: Path):
    created = subprocess.CompletedProcess(
        [],
        0,
        "https://github.com/Oplink76/hermes-agent/pull/41\n",
        "",
    )
    github, runner = _github(tmp_path, created)

    number = github.create_pull_request(
        head="auto-sync/upstream",
        base="main",
        title="sync",
        body="body",
    )

    assert number == 41
    assert runner.calls[0].argv[:3] == ("gh", "pr", "create")


def test_update_pull_request_uses_normalized_edit_command(tmp_path: Path):
    github, runner = _github(tmp_path, _completed())

    github.update_pull_request(17, title="sync", body="body")

    assert runner.calls[0].argv[:4] == ("gh", "pr", "edit", "17")


def test_cli_failure_details_are_redacted(tmp_path: Path):
    failure = subprocess.CompletedProcess([], 1, "", "token ghp_do_not_expose")
    github, _ = _github(tmp_path, failure)

    with pytest.raises(SyncGitHubError, match="GitHub CLI pr view failed") as exc_info:
        github.evidence(7)

    assert "ghp_do_not_expose" not in str(exc_info.value)
