from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncResult,
    AutonomousSyncState,
)
from ops.cloudadvisor.hermes_ops.sync_status import (
    SyncStatusContext,
    SyncDecisionOutbox,
    SyncStatus,
    escalation_fingerprint,
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


def test_escalation_fingerprint_binds_live_repository_identities() -> None:
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        candidate_sha="c" * 40,
        fork_main_sha="f" * 40,
        installed_sha="1" * 40,
        needs_ole=True,
        reason_code="CONFLICT_REVIEW_INVALID",
        failed_gate="conflict_review",
    )
    fingerprint = escalation_fingerprint(result)

    assert fingerprint == escalation_fingerprint(replace(result))
    assert fingerprint != escalation_fingerprint(
        replace(result, fork_main_sha="a" * 40)
    )
    assert fingerprint != escalation_fingerprint(
        replace(result, installed_sha="b" * 40)
    )


def test_pending_decision_retries_until_exact_delivery_ack(tmp_path: Path) -> None:
    store = SyncDecisionOutbox(tmp_path / "notifications.json")
    packet_path = (tmp_path / "packet.json").resolve()

    assert store.stage(
        fingerprint="1" * 64,
        packet_path=packet_path,
        packet_sha256="2" * 64,
    ) is True
    pending = store.load()
    assert pending is not None
    assert pending.status == "pending"
    assert store.stage(
        fingerprint="1" * 64,
        packet_path=packet_path,
        packet_sha256="2" * 64,
    ) is True

    store.acknowledge(
        fingerprint="1" * 64,
        packet_sha256="2" * 64,
        idempotency_key=pending.idempotency_key,
    )

    assert store.load().status == "acknowledged"
    assert store.stage(
        fingerprint="1" * 64,
        packet_path=packet_path,
        packet_sha256="2" * 64,
    ) is False


def test_changed_decision_reopens_acknowledged_outbox(
    tmp_path: Path,
) -> None:
    store = SyncDecisionOutbox(tmp_path / "notifications.json")
    packet_path = (tmp_path / "packet.json").resolve()
    store.stage(
        fingerprint="1" * 64,
        packet_path=packet_path,
        packet_sha256="2" * 64,
    )
    pending = store.load()
    store.acknowledge(
        fingerprint="1" * 64,
        packet_sha256="2" * 64,
        idempotency_key=pending.idempotency_key,
    )

    assert store.stage(
        fingerprint="3" * 64,
        packet_path=(tmp_path / "changed.json").resolve(),
        packet_sha256="4" * 64,
    ) is True
    assert store.load().status == "pending"
    assert store.load().escalation_fingerprint == "3" * 64


def test_pending_decision_cannot_be_overwritten_or_cleared(tmp_path: Path) -> None:
    store = SyncDecisionOutbox(tmp_path / "notifications.json")
    store.stage(
        fingerprint="1" * 64,
        packet_path=(tmp_path / "packet.json").resolve(),
        packet_sha256="2" * 64,
    )

    with pytest.raises(ValueError, match="still pending"):
        store.stage(
            fingerprint="3" * 64,
            packet_path=(tmp_path / "changed.json").resolve(),
            packet_sha256="4" * 64,
        )
    store.clear_resolved()

    assert store.load().status == "pending"
    assert store.load().escalation_fingerprint == "1" * 64


def test_resolved_cycle_clears_ack_and_same_future_decision_can_reopen(
    tmp_path: Path,
) -> None:
    store = SyncDecisionOutbox(tmp_path / "notifications.json")
    packet_path = (tmp_path / "packet.json").resolve()
    store.stage(
        fingerprint="1" * 64,
        packet_path=packet_path,
        packet_sha256="2" * 64,
    )
    pending = store.load()
    store.acknowledge(
        fingerprint="1" * 64,
        packet_sha256="2" * 64,
        idempotency_key=pending.idempotency_key,
    )

    store.clear_resolved()

    assert store.load() is None
    assert store.stage(
        fingerprint="1" * 64,
        packet_path=packet_path,
        packet_sha256="2" * 64,
    ) is True


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
