"""Verified pre-deploy Git and SQLite snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .command import CommandRunner


@dataclass(frozen=True)
class DatabaseSnapshot:
    original_path: Path
    backup_path: Path
    sha256: str


@dataclass(frozen=True)
class SnapshotRecord:
    id: str
    directory: Path
    install_root: Path
    source_sha: str
    git_bundle: Path
    git_bundle_sha256: str
    databases: tuple[DatabaseSnapshot, ...]
    manifest_path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "directory": str(self.directory),
            "install_root": str(self.install_root),
            "source_sha": self.source_sha,
            "git_bundle": str(self.git_bundle),
            "git_bundle_sha256": self.git_bundle_sha256,
            "databases": [
                {
                    **asdict(database),
                    "original_path": str(database.original_path),
                    "backup_path": str(database.backup_path),
                }
                for database in self.databases
            ],
            "manifest_path": str(self.manifest_path),
        }


class SnapshotCoordinator:
    """Concrete deploy snapshot provider with an external preservation gate."""

    def __init__(
        self,
        *,
        install_root: Path,
        hermes_homes: Iterable[Path],
        snapshot_root: Path,
        preservation_command: tuple[str, ...],
        runner: CommandRunner,
    ):
        self.install_root = Path(install_root).expanduser().resolve(strict=False)
        self.hermes_homes = tuple(Path(home) for home in hermes_homes)
        self.snapshot_root = Path(snapshot_root)
        self.preservation_command = preservation_command
        self.runner = runner

    def verify_preservation(self) -> bool:
        if not self.preservation_command:
            return False
        completed = self.runner.run(
            list(self.preservation_command),
            cwd=self.install_root,
            timeout=600,
        )
        return completed.returncode == 0

    def create(self, previous_sha: str) -> SnapshotRecord:
        record = create_snapshot(
            install_root=self.install_root,
            hermes_homes=self.hermes_homes,
            snapshot_root=self.snapshot_root,
            runner=self.runner,
        )
        if record.source_sha != previous_sha:
            raise RuntimeError(
                "predeploy snapshot source SHA does not match the install checkout"
            )
        return record

    def verify(self, snapshot: object) -> bool:
        return isinstance(snapshot, SnapshotRecord) and verify_snapshot(
            snapshot,
            runner=self.runner,
        )

    def restore(self, snapshot: object) -> None:
        if not isinstance(snapshot, SnapshotRecord):
            raise TypeError("snapshot is not a SnapshotRecord")
        restore_snapshot(snapshot)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_required(
    runner: CommandRunner,
    argv: list[str],
    *,
    cwd: Path,
) -> str:
    completed = runner.run(argv, cwd=cwd, timeout=300)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"command failed ({' '.join(argv)}): {detail}")
    return (completed.stdout or "").strip()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _sqlite_files(hermes_homes: Iterable[Path]) -> tuple[Path, ...]:
    suffixes = {".db", ".sqlite", ".sqlite3"}
    excluded_directories = {
        ".git",
        ".venv",
        "backups",
        "hermes-agent",
        "node_modules",
        "recovery",
        "venv",
        "worktrees",
    }
    files: set[Path] = set()
    for raw_home in hermes_homes:
        home = Path(raw_home).expanduser().resolve(strict=False)
        if not home.is_dir():
            continue
        for path in home.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            relative = path.relative_to(home)
            if any(part in excluded_directories for part in relative.parts[:-1]):
                continue
            files.add(path.resolve(strict=False))
    return tuple(sorted(files))


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)
            result = destination_connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"SQLite verification failed for {source}")


def create_snapshot(
    *,
    install_root: Path,
    hermes_homes: Iterable[Path],
    snapshot_root: Path,
    runner: CommandRunner,
) -> SnapshotRecord:
    root = Path(install_root).expanduser().resolve(strict=True)
    created_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = f"{created_at}-{uuid.uuid4().hex[:12]}"
    directory = Path(snapshot_root).expanduser().resolve(strict=False) / snapshot_id
    directory.mkdir(parents=True, exist_ok=False)

    source_sha = _run_required(
        runner,
        ["git", "rev-parse", "HEAD"],
        cwd=root,
    )
    bundle = directory / "source.bundle"
    _run_required(
        runner,
        ["git", "bundle", "create", str(bundle), "--all"],
        cwd=root,
    )

    database_records = []
    database_dir = directory / "sqlite"
    for index, source in enumerate(_sqlite_files(hermes_homes)):
        path_hash = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        backup = database_dir / f"{index:04d}-{path_hash}.sqlite3"
        _backup_database(source, backup)
        database_records.append(
            DatabaseSnapshot(
                original_path=source,
                backup_path=backup,
                sha256=_sha256(backup),
            )
        )

    manifest = directory / "manifest.json"
    record = SnapshotRecord(
        id=snapshot_id,
        directory=directory,
        install_root=root,
        source_sha=source_sha,
        git_bundle=bundle,
        git_bundle_sha256=_sha256(bundle),
        databases=tuple(database_records),
        manifest_path=manifest,
    )
    _write_json_atomic(manifest, record.to_dict())
    return record


def _sqlite_ok(path: Path) -> bool:
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except (OSError, sqlite3.Error):
        return False
    return bool(result and result[0] == "ok")


def verify_snapshot(record: SnapshotRecord, *, runner: CommandRunner) -> bool:
    try:
        if not record.manifest_path.is_file():
            return False
        manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
        if manifest != record.to_dict():
            return False
        if _sha256(record.git_bundle) != record.git_bundle_sha256:
            return False
        completed = runner.run(
            ["git", "bundle", "verify", str(record.git_bundle)],
            cwd=record.install_root,
            timeout=300,
        )
        if completed.returncode != 0:
            return False
        for database in record.databases:
            if _sha256(database.backup_path) != database.sha256:
                return False
            if not _sqlite_ok(database.backup_path):
                return False
    except (OSError, json.JSONDecodeError):
        return False
    return True


def restore_snapshot(record: SnapshotRecord) -> None:
    for database in record.databases:
        if _sha256(database.backup_path) != database.sha256:
            raise RuntimeError(f"snapshot checksum failed for {database.backup_path}")
        if not _sqlite_ok(database.backup_path):
            raise RuntimeError(f"snapshot database is invalid: {database.backup_path}")
        target = database.original_path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copy2(database.backup_path, temporary)
            os.replace(temporary, target)
            target.with_name(f"{target.name}-wal").unlink(missing_ok=True)
            target.with_name(f"{target.name}-shm").unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
