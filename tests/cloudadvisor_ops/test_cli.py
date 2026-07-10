from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops import cli
from ops.cloudadvisor.hermes_ops.cli import GhGitHub, load_sync_config
from ops.cloudadvisor.hermes_ops.sync import SyncResult, SyncState


class FakeRunner:
    def __init__(self, response: subprocess.CompletedProcess[str]):
        self.response = response
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        self.calls.append((tuple(argv), Path(cwd)))
        return self.response


def test_load_sync_config_resolves_paths_and_fixed_candidate(tmp_path: Path):
    config_file = tmp_path / "hermes-operations.yaml"
    config_file.write_text(
        "\n".join(
            [
                "sync:",
                f"  repo: {tmp_path / 'repo'}",
                f"  worktree: {tmp_path / 'candidate'}",
                "  origin: origin",
                "  upstream: upstream",
                "  candidate_branch: auto-sync/upstream",
                "  repo_slug: Oplink76/hermes-agent",
                f"  lock_path: {tmp_path / 'sync.lock'}",
            ]
        )
        + "\n"
    )

    config = load_sync_config(config_file)

    assert config.repo == (tmp_path / "repo").resolve()
    assert config.worktree == (tmp_path / "candidate").resolve()
    assert config.candidate_branch == "auto-sync/upstream"


def test_github_query_rejects_duplicate_open_sync_pull_requests(tmp_path: Path):
    response = subprocess.CompletedProcess(
        [],
        0,
        json.dumps([{"number": 2}, {"number": 7}]),
        "",
    )
    github = GhGitHub("Oplink76/hermes-agent", FakeRunner(response), tmp_path)

    with pytest.raises(RuntimeError, match="more than one open upstream sync PR"):
        github.find_open_pull_request("auto-sync/upstream", "main")


def test_github_create_returns_number_from_created_pull_request_url(tmp_path: Path):
    response = subprocess.CompletedProcess(
        [],
        0,
        "https://github.com/Oplink76/hermes-agent/pull/41\n",
        "",
    )
    runner = FakeRunner(response)
    github = GhGitHub("Oplink76/hermes-agent", runner, tmp_path)

    number = github.create_pull_request(
        head="auto-sync/upstream",
        base="main",
        title="sync",
        body="body",
    )

    assert number == 41
    assert runner.calls[0][0][:3] == ("gh", "pr", "create")


def test_github_update_surfaces_cli_failure(tmp_path: Path):
    response = subprocess.CompletedProcess([], 1, "", "permission denied")
    github = GhGitHub("Oplink76/hermes-agent", FakeRunner(response), tmp_path)

    with pytest.raises(RuntimeError, match="permission denied"):
        github.update_pull_request(17, title="sync", body="body")


def test_sync_cli_prints_machine_readable_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    config_file = tmp_path / "hermes-operations.yaml"
    config_file.write_text(
        "\n".join(
            [
                "sync:",
                f"  repo: {tmp_path / 'repo'}",
                f"  worktree: {tmp_path / 'candidate'}",
                "  origin: origin",
                "  upstream: upstream",
                "  candidate_branch: auto-sync/upstream",
                "  repo_slug: Oplink76/hermes-agent",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        cli,
        "run_sync",
        lambda config, runner, github: SyncResult(
            state=SyncState.NO_CHANGE,
            base_sha="base",
            upstream_sha="upstream",
            transitions=(SyncState.LOCKED, SyncState.FETCHED, SyncState.NO_CHANGE),
        ),
    )

    exit_code = cli.main(["sync", "--config", str(config_file)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "NO_CHANGE"
    assert payload["transitions"] == ["LOCKED", "FETCHED", "NO_CHANGE"]
