from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

from ops.cloudadvisor.hermes_ops import cli
from ops.cloudadvisor.hermes_ops.cli import (
    GhGitHub,
    load_operations_config,
    load_sync_config,
)
from ops.cloudadvisor.hermes_ops.health import HealthCheck, HealthReport
from ops.cloudadvisor.hermes_ops.sync import SyncResult, SyncState


class FakeRunner:
    def __init__(self, response: subprocess.CompletedProcess[str]):
        self.response = response
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        self.calls.append((tuple(argv), Path(cwd)))
        return self.response


def _write_operations_config(
    tmp_path: Path,
    *,
    environment: str = "production",
) -> Path:
    config_file = tmp_path / "hermes-operations.yaml"
    config_file.write_text(
        "\n".join([
            f"environment: {environment}",
            "runtime:",
            f"  install_root: {tmp_path / 'repo'}",
            "  uid: 501",
            "  services:",
            "    - label: ai.hermes.gateway",
            f"      plist: {tmp_path / 'gateway.plist'}",
            "  gateways:",
            "    - profile: default",
            f"      hermes_home: {tmp_path / 'home'}",
            f"      plist: {tmp_path / 'gateway.plist'}",
            "deploy:",
            "  origin: origin",
            "  repo_slug: Oplink76/hermes-agent",
            f"  record_root: {tmp_path / 'records'}",
            f"  snapshot_root: {tmp_path / 'snapshots'}",
            "  hermes_homes:",
            f"    - {tmp_path / 'home'}",
            "  preservation_command: [python, verify-preservation.py]",
            "  postinstall_commands:",
            "    - [python, migrate.py]",
        ])
        + "\n",
        encoding="utf-8",
    )
    return config_file


def test_load_sync_config_resolves_paths_and_fixed_candidate(tmp_path: Path):
    config_file = tmp_path / "hermes-operations.yaml"
    config_file.write_text(
        "\n".join([
            "sync:",
            f"  repo: {tmp_path / 'repo'}",
            f"  worktree: {tmp_path / 'candidate'}",
            "  origin: origin",
            "  upstream: upstream",
            "  candidate_branch: auto-sync/upstream",
            "  repo_slug: Oplink76/hermes-agent",
            f"  lock_path: {tmp_path / 'sync.lock'}",
        ])
        + "\n"
    )

    config = load_sync_config(config_file)

    assert config.repo == (tmp_path / "repo").resolve()
    assert config.worktree == (tmp_path / "candidate").resolve()
    assert config.candidate_branch == "auto-sync/upstream"


def test_load_operations_config_builds_explicit_runtime_and_deploy_scope(
    tmp_path: Path,
):
    config = load_operations_config(_write_operations_config(tmp_path))

    assert config.environment == "production"
    assert config.install_root == (tmp_path / "repo").resolve()
    assert config.services[0].label == "ai.hermes.gateway"
    assert config.gateway_targets[0].profile == "default"
    assert config.snapshot_root == (tmp_path / "snapshots").resolve()
    assert config.hermes_homes == ((tmp_path / "home").resolve(),)
    assert config.deploy_config.postinstall_commands == (("python", "migrate.py"),)


def test_load_operations_config_requires_gateway_identity_scope(tmp_path: Path):
    config_file = _write_operations_config(tmp_path)
    payload = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    payload["runtime"]["gateways"] = []
    config_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="at least one gateway"):
        load_operations_config(config_file)


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
        "\n".join([
            "sync:",
            f"  repo: {tmp_path / 'repo'}",
            f"  worktree: {tmp_path / 'candidate'}",
            "  origin: origin",
            "  upstream: upstream",
            "  candidate_branch: auto-sync/upstream",
            "  repo_slug: Oplink76/hermes-agent",
        ])
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


def test_health_cli_reports_mandatory_matrix_and_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    config_file = _write_operations_config(tmp_path)

    class FakeHealthChecker:
        def __init__(self, **kwargs):
            assert kwargs["inject_failure"] is None

        def check(self, *, expected_sha: str, services: tuple[str, ...]):
            assert expected_sha == "approved-sha"
            assert services == ("ai.hermes.gateway",)
            return HealthReport(
                checks=(HealthCheck("service:ai.hermes.gateway", True, "healthy"),)
            )

    monkeypatch.setattr(cli, "SubprocessCommandRunner", lambda: object())
    monkeypatch.setattr(cli, "LaunchdServiceController", lambda **kwargs: object())
    monkeypatch.setattr(cli, "RuntimeHealthChecker", FakeHealthChecker)

    exit_code = cli.main([
        "health",
        "--config",
        str(config_file),
        "--sha",
        "approved-sha",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["expected_sha"] == "approved-sha"
    assert payload["checks"][0]["mandatory"] is True


def test_deploy_cli_rejects_production_failure_injection_for_new_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    config_file = _write_operations_config(tmp_path)
    monkeypatch.setattr(cli, "current_checkout_sha", lambda root, runner: "old-sha")

    exit_code = cli.main([
        "deploy",
        "--config",
        str(config_file),
        "--sha",
        "new-sha",
        "--pr-number",
        "41",
        "--approval-record",
        str(tmp_path / "approval.json"),
        "--actor",
        "operator",
        "--inject-health-failure",
        "after_restart",
    ])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "rejected"
    assert "recovery_canary" in payload["error"]


def test_deploy_cli_wires_production_adapters_and_prints_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    config_file = _write_operations_config(tmp_path)
    captured = {}
    runner = object()
    monkeypatch.setattr(cli, "SubprocessCommandRunner", lambda: runner)
    monkeypatch.setattr(
        cli,
        "LaunchdServiceController",
        lambda **kwargs: SimpleNamespace(kind="services", kwargs=kwargs),
    )
    monkeypatch.setattr(
        cli,
        "RuntimeHealthChecker",
        lambda **kwargs: SimpleNamespace(kind="health", kwargs=kwargs),
    )
    monkeypatch.setattr(
        cli,
        "SnapshotCoordinator",
        lambda **kwargs: SimpleNamespace(kind="snapshots", kwargs=kwargs),
    )
    monkeypatch.setattr(
        cli,
        "GhReleaseVerifier",
        lambda **kwargs: SimpleNamespace(kind="github", kwargs=kwargs),
    )

    def fake_deploy(request, **kwargs):
        captured["request"] = request
        captured.update(kwargs)
        return SimpleNamespace(
            status="deployed",
            to_dict=lambda: {"id": "deploy-1", "status": "deployed"},
        )

    monkeypatch.setattr(cli, "run_deploy", fake_deploy)

    exit_code = cli.main([
        "deploy",
        "--config",
        str(config_file),
        "--sha",
        "new-sha",
        "--pr-number",
        "41",
        "--approval-record",
        str(tmp_path / "approval.json"),
        "--actor",
        "operator",
    ])

    assert exit_code == 0
    assert captured["request"].sha == "new-sha"
    assert captured["runner"] is runner
    assert captured["services"].kind == "services"
    assert captured["health"].kind == "health"
    assert captured["snapshots"].kind == "snapshots"
    assert captured["github"].kind == "github"
    assert json.loads(capsys.readouterr().out)["status"] == "deployed"
