from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.snapshot import (
    SnapshotCoordinator,
    create_snapshot,
    restore_snapshot,
    verify_snapshot,
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


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    (repo / "tracked.txt").write_text("foundation\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "foundation")
    return repo


def _database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS values_table (value TEXT)")
        connection.execute("DELETE FROM values_table")
        connection.execute("INSERT INTO values_table VALUES (?)", (value,))


def _read_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM values_table").fetchone()[0]


def test_snapshot_creates_verified_git_bundle_and_restorable_sqlite_backups(
    tmp_path: Path,
):
    repo = _source_repo(tmp_path)
    hermes_home = tmp_path / "profile"
    database = hermes_home / "state.db"
    _database(database, "before")
    _database(hermes_home / "recovery" / "old-snapshot.db", "archived")

    snapshot = create_snapshot(
        install_root=repo,
        hermes_homes=[hermes_home],
        snapshot_root=tmp_path / "snapshots",
        runner=SubprocessCommandRunner(),
    )

    assert snapshot.source_sha == _git(repo, "rev-parse", "HEAD")
    assert snapshot.git_bundle.exists()
    assert len(snapshot.databases) == 1
    assert snapshot.manifest_path.exists()
    assert verify_snapshot(snapshot, runner=SubprocessCommandRunner()) is True

    _database(database, "after")
    restore_snapshot(snapshot)

    assert _read_value(database) == "before"


def test_snapshot_verification_fails_after_backup_corruption(tmp_path: Path):
    repo = _source_repo(tmp_path)
    hermes_home = tmp_path / "profile"
    _database(hermes_home / "state.db", "before")
    snapshot = create_snapshot(
        install_root=repo,
        hermes_homes=[hermes_home],
        snapshot_root=tmp_path / "snapshots",
        runner=SubprocessCommandRunner(),
    )
    snapshot.databases[0].backup_path.write_bytes(b"corrupt")

    assert verify_snapshot(snapshot, runner=SubprocessCommandRunner()) is False


def test_snapshot_coordinator_gates_on_preservation_and_source_sha(tmp_path: Path):
    repo = _source_repo(tmp_path)
    hermes_home = tmp_path / "profile"
    _database(hermes_home / "state.db", "before")
    coordinator = SnapshotCoordinator(
        install_root=repo,
        hermes_homes=[hermes_home],
        snapshot_root=tmp_path / "snapshots",
        preservation_command=(sys.executable, "-c", "raise SystemExit(0)"),
        runner=SubprocessCommandRunner(),
    )
    previous_sha = _git(repo, "rev-parse", "HEAD")

    assert coordinator.verify_preservation() is True
    snapshot = coordinator.create(previous_sha)
    assert coordinator.verify(snapshot) is True


def test_snapshot_verification_fails_when_manifest_is_not_the_record(tmp_path: Path):
    repo = _source_repo(tmp_path)
    hermes_home = tmp_path / "profile"
    _database(hermes_home / "state.db", "before")
    snapshot = create_snapshot(
        install_root=repo,
        hermes_homes=[hermes_home],
        snapshot_root=tmp_path / "snapshots",
        runner=SubprocessCommandRunner(),
    )
    snapshot.manifest_path.write_text("{}\n", encoding="utf-8")

    assert verify_snapshot(snapshot, runner=SubprocessCommandRunner()) is False
