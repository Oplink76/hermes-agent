from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from ops.cloudadvisor.hermes_ops.config_migration import (
    migrate_operations_config,
    rollback_operations_config,
)
from ops.cloudadvisor.hermes_ops.sync_preflight import run_sync_preflight


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _july_10_config(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    hermes_root = tmp_path / ".hermes"
    install_root = hermes_root / "hermes-agent"
    install_root.mkdir(parents=True)
    _git(install_root, "init", "-b", "main")
    _git(
        install_root,
        "remote",
        "add",
        "origin",
        "https://github.com/Oplink76/hermes-agent.git",
    )
    _git(
        install_root,
        "remote",
        "add",
        "upstream",
        "https://github.com/NousResearch/hermes-agent.git",
    )
    preservation = _executable(tmp_path / "work" / "verify_package_1.py")
    codex = _executable(tmp_path / "bin" / "codex")
    payload: dict[str, object] = {
        "environment": "production",
        "sync": {
            "repo": str(install_root),
            "worktree": str(hermes_root / "worktrees" / "hermes-upstream-sync"),
            "origin": "origin",
            "upstream": "upstream",
            "candidate_branch": "auto-sync/upstream",
            "repo_slug": "Oplink76/hermes-agent",
            "lock_path": str(hermes_root / "locks" / "upstream-sync.lock"),
            "conflict_resolver": {
                "codex_executable": str(codex),
                "prompt": (
                    "Resolve only the current Git merge conflicts. "
                    "Do not commit, push, or change remotes."
                ),
            },
        },
        "runtime": {
            "install_root": str(install_root),
            "uid": 501,
            "services": [
                {
                    "label": "ai.hermes.gateway",
                    "plist": str(tmp_path / "LaunchAgents" / "ai.hermes.gateway.plist"),
                },
                {
                    "label": "com.cloudadvisor.hermes-dashboard",
                    "plist": str(
                        tmp_path
                        / "LaunchAgents"
                        / "com.cloudadvisor.hermes-dashboard.plist"
                    ),
                },
            ],
            "gateways": [
                {
                    "profile": "default",
                    "hermes_home": str(hermes_root),
                    "plist": str(tmp_path / "LaunchAgents" / "ai.hermes.gateway.plist"),
                }
            ],
            "unrelated_runtime_value": "must-survive",
        },
        "deploy": {
            "origin": "origin",
            "repo_slug": "Oplink76/hermes-agent",
            "record_root": str(hermes_root / "recovery" / "deployments"),
            "lock_path": str(hermes_root / "locks" / "deploy.lock"),
            "snapshot_root": str(hermes_root / "recovery" / "predeploy"),
            "hermes_homes": [str(hermes_root)],
            "preservation_command": [
                str(install_root / ".venv" / "bin" / "python"),
                str(preservation),
                "--recovery",
                str(hermes_root / "recovery" / "20260710"),
                "--remote",
                str(tmp_path / "Hermes Recovery" / "20260710"),
            ],
            "required_approver": "Ole Ørum-Petersen",
            "required_check": "All required checks pass",
            "uv_extras": ["all", "dev", "slack"],
            "postinstall_commands": [
                [".venv/bin/python", "scripts/docker_config_migrate.py"]
            ],
            "unrelated_deploy_value": {"keep": True},
        },
        "unrelated_top_level": ["preserve", "exactly"],
    }
    _executable(install_root / ".venv" / "bin" / "python")
    _executable(install_root / ".venv" / "bin" / "hermes")
    config_path = hermes_root / "operations" / "hermes-operations.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o640)
    return config_path, payload


def test_versioned_migration_preserves_july_10_config_and_rolls_back_exactly(
    tmp_path: Path,
) -> None:
    config_path, original = _july_10_config(tmp_path)
    original_bytes = config_path.read_bytes()
    original_mode = stat.S_IMODE(config_path.stat().st_mode)
    backup_dir = tmp_path / "operator-backups"
    claude = _executable(tmp_path / "bin" / "claude")

    record = migrate_operations_config(
        config_path,
        backup_dir=backup_dir,
        claude_executable=claude,
    )

    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    sync = migrated["sync"]
    hermes_root = config_path.parents[1]
    assert sync["receipt_root"] == str(hermes_root / "recovery/sync-receipts")
    assert sync["status_file"] == str(hermes_root / "recovery/sync-status.json")
    assert sync["notification_store"] == str(
        hermes_root / "recovery/sync-notifications.json"
    )
    assert sync["delivery_command"] == [
        str(hermes_root / "hermes-agent/.venv/bin/hermes"),
        "send",
        "--to",
        "slack:C0BFLTFC2LS",
        "--file",
        "-",
        "--quiet",
    ]
    assert sync["required_check"] == "All required checks pass"
    assert sync["check_timeout_seconds"] == 2700
    assert sync["poll_interval_seconds"] == 15
    assert sync["resolver_backend"] == "codex"
    assert sync["reviewer_backend"] == "claude"
    assert sync["conflict_resolver"] == original["sync"]["conflict_resolver"]
    assert sync["conflict_reviewer"] == {"claude_executable": str(claude)}
    assert "decision_root" not in sync
    assert Path(sync["receipt_root"]) / "decision-packets" == (
        hermes_root / "recovery/sync-receipts/decision-packets"
    )
    assert migrated["runtime"] == original["runtime"]
    assert migrated["deploy"] == original["deploy"]
    assert migrated["unrelated_top_level"] == original["unrelated_top_level"]
    assert stat.S_IMODE(config_path.stat().st_mode) == original_mode
    assert record.schema_version == 1
    assert record.original_sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert record.backup_path.read_bytes() == original_bytes
    manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    assert manifest["original_sha256"] == record.original_sha256
    assert manifest["migrated_sha256"] == hashlib.sha256(
        config_path.read_bytes()
    ).hexdigest()

    rollback_operations_config(record.manifest_path)

    assert config_path.read_bytes() == original_bytes
    assert stat.S_IMODE(config_path.stat().st_mode) == original_mode


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["deploy"].__setitem__(
            "preservation_command", ["/usr/bin/false"]
        ),
        lambda payload: payload["deploy"].__setitem__("uv_extras", ["all", "dev"]),
    ],
)
def test_migration_rejects_placeholder_preservation_or_reduced_extras(
    tmp_path: Path, mutate
) -> None:
    config_path, _ = _july_10_config(tmp_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    mutate(payload)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="preservation_command|uv_extras"):
        migrate_operations_config(
            config_path,
            backup_dir=tmp_path / "backups",
            claude_executable=tmp_path / "bin" / "claude",
        )

    assert not (tmp_path / "backups").exists()


def test_sync_preflight_is_read_only_and_validates_tools_remotes_and_roots(
    tmp_path: Path,
) -> None:
    config_path, _ = _july_10_config(tmp_path)
    claude = _executable(tmp_path / "bin" / "claude")
    record = migrate_operations_config(
        config_path,
        backup_dir=tmp_path / "backups",
        claude_executable=claude,
    )
    gh = _executable(tmp_path / "bin" / "gh")
    recovery = config_path.parents[1] / "recovery"
    recovery.mkdir(exist_ok=True)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in config_path.parents[1].rglob("*")
        if path.is_file()
    }

    report = run_sync_preflight(config_path, which=lambda name: str(gh))

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in config_path.parents[1].rglob("*")
        if path.is_file()
    }
    assert report.ok is True
    assert set(report.checks) == {
        "config",
        "runtime_scope",
        "repo_identity",
        "git_remotes",
        "state_paths",
        "gh",
        "codex",
        "claude",
        "delivery",
        "preservation",
    }
    assert before == after
    assert not (recovery / "sync-receipts").exists()
    assert record.manifest_path.exists()


def test_sync_preflight_rejects_wrong_official_upstream_without_mutation(
    tmp_path: Path,
) -> None:
    config_path, _ = _july_10_config(tmp_path)
    claude = _executable(tmp_path / "bin" / "claude")
    migrate_operations_config(
        config_path,
        backup_dir=tmp_path / "backups",
        claude_executable=claude,
    )
    repo = config_path.parents[1] / "hermes-agent"
    _git(repo, "remote", "set-url", "upstream", "https://example.invalid/wrong.git")
    gh = _executable(tmp_path / "bin" / "gh")
    config_before = config_path.read_bytes()

    with pytest.raises(ValueError, match="official upstream remote"):
        run_sync_preflight(config_path, which=lambda name: str(gh))

    assert config_path.read_bytes() == config_before
