from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

from ops.cloudadvisor.hermes_ops import cli
from ops.cloudadvisor.hermes_ops.cli import (
    load_operations_config,
    load_sync_config,
    load_sync_policy_config,
)
from ops.cloudadvisor.hermes_ops.health import HealthCheck, HealthReport
from ops.cloudadvisor.hermes_ops.sync import SyncResult, SyncState
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncResult,
    AutonomousSyncState,
)


class FakeRunner:
    def __init__(self, response: subprocess.CompletedProcess[str]):
        self.response = response
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        self.calls.append((tuple(argv), Path(cwd)))
        return self.response


def test_sync_auto_returns_terminal_state_exit_codes(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "ops.yaml"
    config_path.write_text("sync: {}\n", encoding="utf-8")
    sync_config = SimpleNamespace(repo_slug="Oplink76/hermes-agent", repo=tmp_path)
    policy = SimpleNamespace(
        receipt_root=tmp_path / "receipts",
        required_check="All required checks pass",
        check_timeout_seconds=10,
        poll_interval_seconds=1,
        resolver_backend="codex",
    )
    operations = cli.OperationsConfig(
        environment="production",
        install_root=tmp_path,
        uid=501,
        services=(),
        gateway_targets=(),
        deploy_config=cli.DeployConfig(
            install_root=tmp_path,
            origin="origin",
            record_root=tmp_path / "records",
            required_check="All required checks pass",
        ),
        repo_slug="Oplink76/hermes-agent",
        snapshot_root=tmp_path / "snapshots",
        hermes_homes=(),
        preservation_command=("false",),
    )
    monkeypatch.setattr(cli, "load_sync_config", lambda path: sync_config)
    monkeypatch.setattr(cli, "load_sync_policy_config", lambda path: policy)
    monkeypatch.setattr(cli, "load_operations_config", lambda path: operations)
    monkeypatch.setattr(cli, "load_conflict_resolver", lambda path: object())
    monkeypatch.setattr(cli, "load_conflict_reviewer", lambda path, runner: object())
    monkeypatch.setattr(cli, "GhSyncGitHub", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_sync_remediator", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "SubprocessCommandRunner", lambda: object())
    monkeypatch.setattr(
        cli,
        "_sync_deploy_fn",
        lambda *args, **kwargs: (lambda *deploy_args: object()),
    )
    monkeypatch.setattr(
        cli,
        "_sync_runtime_verify_fn",
        lambda *args, **kwargs: (lambda sha: True),
    )

    for state, expected in (
        (AutonomousSyncState.DEPLOYED, 0),
        (AutonomousSyncState.ROLLED_BACK_REVERTED, 0),
        (AutonomousSyncState.NO_CHANGE, 0),
        (AutonomousSyncState.REFRESH_REQUIRED, 75),
        (AutonomousSyncState.PENDING_REFRESH, 75),
        (AutonomousSyncState.LOCKED, 75),
        (AutonomousSyncState.NEEDS_OLE, 2),
    ):
        monkeypatch.setattr(
            cli,
            "run_autonomous_sync",
            lambda *args, _state=state, **kwargs: AutonomousSyncResult(
                state=_state,
                needs_ole=_state is AutonomousSyncState.NEEDS_OLE,
            ),
        )
        assert cli.main(["sync-auto", "--config", str(config_path)]) == expected


def _write_operations_config(
    tmp_path: Path,
    *,
    environment: str = "production",
) -> Path:
    config_file = tmp_path / "hermes-operations.yaml"
    config_file.write_text(
        "\n".join([
            f"environment: {environment}",
            "sync:",
            f"  receipt_root: {tmp_path / 'sync-receipts'}",
            "  required_check: All required checks pass",
            "  check_timeout_seconds: 2700",
            "  poll_interval_seconds: 15",
            "  resolver_backend: codex",
            "  reviewer_backend: claude",
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
            "  uv_extras: [all, dev, slack]",
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


def test_load_sync_policy_config_reads_exact_authority_settings(tmp_path: Path):
    config_file = tmp_path / "hermes-operations.yaml"
    config_file.write_text(
        "\n".join([
            "sync:",
            f"  receipt_root: {tmp_path / 'receipts'}",
            "  required_check: All required checks pass",
            "  check_timeout_seconds: 2700",
            "  poll_interval_seconds: 15",
            "  resolver_backend: codex",
            "  reviewer_backend: claude",
        ])
        + "\n",
        encoding="utf-8",
    )

    policy = load_sync_policy_config(config_file)

    assert policy.receipt_root == (tmp_path / "receipts").resolve()
    assert policy.required_check == "All required checks pass"
    assert policy.check_timeout_seconds == 2700
    assert policy.poll_interval_seconds == 15
    assert policy.resolver_backend == "codex"
    assert policy.reviewer_backend == "claude"
    assert policy.resolver_backend.casefold() != policy.reviewer_backend.casefold()


@pytest.mark.parametrize(
    ("resolver", "reviewer"),
    [("Codex", "claude"), ("codex", "Claude"), ("other", "claude")],
)
def test_sync_policy_requires_canonical_actual_backend_ids(
    tmp_path: Path, resolver: str, reviewer: str
):
    config_file = tmp_path / "hermes-operations.yaml"
    config_file.write_text(
        "\n".join([
            "sync:",
            f"  receipt_root: {tmp_path / 'receipts'}",
            "  required_check: All required checks pass",
            "  check_timeout_seconds: 2700",
            "  poll_interval_seconds: 15",
            f"  resolver_backend: {resolver}",
            f"  reviewer_backend: {reviewer}",
        ])
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="codex.*claude"):
        load_sync_policy_config(config_file)


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
    assert config.deploy_config.uv_extras == ("all", "dev", "slack")
    assert config.deploy_config.postinstall_commands == (("python", "migrate.py"),)


def test_load_operations_config_requires_gateway_identity_scope(tmp_path: Path):
    config_file = _write_operations_config(tmp_path)
    payload = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    payload["runtime"]["gateways"] = []
    config_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="at least one gateway"):
        load_operations_config(config_file)


def test_load_operations_config_requires_nonempty_uv_extras(tmp_path: Path):
    config_file = _write_operations_config(tmp_path)
    payload = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    payload["deploy"].pop("uv_extras")
    config_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="deploy.uv_extras must contain"):
        load_operations_config(config_file)


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
    constructed = {}

    class FakeGitHub:
        def __init__(self, repo_slug, runner, cwd):
            constructed.update(repo_slug=repo_slug, runner=runner, cwd=cwd)

    monkeypatch.setattr(cli, "GhSyncGitHub", FakeGitHub)

    exit_code = cli.main(["sync", "--config", str(config_file)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "NO_CHANGE"
    assert payload["transitions"] == ["LOCKED", "FETCHED", "NO_CHANGE"]
    assert constructed["repo_slug"] == "Oplink76/hermes-agent"
    assert constructed["cwd"] == (tmp_path / "repo").resolve()


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


def test_deploy_sync_cli_wires_machine_authority_without_human_artifact(
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
            to_dict=lambda: {"id": "deploy-sync-1", "status": "deployed"},
        )

    monkeypatch.setattr(cli, "run_deploy", fake_deploy)
    receipt = tmp_path / "sync-receipt.json"

    exit_code = cli.main([
        "deploy-sync",
        "--config",
        str(config_file),
        "--sha",
        "merged-sha",
        "--pr-number",
        "7",
        "--sync-receipt",
        str(receipt),
    ])

    assert exit_code == 0
    assert captured["request"].sha == "merged-sha"
    assert captured["request"].actor == "hermes-upstream-sync"
    assert captured["request"].authority_kind == "automated_sync"
    assert captured["request"].authority_record == receipt
    assert captured["request"].approval_record is None
    assert captured["config"].sync_receipt_root == (
        tmp_path / "sync-receipts"
    ).resolve()
    assert captured["runner"] is runner
    assert captured["services"].kind == "services"
    assert captured["health"].kind == "health"
    assert captured["snapshots"].kind == "snapshots"
    assert captured["github"].kind == "github"
    assert json.loads(capsys.readouterr().out)["status"] == "deployed"


def test_deploy_sync_cli_rejects_crossed_sync_and_deploy_check_policy(
    tmp_path: Path,
):
    config_file = _write_operations_config(tmp_path)
    payload = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    payload["sync"]["required_check"] = "Different check"
    config_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="required_check settings must be identical"):
        cli.main([
            "deploy-sync",
            "--config",
            str(config_file),
            "--sha",
            "merged-sha",
            "--pr-number",
            "7",
            "--sync-receipt",
            str(tmp_path / "receipt.json"),
        ])
