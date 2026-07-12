from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.github_authority import (
    GitHubAuthorityError,
    GitHubAuthorityReader,
    parse_pull_request_authority,
)


REPO = "Oplink76/hermes-agent"
CHECK = "All required checks pass"


def _payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "number": 7,
        "state": "MERGED",
        "mergedAt": "2026-07-12T20:00:00Z",
        "mergeCommit": {"oid": "d" * 40},
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "statusCheckRollup": [
            {
                "name": CHECK,
                "conclusion": "SUCCESS",
                "detailsUrl": (
                    f"https://github.com/{REPO}/actions/runs/101/job/202"
                ),
            }
        ],
    }
    values.update(updates)
    return values


def test_parser_returns_one_strict_typed_authority_record():
    authority = parse_pull_request_authority(
        _payload(), repo_slug=REPO, required_check=CHECK
    )

    assert authority.number == 7
    assert authority.state == "merged"
    assert authority.base_sha == "b" * 40
    assert authority.head_sha == "a" * 40
    assert authority.merge_sha == "d" * 40
    assert authority.workflow_run_id == 101
    assert authority.required_check_run_id == 202


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number", "7"),
        ("headRefOid", "a" * 39),
        ("baseRefOid", None),
        ("mergeCommit", {"oid": "short"}),
    ],
)
def test_parser_rejects_coercion_and_non_full_commit_ids(field: str, value: object):
    with pytest.raises(GitHubAuthorityError):
        parse_pull_request_authority(
            _payload(**{field: value}), repo_slug=REPO, required_check=CHECK
        )


def test_reader_uses_resolved_windows_cli_path(tmp_path: Path, monkeypatch):
    executable = tmp_path / "bin" / "gh.cmd"
    calls: list[list[str]] = []

    class Runner:
        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(_payload()), "")

    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.github_authority.shutil.which",
        lambda name: str(executable),
    )
    reader = GitHubAuthorityReader(
        repo_slug=REPO,
        required_check=CHECK,
        runner=Runner(),
        cwd=tmp_path,
    )

    assert reader.read(7).merge_sha == "d" * 40
    assert calls[0][:3] == [str(executable), "pr", "view"]
