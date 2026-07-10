from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from gateway.runtime_identity import (
    capture_runtime_identity,
    read_runtime_identity,
    remove_runtime_identity,
    runtime_identity_path,
    write_runtime_identity,
)
from gateway import status as gateway_status


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo_with_commit(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_identity_is_captured_once_and_written_atomically_with_private_mode(
    tmp_path: Path,
):
    repo, first_sha = _repo_with_commit(tmp_path)
    hermes_home = tmp_path / "profile-home"
    started_at = datetime(2026, 7, 10, 9, 30, tzinfo=timezone.utc)

    identity = capture_runtime_identity(
        source_root=repo,
        profile="tradingastrid",
        started_at=started_at,
    )
    (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")
    write_runtime_identity(identity, hermes_home=hermes_home)

    path = runtime_identity_path(hermes_home)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == identity.to_dict()
    assert payload["source_sha"] == first_sha
    assert payload["executable"] == str(Path(sys.executable).resolve())
    assert payload["python_version"] == sys.version.split()[0]
    assert payload["pid"] == os.getpid()
    assert payload["ppid"] == os.getppid()
    assert payload["profile"] == "tradingastrid"
    assert payload["started_at"] == "2026-07-10T09:30:00+00:00"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".gateway.json.*")) == []


def test_read_returns_none_for_invalid_or_missing_identity(tmp_path: Path):
    hermes_home = tmp_path / "profile-home"
    assert read_runtime_identity(hermes_home=hermes_home) is None

    path = runtime_identity_path(hermes_home)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    assert read_runtime_identity(hermes_home=hermes_home) is None


def test_shutdown_removes_only_identity_owned_by_same_pid(tmp_path: Path):
    repo, _ = _repo_with_commit(tmp_path)
    hermes_home = tmp_path / "profile-home"
    identity = capture_runtime_identity(source_root=repo, profile="default")
    path = runtime_identity_path(hermes_home)
    write_runtime_identity(identity, hermes_home=hermes_home)

    assert (
        remove_runtime_identity(
            hermes_home=hermes_home,
            pid=identity.pid + 1,
        )
        is False
    )
    assert path.exists()
    assert (
        remove_runtime_identity(
            hermes_home=hermes_home,
            pid=identity.pid,
        )
        is True
    )
    assert not path.exists()


def test_gateway_status_includes_the_separate_runtime_identity(
    tmp_path: Path,
    monkeypatch,
):
    repo, _ = _repo_with_commit(tmp_path)
    hermes_home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    identity = capture_runtime_identity(source_root=repo, profile="tradingastrid")
    write_runtime_identity(identity, hermes_home=hermes_home)
    gateway_status.write_runtime_status(gateway_state="running")

    status = gateway_status.read_runtime_status()

    assert status is not None
    assert status["runtime_identity"] == identity.to_dict()
