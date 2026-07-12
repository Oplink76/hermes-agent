from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.sync_github import GhSyncGitHub, SyncGitHubError


REPO = "Oplink76/hermes-agent"
CHECK = "All required checks pass"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
OTHER_SHA = "c" * 40
MERGE_SHA = "d" * 40


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
    base: str = BASE_SHA,
    head: str = HEAD_SHA,
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
    expected_base: str | None = BASE_SHA,
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
    assert evidence.base_sha == BASE_SHA
    assert evidence.head_sha == HEAD_SHA
    assert evidence.required_check == CHECK
    assert evidence.required_check_conclusion == "success"
    assert runner.calls[0].argv[:3] == ("gh", "pr", "view")


def test_merge_exact_rejects_changed_head(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(head=OTHER_SHA)))

    with pytest.raises(SyncGitHubError, match="head changed"):
        github.merge_exact(7, expected_head=HEAD_SHA)

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_green_exact_head_merges_without_admin(tmp_path: Path):
    github, runner = _github(
        tmp_path,
        _completed(_pr()),
        _completed(),
        _completed(_pr(state="MERGED", merge_sha=MERGE_SHA)),
    )

    assert github.merge_exact(7, expected_head=HEAD_SHA) == MERGE_SHA
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
        HEAD_SHA,
    )


@pytest.mark.parametrize("conclusion", ["PENDING", "FAILURE"])
def test_merge_exact_rejects_non_green_aggregate_check(
    tmp_path: Path,
    conclusion: str,
):
    github, runner = _github(tmp_path, _completed(_pr(conclusion=conclusion)))

    with pytest.raises(SyncGitHubError, match="required check is not green"):
        github.merge_exact(7, expected_head=HEAD_SHA)

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_missing_aggregate_check(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(conclusion=None)))

    with pytest.raises(SyncGitHubError, match="required check is not green"):
        github.merge_exact(7, expected_head=HEAD_SHA)

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_closed_pull_request(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(state="CLOSED")))

    with pytest.raises(SyncGitHubError, match="pull request is not open"):
        github.merge_exact(7, expected_head=HEAD_SHA)

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_stale_base(tmp_path: Path):
    github, runner = _github(tmp_path, _completed(_pr(base=OTHER_SHA)))

    with pytest.raises(SyncGitHubError, match="base changed"):
        github.merge_exact(7, expected_head=HEAD_SHA)

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_requires_expected_base_sha(tmp_path: Path):
    github, runner = _github(
        tmp_path,
        _completed(_pr()),
        expected_base=None,
    )

    with pytest.raises(SyncGitHubError, match="expected base SHA is required"):
        github.merge_exact(7, expected_head=HEAD_SHA)

    assert all(call.argv[2] != "merge" for call in runner.calls)


def test_merge_exact_rejects_missing_merge_sha(tmp_path: Path):
    github, _ = _github(
        tmp_path,
        _completed(_pr()),
        _completed(),
        _completed(_pr(state="MERGED")),
    )

    with pytest.raises(SyncGitHubError, match="merge SHA is missing"):
        github.merge_exact(7, expected_head=HEAD_SHA)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseRefOid", None),
        ("baseRefOid", 7),
        ("baseRefOid", "a" * 39),
        ("baseRefOid", "g" * 40),
        ("headRefOid", None),
        ("headRefOid", 7),
        ("headRefOid", "b" * 39),
        ("headRefOid", "g" * 40),
    ],
)
def test_evidence_rejects_malformed_base_and_head_oids(
    tmp_path: Path,
    field: str,
    value: object,
):
    payload = _pr()
    payload[field] = value
    github, _ = _github(tmp_path, _completed(payload))

    with pytest.raises(SyncGitHubError, match="evidence was incomplete"):
        github.evidence(7)


@pytest.mark.parametrize(
    "merge_commit",
    [
        {"oid": None},
        {"oid": 7},
        {"oid": "d" * 39},
        {"oid": "g" * 40},
        {},
        MERGE_SHA,
    ],
)
def test_evidence_rejects_malformed_merge_oid(
    tmp_path: Path,
    merge_commit: object,
):
    payload = _pr()
    payload["mergeCommit"] = merge_commit
    github, _ = _github(tmp_path, _completed(payload))

    with pytest.raises(SyncGitHubError, match="evidence was incomplete"):
        github.evidence(7)


@pytest.mark.parametrize(
    ("field", "value"),
    [("number", "7"), ("number", True), ("state", 7), ("state", None)],
)
def test_evidence_rejects_wrong_scalar_source_types(
    tmp_path: Path,
    field: str,
    value: object,
):
    payload = _pr()
    payload[field] = value
    github, _ = _github(tmp_path, _completed(payload))

    with pytest.raises(SyncGitHubError, match="evidence was incomplete"):
        github.evidence(7)


@pytest.mark.parametrize(
    "duplicate",
    [
        {"context": CHECK, "state": "SUCCESS"},
        {"context": CHECK, "state": "FAILURE"},
        {"name": "another check", "context": CHECK, "state": "FAILURE"},
    ],
)
def test_evidence_rejects_duplicate_configured_aggregate_checks(
    tmp_path: Path,
    duplicate: dict[str, str],
):
    payload = _pr()
    payload["statusCheckRollup"] = [
        {"name": CHECK, "conclusion": "SUCCESS"},
        duplicate,
    ]
    github, _ = _github(tmp_path, _completed(payload))

    with pytest.raises(SyncGitHubError, match="required check evidence is ambiguous"):
        github.evidence(7)


@pytest.mark.parametrize(
    "check",
    [
        {"name": CHECK, "conclusion": 0},
        {"name": CHECK, "conclusion": "SUCCESS", "state": 0},
    ],
)
def test_evidence_rejects_wrong_configured_check_source_types(
    tmp_path: Path,
    check: dict[str, object],
):
    payload = _pr()
    payload["statusCheckRollup"] = [check]
    github, _ = _github(tmp_path, _completed(payload))

    with pytest.raises(SyncGitHubError, match="evidence was incomplete"):
        github.evidence(7)


def test_evidence_crosses_real_subprocess_runner_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    driver = tmp_path / "fake_gh.py"
    driver.write_text(
        "\n".join([
            "import json",
            "import sys",
            'if sys.argv[1:4] != ["pr", "view", "7"]:',
            "    raise SystemExit(9)",
            f"print({json.dumps(json.dumps(_pr()))})",
        ])
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = bin_dir / "gh.cmd"
        launcher.write_text(
            f'@"{sys.executable}" "{driver}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = bin_dir / "gh"
        launcher.write_text(
            f"#!{sys.executable}\nexec(compile(open({str(driver)!r}).read(), "
            f"{str(driver)!r}, 'exec'))\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    github = GhSyncGitHub(
        repo_slug=REPO,
        required_check=CHECK,
        expected_base_sha=BASE_SHA,
        runner=SubprocessCommandRunner(),
        cwd=tmp_path,
    )

    evidence = github.evidence(7)

    assert evidence.base_sha == BASE_SHA
    assert evidence.head_sha == HEAD_SHA


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
