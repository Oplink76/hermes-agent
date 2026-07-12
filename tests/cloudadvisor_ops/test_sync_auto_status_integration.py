from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.upstream_sync_status import SyncStatus
from ops.cloudadvisor.hermes_ops import cli
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncResult,
    AutonomousSyncState,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _status_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(
        repo,
        "-c",
        "user.name=Sync Test",
        "-c",
        "user.email=sync@example.invalid",
        "commit",
        "-m",
        "base",
    )
    installed_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "upstream-work")
    (repo / "upstream.txt").write_text("upstream", encoding="utf-8")
    _git(repo, "add", "upstream.txt")
    _git(
        repo,
        "-c",
        "user.name=Sync Test",
        "-c",
        "user.email=sync@example.invalid",
        "commit",
        "-m",
        "upstream",
    )
    upstream_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "update-ref", "refs/remotes/origin/main", installed_sha)
    _git(repo, "update-ref", "refs/remotes/upstream/main", upstream_sha)
    return repo, installed_sha


def _config(tmp_path: Path, repo: Path) -> Path:
    config = tmp_path / "operations.yaml"
    config.write_text(
        "\n".join([
            "environment: production",
            "sync:",
            f"  repo: {repo}",
            f"  worktree: {tmp_path / 'candidate'}",
            "  origin: origin",
            "  upstream: upstream",
            "  candidate_branch: auto-sync/upstream",
            "  repo_slug: Oplink76/hermes-agent",
            f"  lock_path: {tmp_path / 'sync.lock'}",
            f"  receipt_root: {tmp_path / 'receipts'}",
            f"  status_file: {tmp_path / 'sync-status.json'}",
            f"  notification_store: {tmp_path / 'notifications.json'}",
            "  required_check: All required checks pass",
            "  check_timeout_seconds: 10",
            "  poll_interval_seconds: 1",
            "  resolver_backend: codex",
            "  reviewer_backend: claude",
            "runtime:",
            f"  install_root: {repo}",
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
            f"  record_root: {tmp_path / 'deployments'}",
            f"  snapshot_root: {tmp_path / 'snapshots'}",
            "  hermes_homes:",
            f"    - {tmp_path / 'home'}",
            "  preservation_command: [python, verify.py]",
            "  required_check: All required checks pass",
            "  uv_extras: [all, dev]",
            "  postinstall_commands: []",
        ])
        + "\n",
        encoding="utf-8",
    )
    return config


def test_sync_auto_publishes_status_api_and_deduplicated_alert_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, installed_sha = _status_repo(tmp_path)
    config = _config(tmp_path, repo)
    needs_ole = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        candidate_sha="c" * 40,
        pr_number=7,
        needs_ole=True,
        reason="major conflict",
    )
    outcomes = iter([
        needs_ole,
        AutonomousSyncResult(state=AutonomousSyncState.LOCKED),
        needs_ole,
        AutonomousSyncResult(state=AutonomousSyncState.NO_CHANGE),
        needs_ole,
    ])
    monkeypatch.setattr(cli, "load_conflict_resolver", lambda path: object())
    monkeypatch.setattr(cli, "load_conflict_reviewer", lambda path, runner: object())
    monkeypatch.setattr(cli, "GhSyncGitHub", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_sync_remediator", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_sync_deploy_fn", lambda *args: lambda *ignored: object())
    monkeypatch.setattr(cli, "_sync_runtime_verify_fn", lambda *args: lambda sha: True)
    def run_with_publication(*args, **kwargs):
        result = next(outcomes)
        if result.state is AutonomousSyncState.LOCKED:
            return result
        notify_ole = kwargs["publish_outcome"](result)
        return replace(result, notify_ole=notify_ole)

    monkeypatch.setattr(cli, "run_autonomous_sync", run_with_publication)

    assert cli.main(["sync-auto", "--config", str(config)]) == 2
    first = json.loads(capsys.readouterr().out)
    assert first["notify_ole"] is True
    assert "ole_notified" not in first
    status = SyncStatus.load(tmp_path / "sync-status.json")
    assert status.upstream_behind == 1
    assert status.fork_behind == 0
    assert status.installed_sha == installed_sha
    assert status.fork_main_sha == installed_sha
    assert status.sync_pr_number == 7
    assert status.sync_state == "NEEDS_OLE"

    monkeypatch.setattr(
        web_server,
        "_upstream_sync_status_path",
        lambda: tmp_path / "sync-status.json",
    )
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 0)
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    api = client.get("/api/hermes/update/check").json()
    assert api["behind"] == 0
    assert api["fork_behind"] == 0
    assert api["upstream_behind"] == 1
    assert api["sync_state"] == "NEEDS_OLE"
    assert api["message"].endswith("needs attention")

    status_before_locked = (tmp_path / "sync-status.json").read_bytes()
    notifications_before_locked = (tmp_path / "notifications.json").read_bytes()
    assert cli.main(["sync-auto", "--config", str(config)]) == 75
    assert json.loads(capsys.readouterr().out)["notify_ole"] is False
    assert (tmp_path / "sync-status.json").read_bytes() == status_before_locked
    assert (
        tmp_path / "notifications.json"
    ).read_bytes() == notifications_before_locked
    assert cli.main(["sync-auto", "--config", str(config)]) == 2
    assert json.loads(capsys.readouterr().out)["notify_ole"] is False
    assert cli.main(["sync-auto", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["notify_ole"] is False
    assert cli.main(["sync-auto", "--config", str(config)]) == 2
    assert json.loads(capsys.readouterr().out)["notify_ole"] is True
