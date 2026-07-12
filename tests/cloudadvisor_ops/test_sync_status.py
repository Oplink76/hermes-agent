from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncResult,
    AutonomousSyncState,
)
from ops.cloudadvisor.hermes_ops.sync_status import (
    SyncStatusContext,
    SyncNotificationStore,
    SyncStatus,
    status_from_result,
)
from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.sync import SyncConfig


def _result(
    state: AutonomousSyncState,
    *,
    candidate_sha: str = "a" * 40,
    reason: str | None = None,
) -> AutonomousSyncResult:
    return AutonomousSyncResult(
        state=state,
        candidate_sha=candidate_sha,
        needs_ole=state is AutonomousSyncState.NEEDS_OLE,
        reason=reason,
    )


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str, filename: str) -> str:
    (repo / filename).write_text(message, encoding="utf-8")
    _git("add", filename, cwd=repo)
    _git(
        "-c",
        "user.name=Sync Test",
        "-c",
        "user.email=sync@example.invalid",
        "commit",
        "-m",
        message,
        cwd=repo,
    )
    return _git("rev-parse", "HEAD", cwd=repo)


def _status_context(tmp_path: Path) -> tuple[SyncStatusContext, str, str]:
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    _git("init", "-b", "main", cwd=seed)
    installed_sha = _commit(seed, "base", "base.txt")
    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
    _git("clone", "--bare", str(seed), str(origin), cwd=tmp_path)
    _git("clone", "--bare", str(seed), str(upstream), cwd=tmp_path)

    repo = tmp_path / "repo"
    _git("clone", str(origin), str(repo), cwd=tmp_path)
    _git("remote", "add", "upstream", str(upstream), cwd=repo)

    upstream_work = tmp_path / "upstream-work"
    _git("clone", str(upstream), str(upstream_work), cwd=tmp_path)
    _commit(upstream_work, "upstream one", "upstream-1.txt")
    _commit(upstream_work, "upstream two", "upstream-2.txt")
    _git("push", "origin", "main", cwd=upstream_work)

    fork_work = tmp_path / "fork-work"
    _git("clone", str(origin), str(fork_work), cwd=tmp_path)
    fork_sha = _commit(fork_work, "fork change", "fork.txt")
    _git("push", "origin", "main", cwd=fork_work)
    _git("fetch", "origin", cwd=repo)
    _git("fetch", "upstream", cwd=repo)

    config = SyncConfig(
        repo=repo,
        worktree=tmp_path / "candidate",
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "sync.lock",
    )
    return (
        SyncStatusContext(
            sync=config,
            install_root=repo,
            required_check="All required checks pass",
        ),
        installed_sha,
        fork_sha,
    )


def test_pending_result_collects_real_git_status(tmp_path: Path) -> None:
    context, installed_sha, fork_sha = _status_context(tmp_path)
    result = AutonomousSyncResult(
        state=AutonomousSyncState.PENDING_REFRESH,
        candidate_sha="c" * 40,
        pr_number=7,
        reason="exact check pending",
    )

    status = status_from_result(
        result,
        context=context,
        runner=SubprocessCommandRunner(),
    )

    assert status.sync_state == "PENDING_REFRESH"
    assert status.converged is False
    assert status.upstream_behind == 2
    assert status.fork_behind == 1
    assert status.sync_pr_number == 7
    assert status.required_check == "All required checks pass"
    assert status.fork_main_sha == fork_sha
    assert status.installed_sha == installed_sha
    assert status.checked_at


def test_status_collection_survives_unavailable_git_evidence(tmp_path: Path) -> None:
    context, _, _ = _status_context(tmp_path)

    class UnavailableRunner:
        def run(self, argv, cwd, timeout=300):
            raise OSError("git unavailable")

    status = status_from_result(
        _result(AutonomousSyncState.LOCKED),
        context=context,
        runner=UnavailableRunner(),
    )

    assert status.sync_state == "LOCKED"
    assert status.upstream_behind is None
    assert status.fork_behind is None
    assert status.fork_main_sha is None
    assert status.installed_sha is None


def test_same_needs_ole_fingerprint_notified_once(tmp_path: Path) -> None:
    store = SyncNotificationStore(tmp_path / "notifications.json")
    packet = _result(AutonomousSyncState.NEEDS_OLE, reason="major conflict")

    assert store.should_emit(packet) is True
    store.record_emitted(packet)
    assert store.should_emit(packet) is False


def test_notification_fingerprint_changes_with_escalation_evidence(
    tmp_path: Path,
) -> None:
    store = SyncNotificationStore(tmp_path / "notifications.json")
    first = _result(AutonomousSyncState.NEEDS_OLE, reason="major conflict")
    second = _result(
        AutonomousSyncState.NEEDS_OLE,
        candidate_sha="b" * 40,
        reason="major conflict",
    )

    store.record_emitted(first)

    assert store.should_emit(second) is True


def test_non_escalation_result_never_notifies(tmp_path: Path) -> None:
    store = SyncNotificationStore(tmp_path / "notifications.json")

    assert store.should_emit(_result(AutonomousSyncState.DEPLOYED)) is False


def test_status_write_is_canonical_atomic_and_excludes_reason(tmp_path: Path) -> None:
    status_path = tmp_path / "state" / "sync-status.json"
    result = _result(
        AutonomousSyncState.NEEDS_OLE,
        reason="raw command output must not be persisted",
    )
    context, _, _ = _status_context(tmp_path / "git")
    status = status_from_result(
        result,
        context=context,
        runner=SubprocessCommandRunner(),
    )

    status.write(status_path)

    raw = status_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert raw == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    assert "reason" not in payload
    assert SyncStatus.load(status_path) == status
