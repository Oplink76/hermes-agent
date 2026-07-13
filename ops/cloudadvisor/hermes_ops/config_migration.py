"""Versioned, reversible migration for the production operations config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


MIGRATION_SCHEMA_VERSION = 1
_REPO_SLUG = "Oplink76/hermes-agent"
_DELIVERY_TARGET = "slack:C0BFLTFC2LS"
_REQUIRED_EXTRAS = frozenset({"all", "dev", "slack"})


@dataclass(frozen=True)
class MigrationRecord:
    schema_version: int
    config_path: Path
    backup_path: Path
    manifest_path: Path
    original_sha256: str
    migrated_sha256: str
    original_mode: int

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for field in ("config_path", "backup_path", "manifest_path"):
            payload[field] = str(payload[field])
        return payload


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _mapping(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"operations config must contain a '{name}' mapping")
    return value


def _load(path: Path) -> tuple[bytes, dict[str, Any], os.stat_result]:
    resolved = Path(path).expanduser().resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("operations config must be a regular file")
    content = resolved.read_bytes()
    try:
        payload = yaml.safe_load(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("operations config is not valid UTF-8 YAML") from exc
    if not isinstance(payload, dict):
        raise ValueError("operations config must contain a YAML mapping")
    return content, payload, resolved.stat()


def _require_source_contract(payload: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    sync = _mapping(payload, "sync")
    runtime = _mapping(payload, "runtime")
    deploy = _mapping(payload, "deploy")
    install_value = runtime.get("install_root")
    if not isinstance(install_value, str) or not install_value:
        raise ValueError("runtime.install_root must be configured")
    install_root = Path(install_value).expanduser().resolve(strict=False)
    if sync.get("repo_slug") != _REPO_SLUG or deploy.get("repo_slug") != _REPO_SLUG:
        raise ValueError("sync and deploy repository identities must be canonical")
    if Path(str(sync.get("repo", ""))).expanduser().resolve(strict=False) != install_root:
        raise ValueError("sync.repo must equal runtime.install_root")
    if sync.get("origin") != "origin" or deploy.get("origin") != "origin":
        raise ValueError("sync and deploy origin identities must be canonical")
    if sync.get("upstream") != "upstream":
        raise ValueError("sync upstream identity must be canonical")
    preservation = deploy.get("preservation_command")
    if (
        not isinstance(preservation, list)
        or not preservation
        or not all(isinstance(value, str) and value for value in preservation)
        or preservation == ["/usr/bin/false"]
    ):
        raise ValueError("deploy.preservation_command must preserve Package 1")
    extras = deploy.get("uv_extras")
    if not isinstance(extras, list) or not _REQUIRED_EXTRAS.issubset(extras):
        raise ValueError("deploy.uv_extras must preserve all, dev, and slack")
    resolver = sync.get("conflict_resolver")
    if not isinstance(resolver, dict):
        raise ValueError("sync.conflict_resolver must be preserved")
    executable = resolver.get("codex_executable")
    if not isinstance(executable, str) or Path(executable).name != "codex":
        raise ValueError("sync conflict resolver path must identify Codex")
    return install_root, sync


def _set_exact(mapping: dict[str, Any], name: str, value: object) -> None:
    existing = mapping.get(name)
    if name in mapping and existing != value:
        raise ValueError(f"existing sync.{name} conflicts with migration value")
    mapping[name] = value


def _migrated_payload(
    payload: dict[str, Any],
    *,
    claude_executable: Path,
) -> dict[str, Any]:
    install_root, sync = _require_source_contract(payload)
    hermes_root = install_root.parent
    recovery_root = hermes_root / "recovery"
    claude = Path(claude_executable).expanduser().resolve(strict=False)
    if claude.name != "claude":
        raise ValueError("sync conflict reviewer path must identify Claude")
    values: dict[str, object] = {
        "receipt_root": str(recovery_root / "sync-receipts"),
        "status_file": str(recovery_root / "sync-status.json"),
        "notification_store": str(recovery_root / "sync-notifications.json"),
        "delivery_command": [
            str(install_root / ".venv" / "bin" / "hermes"),
            "send",
            "--to",
            _DELIVERY_TARGET,
            "--file",
            "-",
            "--quiet",
        ],
        "required_check": _mapping(payload, "deploy")["required_check"],
        "check_timeout_seconds": 2700,
        "poll_interval_seconds": 15,
        "resolver_backend": "codex",
        "reviewer_backend": "claude",
        "conflict_reviewer": {"claude_executable": str(claude)},
    }
    for name, value in values.items():
        _set_exact(sync, name, value)
    return payload


def _atomic_replace(path: Path, content: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _write_once(path: Path, content: bytes, *, mode: int) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"existing migration artifact conflicts: {path.name}")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _validate_candidate(path: Path) -> None:
    from .cli import (
        _validate_autonomous_runtime_scope,
        load_conflict_resolver,
        load_operations_config,
        load_sync_config,
        load_sync_policy_config,
    )

    sync = load_sync_config(path)
    policy = load_sync_policy_config(path)
    operations = load_operations_config(path)
    _validate_autonomous_runtime_scope(operations)
    resolver = load_conflict_resolver(path)
    if resolver is None:
        raise ValueError("migrated config lacks the conflict resolver")
    if sync.repo_slug != operations.repo_slug:
        raise ValueError("migrated repository identities differ")
    if policy.required_check != operations.deploy_config.required_check:
        raise ValueError("migrated required checks differ")


def migrate_operations_config(
    config_path: Path,
    *,
    backup_dir: Path,
    claude_executable: Path | None = None,
) -> MigrationRecord:
    """Back up, validate, and atomically add the reviewed sync schema."""
    config = Path(config_path).expanduser().resolve(strict=True)
    original, payload, metadata = _load(config)
    claude = claude_executable or (Path.home() / ".local" / "bin" / "claude")
    migrated_payload = _migrated_payload(payload, claude_executable=claude)
    migrated = yaml.safe_dump(migrated_payload, sort_keys=False).encode("utf-8")
    original_sha = _digest(original)
    migrated_sha = _digest(migrated)
    mode = stat.S_IMODE(metadata.st_mode)

    with tempfile.TemporaryDirectory(dir=config.parent) as directory:
        candidate = Path(directory) / config.name
        candidate.write_bytes(migrated)
        _validate_candidate(candidate)

    backups = Path(backup_dir).expanduser().resolve(strict=False)
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backups / f"{config.name}.before-sync-v1.{original_sha}.yaml"
    manifest = backups / f"{config.name}.sync-v1.{migrated_sha}.rollback.json"
    record = MigrationRecord(
        schema_version=MIGRATION_SCHEMA_VERSION,
        config_path=config,
        backup_path=backup,
        manifest_path=manifest,
        original_sha256=original_sha,
        migrated_sha256=migrated_sha,
        original_mode=mode,
    )
    _write_once(backup, original, mode=0o400)
    manifest_bytes = (
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_once(manifest, manifest_bytes, mode=0o400)
    _atomic_replace(config, migrated, mode=mode)
    return record


def _load_manifest(path: Path) -> MigrationRecord:
    try:
        payload = json.loads(Path(path).expanduser().resolve(strict=True).read_bytes())
        if set(payload) != {
            "schema_version",
            "config_path",
            "backup_path",
            "manifest_path",
            "original_sha256",
            "migrated_sha256",
            "original_mode",
        }:
            raise ValueError
        record = MigrationRecord(
            schema_version=int(payload["schema_version"]),
            config_path=Path(payload["config_path"]).resolve(strict=True),
            backup_path=Path(payload["backup_path"]).resolve(strict=True),
            manifest_path=Path(payload["manifest_path"]).resolve(strict=True),
            original_sha256=str(payload["original_sha256"]),
            migrated_sha256=str(payload["migrated_sha256"]),
            original_mode=int(payload["original_mode"]),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("rollback manifest is invalid") from exc
    if record.schema_version != MIGRATION_SCHEMA_VERSION:
        raise ValueError("rollback manifest schema is unsupported")
    if record.manifest_path != Path(path).expanduser().resolve(strict=True):
        raise ValueError("rollback manifest identity does not match")
    return record


def rollback_operations_config(manifest_path: Path) -> MigrationRecord:
    """Restore the exact backed-up bytes only from the expected migrated SHA."""
    record = _load_manifest(manifest_path)
    current = record.config_path.read_bytes()
    backup = record.backup_path.read_bytes()
    if _digest(current) != record.migrated_sha256:
        raise ValueError("current operations config does not match migrated checksum")
    if _digest(backup) != record.original_sha256:
        raise ValueError("operations config rollback backup checksum is invalid")
    _atomic_replace(record.config_path, backup, mode=record.original_mode)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--config", type=Path, required=True)
    migrate.add_argument("--backup-dir", type=Path, required=True)
    migrate.add_argument("--claude-executable", type=Path)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "migrate":
        record = migrate_operations_config(
            args.config,
            backup_dir=args.backup_dir,
            claude_executable=args.claude_executable,
        )
    else:
        record = rollback_operations_config(args.manifest)
    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
