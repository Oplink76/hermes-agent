"""Durable worker protocol tests for the external Agent Memory vault."""

from datetime import datetime, timedelta
import json
import os
import sqlite3
import stat
import threading

import pytest

from hermes_cli import agent_memory_protocol as protocol
from hermes_cli.agent_memory_protocol import (
    MemoryReceipt,
    WorkerRecallRequest,
    WorkerWriteRequest,
    acknowledge_attention,
    configured_outbox_path,
    configured_outbox_status,
    functional_identity_for_task,
    recall_for_worker,
    receipt_is_present,
    reconcile_configured_outbox,
    write_worker_gist,
)
from hermes_cli.agent_memory_vault import (
    ExecutorIdentity,
    SessionGist,
    append_gist,
    recall,
)


def _configured_paths(tmp_path, monkeypatch, *, create_vault):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    vault = tmp_path / "Agent Memory"
    outbox = tmp_path / "outbox"
    if create_vault:
        vault.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_VAULT", str(vault))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_OUTBOX", str(outbox))
    return vault, outbox


def _write_request(execution_id, gist_id):
    executor = ExecutorIdentity(
        agent_id="codex", model="test-model", surface="codex-cli",
        hermes_role="developer", execution_id=execution_id,
        responsibility="writer",
    )
    return WorkerWriteRequest(
        operation_id=f"write-{execution_id}", task_id="t-memory",
        run_id=7, delegation_id="delegation-7", gist_id=gist_id,
        occurred_at=datetime(2026, 7, 19, 12, 0),
        function_id="function-memory", title="Agent Memory",
        context="board=default; task=t-memory; run=7",
        summary="Implemented bounded memory work.", reused="none",
        result="Worker memory recorded.", maturity="code_complete",
        evidence="tests: focused suite passed", behavior="none",
        decisions="none", open_loops="none", executor=executor,
    )


def _recall_request(execution_id="exec-123"):
    return WorkerRecallRequest(
        operation_id=f"recall-{execution_id}", task_id="t-memory", run_id=7,
        delegation_id="delegation-7", function_id="function-memory",
        title="Agent Memory", query="bounded memory work",
        executor=ExecutorIdentity(
            agent_id="codex", model="test-model", surface="codex-cli",
            hermes_role="developer", execution_id=execution_id,
            responsibility="writer",
        ),
    )


def test_request_and_receipt_mappings_are_exact_and_round_trip():
    write = _write_request("exec-123", "gist-123")
    recall_request = _recall_request()
    assert WorkerWriteRequest.from_mapping(write.to_mapping()) == write
    assert WorkerRecallRequest.from_mapping(recall_request.to_mapping()) == recall_request
    assert len(write.to_mapping()) == 18
    assert len(recall_request.to_mapping()) == 8
    with pytest.raises(ValueError, match="exactly"):
        WorkerRecallRequest.from_mapping({**recall_request.to_mapping(), "extra": True})
    with pytest.raises(ValueError, match="exactly"):
        WorkerWriteRequest.from_mapping({**write.to_mapping(), "extra": True})

    receipt = MemoryReceipt.for_gist(
        operation_id="write-exec-123", operation="write", status="queued",
        continue_work=True, task_id="t-memory", run_id=7,
        delegation_id="delegation-7", gist_id="gist-123", executor=write.executor,
    )
    assert MemoryReceipt.from_mapping(receipt.to_mapping()) == receipt
    assert list(receipt.to_mapping()) == sorted(receipt.to_mapping())


def test_unavailable_vault_queues_validated_gist_and_continues(tmp_path, monkeypatch):
    vault = tmp_path / "missing-onedrive" / "Agent Memory"
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("HERMES_AGENT_MEMORY_VAULT", str(vault))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_OUTBOX", str(outbox))
    receipt = write_worker_gist(_write_request("exec-123", "gist-123"))
    assert receipt.status == "queued"
    assert receipt.continue_work is True
    assert not vault.exists()
    assert [p.name for p in outbox.glob("*.json")] == ["gist-gist-123.json"]
    assert oct(outbox.stat().st_mode & 0o777) == "0o700"
    assert oct(next(outbox.glob("*.json")).stat().st_mode & 0o777) == "0o600"
    assert oct((outbox / ".agent-memory.lock").stat().st_mode & 0o777) == "0o600"
    assert receipt_is_present(receipt) is True


@pytest.mark.parametrize("failed_step", ("append", "lint"))
def test_existing_vault_write_failure_falls_back_to_durable_queue(
    tmp_path, monkeypatch, failed_step
):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=True)

    def fail(*_args, **_kwargs):
        raise OSError(f"simulated {failed_step} failure")

    target = "lint_vault" if failed_step == "lint" else "append_gist"
    monkeypatch.setattr(protocol, target, fail)

    receipt = write_worker_gist(_write_request("exec-123", "gist-123"))

    assert receipt.status == "queued"
    assert receipt.continue_work is True
    assert [path.name for path in outbox.glob("gist-*.json")] == [
        "gist-gist-123.json"
    ]
    assert MemoryReceipt.from_mapping(receipt.to_mapping()) == receipt
    assert receipt_is_present(receipt) is True


def test_vault_config_resolution_failure_falls_back_to_durable_queue(
    tmp_path, monkeypatch
):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)

    def fail_config():
        raise OSError("simulated vault config failure")

    monkeypatch.setattr(protocol, "configured_vault_path", fail_config)

    receipt = write_worker_gist(_write_request("exec-123", "gist-123"))

    assert receipt.status == "queued"
    assert (outbox / "gist-gist-123.json").is_file()
    assert receipt_is_present(receipt) is True


def test_atomic_queue_syncs_containing_directory_before_receipt_return(
    tmp_path, monkeypatch
):
    _, _ = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    events = []
    original_fsync = os.fsync

    def record_fsync(descriptor):
        kind = (
            "directory-sync"
            if stat.S_ISDIR(os.fstat(descriptor).st_mode)
            else "file-sync"
        )
        events.append(kind)
        return original_fsync(descriptor)

    monkeypatch.setattr(protocol.os, "fsync", record_fsync)

    receipt = write_worker_gist(_write_request("exec-123", "gist-123"))
    events.append("receipt-returned")

    assert receipt.status == "queued"
    assert "file-sync" in events
    assert events.index("directory-sync") < events.index("receipt-returned")


def test_reconcile_after_restart_moves_and_verifies(tmp_path, monkeypatch):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    queued = write_worker_gist(_write_request("exec-123", "gist-123"))
    assert queued.status == "queued"
    vault.mkdir(parents=True)
    report = reconcile_configured_outbox(now=datetime(2026, 7, 19, 10, 0))
    assert (report.moved, report.pending) == (1, 0)
    assert not list(outbox.glob("*.json"))
    assert recall(vault, "gist-123")[0].gist_id == "gist-123"
    assert receipt_is_present(queued) is True


def test_reconcile_reclassifies_conflicting_stored_operation_as_unsafe(
    tmp_path, monkeypatch
):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    queued_request = _write_request("exec-123", "gist-queued")
    stored_request = _write_request("exec-123", "gist-stored")
    assert write_worker_gist(queued_request).status == "queued"
    queued_path = outbox / "gist-gist-queued.json"
    queued_content = queued_path.read_bytes()
    vault.mkdir(parents=True)
    assert append_gist(vault, stored_request.to_session_gist()) is True
    fsync_modes = []
    original_fsync = os.fsync

    def record_fsync(descriptor):
        fsync_modes.append(os.fstat(descriptor).st_mode)
        return original_fsync(descriptor)

    monkeypatch.setattr(protocol.os, "fsync", record_fsync)

    report = reconcile_configured_outbox(now=datetime(2026, 7, 19, 12, 0))
    unsafe_path = outbox / "unsafe-gist-gist-queued.json"
    status = configured_outbox_status(now=datetime(2026, 7, 19, 12, 0))

    assert report.to_mapping() == {
        "closed_incidents": 0,
        "corrupt": 1,
        "moved": 0,
        "pending": 1,
        "vault_available": True,
    }
    assert not queued_path.exists()
    assert unsafe_path.read_bytes() == queued_content
    assert any(stat.S_ISDIR(mode) for mode in fsync_modes)
    assert status.attention_required is True
    assert status.reason == "corrupt_or_unsafe"
    assert status.notify_ole is True
    acknowledge_attention(status.fingerprint)
    acknowledged = configured_outbox_status(now=datetime(2026, 7, 19, 12, 0))
    assert acknowledged.attention_required is True
    assert acknowledged.notify_ole is False


@pytest.mark.parametrize("failed_step", ("append", "lint"))
def test_reconcile_keeps_transient_vault_failure_pending_without_alert(
    tmp_path, monkeypatch, failed_step
):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    queued_at = datetime(2026, 7, 19, 11, 0)
    monkeypatch.setattr(protocol, "_utc_now", lambda: queued_at)
    assert write_worker_gist(
        _write_request("exec-123", "gist-queued")
    ).status == "queued"
    queued_path = outbox / "gist-gist-queued.json"
    queued_content = queued_path.read_bytes()
    vault.mkdir(parents=True)

    def fail_transiently(*_args, **_kwargs):
        raise OSError("simulated transient vault failure")

    target = "lint_vault" if failed_step == "lint" else "append_gist"
    monkeypatch.setattr(protocol, target, fail_transiently)

    now = queued_at + timedelta(minutes=30)
    report = reconcile_configured_outbox(now=now)
    status = configured_outbox_status(now=now)

    assert report.to_mapping() == {
        "closed_incidents": 0,
        "corrupt": 0,
        "moved": 0,
        "pending": 1,
        "vault_available": True,
    }
    assert queued_path.read_bytes() == queued_content
    assert status.attention_required is False
    assert status.notify_ole is False
    assert status.reason == "none"


def test_reconciled_receipt_rejects_a_different_executor(tmp_path, monkeypatch):
    vault, _outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    queued = write_worker_gist(_write_request("exec-123", "gist-123"))
    vault.mkdir(parents=True)
    assert reconcile_configured_outbox().moved == 1
    forged = MemoryReceipt.for_gist(
        operation_id=queued.operation_id,
        operation="write",
        status="queued",
        continue_work=True,
        task_id=queued.task_id,
        run_id=queued.run_id,
        delegation_id=queued.delegation_id,
        gist_id=queued.gist_id,
        executor=_write_request("exec-forged", "gist-123").executor,
    )

    assert receipt_is_present(forged) is False


def test_concurrent_writers_leave_two_valid_atomic_envelopes(tmp_path, monkeypatch):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    receipts = []
    errors = []

    def write(request):
        try:
            receipts.append(write_worker_gist(request))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(_write_request(f"exec-{index}", f"gist-{index}"),))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert [thread.is_alive() for thread in threads] == [False, False]
    assert sorted(receipt.status for receipt in receipts) == ["queued", "queued"]
    paths = sorted(outbox.glob("gist-*.json"))
    assert [path.name for path in paths] == ["gist-gist-0.json", "gist-gist-1.json"]
    assert all(json.loads(path.read_text())["kind"] == "gist" for path in paths)


def test_duplicate_gist_operation_reuses_one_envelope_and_receipt(tmp_path, monkeypatch):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    request = _write_request("exec-123", "gist-123")

    first = write_worker_gist(request)
    before = next(outbox.glob("gist-*.json")).read_bytes()
    second = write_worker_gist(request)

    assert first.to_mapping() == second.to_mapping()
    assert len(list(outbox.glob("gist-*.json"))) == 1
    assert next(outbox.glob("gist-*.json")).read_bytes() == before


def test_queued_operation_retry_with_new_gist_id_reuses_original_receipt(
    tmp_path, monkeypatch
):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    first_request = _write_request("exec-123", "gist-original")
    retry_request = _write_request("exec-123", "gist-retry")
    retry_request.operation_id = first_request.operation_id

    first = write_worker_gist(first_request)
    retry = write_worker_gist(retry_request)

    assert retry.to_mapping() == first.to_mapping()
    assert retry.gist_id == "gist-original"
    assert [path.name for path in outbox.glob("gist-*.json")] == [
        "gist-gist-original.json"
    ]


def test_stored_operation_retry_with_new_gist_id_reuses_original_gist(
    tmp_path, monkeypatch
):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=True)
    first_request = _write_request("exec-123", "gist-original")
    retry_request = _write_request("exec-123", "gist-retry")
    retry_request.operation_id = first_request.operation_id

    first = write_worker_gist(first_request)
    retry = write_worker_gist(retry_request)

    assert first.status == "stored"
    assert retry.status == "already_stored"
    assert retry.gist_id == first.gist_id == "gist-original"
    assert recall(vault, first_request.operation_id)[0].gist_id == "gist-original"
    assert len(list((vault / "memory").glob("*.md"))) == 1
    assert not outbox.exists()


def test_queued_envelope_contains_redacted_values_only(tmp_path, monkeypatch):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    request = _write_request("exec-123", "gist-123")
    request.summary = f"Completed with token {secret}"

    write_worker_gist(request)

    raw = next(outbox.glob("gist-*.json")).read_bytes()
    assert secret.encode() not in raw
    assert b"ghp_" in raw


def test_corrupt_envelope_is_retained_and_requires_immediate_attention(tmp_path, monkeypatch):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=True)
    outbox.mkdir(mode=0o700)
    corrupt = outbox / "gist-corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    report = reconcile_configured_outbox(now=datetime(2026, 7, 19, 12, 0))
    status = configured_outbox_status(now=datetime(2026, 7, 19, 12, 0))

    assert report.corrupt == 1
    assert corrupt.exists()
    assert status.attention_required is True
    assert status.notify_ole is True
    assert status.reason == "corrupt_or_unsafe"
    acknowledge_attention(status.fingerprint)
    assert configured_outbox_status(now=datetime(2026, 7, 19, 12, 0)).notify_ole is False


def test_symlinked_envelope_is_retained_as_unsafe_data(tmp_path, monkeypatch):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    write_worker_gist(_write_request("exec-123", "gist-123"))
    queued = next(outbox.glob("gist-*.json"))
    outside = tmp_path / queued.name
    queued.rename(outside)
    queued.symlink_to(outside)
    vault.mkdir()

    report = reconcile_configured_outbox()

    assert report.corrupt == 1
    assert queued.is_symlink()
    assert recall(vault, "gist-123") == []


def test_missing_external_root_is_never_created(tmp_path, monkeypatch):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)

    write_worker_gist(_write_request("exec-123", "gist-123"))
    report = reconcile_configured_outbox(now=datetime(2026, 7, 19, 12, 0))

    assert not vault.exists()
    assert outbox.is_dir()
    assert report.vault_available is False
    assert report.pending == 1


def test_outbox_lock_timeout_never_returns_a_false_queued_receipt(tmp_path, monkeypatch):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    ready = threading.Event()
    release = threading.Event()

    def hold_lock():
        with protocol._outbox_lock(outbox):
            ready.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert ready.wait(timeout=2)
    monkeypatch.setattr(protocol, "_OUTBOX_LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(TimeoutError, match="Agent Memory outbox lock"):
            write_worker_gist(_write_request("exec-123", "gist-123"))
    finally:
        release.set()
        holder.join(timeout=2)

    assert not list(outbox.glob("gist-*.json"))


def test_vault_outage_requires_attention_only_after_24_hours(tmp_path, monkeypatch):
    _, _ = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    queued_at = datetime(2026, 7, 18, 10, 0)
    monkeypatch.setattr(protocol, "_utc_now", lambda: queued_at)
    write_worker_gist(_write_request("exec-123", "gist-123"))

    before = configured_outbox_status(now=queued_at + timedelta(hours=23, minutes=59))
    threshold = configured_outbox_status(now=queued_at + timedelta(hours=24))

    assert before.attention_required is False
    assert before.notify_ole is False
    assert threshold.attention_required is True
    assert threshold.notify_ole is True
    assert threshold.reason == "pending_for_24_hours"


def test_old_pending_write_is_quiet_while_vault_is_available(tmp_path, monkeypatch):
    vault, _outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    queued_at = datetime(2026, 7, 18, 10, 0)
    monkeypatch.setattr(protocol, "_utc_now", lambda: queued_at)
    assert write_worker_gist(_write_request("exec-123", "gist-123")).status == "queued"
    vault.mkdir(parents=True)

    status = configured_outbox_status(now=queued_at + timedelta(hours=48))

    assert status.vault_available is True
    assert status.pending == 1
    assert status.attention_required is False
    assert status.notify_ole is False
    assert status.reason == "none"


@pytest.mark.parametrize(
    ("age", "attention_required", "notify_ole"),
    (
        (timedelta(hours=23, minutes=59), False, False),
        (timedelta(hours=24), True, True),
    ),
)
def test_vault_config_failure_preserves_pending_status_and_attention(
    tmp_path, monkeypatch, age, attention_required, notify_ole
):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    queued_at = datetime(2026, 7, 18, 10, 0)
    monkeypatch.setattr(protocol, "_utc_now", lambda: queued_at)
    assert write_worker_gist(_write_request("exec-123", "gist-123")).status == "queued"
    envelope = outbox / "gist-gist-123.json"
    before = envelope.read_bytes()

    def fail_config():
        raise OSError("persistent vault config failure")

    monkeypatch.setattr(protocol, "configured_vault_path", fail_config)
    now = queued_at + age

    report = reconcile_configured_outbox(now=now)
    status = configured_outbox_status(now=now)

    assert report.to_mapping() == {
        "closed_incidents": 0,
        "corrupt": 0,
        "moved": 0,
        "pending": 1,
        "vault_available": False,
    }
    assert envelope.read_bytes() == before
    assert not vault.exists()
    assert status.enabled is True
    assert status.vault_available is False
    assert status.pending == 1
    assert status.attention_required is attention_required
    assert status.notify_ole is notify_ole
    assert status.reason == (
        "pending_for_24_hours" if attention_required else "none"
    )
    assert len(status.fingerprint) == 64


def test_recall_receipts_cover_matches_empty_outages_and_incident_recovery(tmp_path, monkeypatch):
    vault, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    request = _recall_request()
    matches, unavailable = recall_for_worker(request)
    assert matches == []
    assert unavailable.status == "unavailable"
    assert unavailable.continue_work is True
    assert len(list(outbox.glob("recall-*.json"))) == 1

    vault.mkdir()
    empty_matches, empty = recall_for_worker(request)
    assert empty_matches == []
    assert empty.status == "empty"
    report = reconcile_configured_outbox()
    assert report.closed_incidents == 1
    assert not list(outbox.glob("recall-*.json"))

    assert write_worker_gist(_write_request("exec-123", "gist-123")).status == "stored"
    matched_results, matched = recall_for_worker(request)
    assert [item.gist_id for item in matched_results] == ["gist-123"]
    assert matched.status == "matched"


def test_recall_config_resolution_failure_is_fail_open(tmp_path, monkeypatch):
    _configured_paths(tmp_path, monkeypatch, create_vault=False)

    def fail_config():
        raise OSError("simulated vault config failure")

    monkeypatch.setattr(protocol, "configured_vault_path", fail_config)

    matches, receipt = recall_for_worker(_recall_request())

    assert matches == []
    assert receipt.status == "unavailable"
    assert receipt.continue_work is True


def test_recall_outbox_lock_timeout_is_fail_open(tmp_path, monkeypatch):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    ready = threading.Event()
    release = threading.Event()

    def hold_lock():
        with protocol._outbox_lock(outbox):
            ready.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert ready.wait(timeout=2)
    monkeypatch.setattr(protocol, "_OUTBOX_LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        matches, receipt = recall_for_worker(_recall_request())
    finally:
        release.set()
        holder.join(timeout=2)

    assert holder.is_alive() is False
    assert matches == []
    assert receipt.status == "unavailable"
    assert receipt.continue_work is True
    assert not list(outbox.glob("recall-*.json"))


def test_recall_outbox_disk_failure_is_fail_open(tmp_path, monkeypatch):
    _configured_paths(tmp_path, monkeypatch, create_vault=False)

    def fail_disk(*_args, **_kwargs):
        raise OSError("simulated outbox disk failure")

    monkeypatch.setattr(protocol, "_atomic_write", fail_disk)

    matches, receipt = recall_for_worker(_recall_request())

    assert matches == []
    assert receipt.status == "unavailable"
    assert receipt.continue_work is True


def test_unavailable_recall_incident_never_queues_secret_query_text(tmp_path, monkeypatch):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    request = _recall_request()
    request = WorkerRecallRequest(
        operation_id=request.operation_id,
        task_id=request.task_id,
        run_id=request.run_id,
        delegation_id=request.delegation_id,
        function_id=request.function_id,
        title=request.title,
        query=f"find memory for token {secret}",
        executor=request.executor,
    )

    recall_for_worker(request)

    raw = next(outbox.glob("recall-*.json")).read_bytes()
    assert secret.encode() not in raw
    assert b"find memory for token" not in raw


def test_configured_outbox_path_defaults_to_profile_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_AGENT_MEMORY_OUTBOX", raising=False)
    assert configured_outbox_path() == home / "recovery" / "agent-memory-outbox"


def test_configured_outbox_path_normalizes_relative_hermes_home(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", "relative-profile")
    monkeypatch.delenv("HERMES_AGENT_MEMORY_OUTBOX", raising=False)

    outbox = configured_outbox_path()

    assert outbox == (
        tmp_path / "relative-profile" / "recovery" / "agent-memory-outbox"
    ).resolve()
    assert outbox.is_absolute()


def test_configured_outbox_path_rejects_relative_explicit_override(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_MEMORY_OUTBOX", "relative-outbox")

    with pytest.raises(ValueError, match="absolute"):
        configured_outbox_path()


def test_unconfigured_write_returns_disabled_without_creating_outbox(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_AGENT_MEMORY_VAULT", raising=False)
    monkeypatch.delenv("HERMES_AGENT_MEMORY_OUTBOX", raising=False)
    monkeypatch.setattr(protocol, "configured_vault_path", lambda: None)

    receipt = write_worker_gist(_write_request("exec-123", "gist-123"))

    assert receipt.status == "disabled"
    assert receipt.continue_work is True
    assert not configured_outbox_path().exists()


def test_queue_portability_without_os_fchmod(tmp_path, monkeypatch):
    _configured_paths(tmp_path, monkeypatch, create_vault=False)
    monkeypatch.delattr(protocol.os, "fchmod", raising=False)

    receipt = write_worker_gist(_write_request("exec-123", "gist-123"))

    assert receipt.status == "queued"
    assert receipt_is_present(receipt) is True


def test_outbox_rejects_symlinked_root_without_mutating_external_target(
    tmp_path, monkeypatch
):
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    original_mode = external.stat().st_mode
    outbox = tmp_path / "outbox-link"
    outbox.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("HERMES_AGENT_MEMORY_VAULT", str(tmp_path / "missing-vault"))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_OUTBOX", str(outbox))

    with pytest.raises(ValueError, match="symlink"):
        write_worker_gist(_write_request("exec-123", "gist-123"))

    assert list(external.iterdir()) == []
    assert external.stat().st_mode == original_mode


def test_outbox_rejects_symlinked_existing_component_before_mkdir(
    tmp_path, monkeypatch
):
    external = tmp_path / "external"
    external.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external, target_is_directory=True)
    outbox = linked_parent / "nested-outbox"
    monkeypatch.setenv("HERMES_AGENT_MEMORY_VAULT", str(tmp_path / "missing-vault"))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_OUTBOX", str(outbox))

    with pytest.raises(ValueError, match="symlink"):
        write_worker_gist(_write_request("exec-123", "gist-123"))

    assert not (external / "nested-outbox").exists()


def test_outbox_envelope_exact_size_boundary_and_one_byte_over(
    tmp_path, monkeypatch
):
    _, outbox = _configured_paths(tmp_path, monkeypatch, create_vault=False)
    write_worker_gist(_write_request("exec-123", "gist-123"))
    path = outbox / "gist-gist-123.json"
    original = path.read_bytes()
    limit = getattr(protocol, "_MAX_OUTBOX_ENVELOPE_BYTES", 131_072)
    assert len(original) < limit

    path.write_bytes(original + (b" " * (limit - len(original))))
    assert protocol._load_envelope(path)["kind"] == "gist"

    path.write_bytes(original + (b" " * (limit + 1 - len(original))))
    with pytest.raises(ValueError, match="corrupt"):
        protocol._load_envelope(path)
    status = configured_outbox_status()
    assert status.reason == "corrupt_or_unsafe"
    assert status.attention_required is True


def test_functional_identity_reuses_work_contract_identity_algorithm():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, idempotency_key TEXT, work_contract_id TEXT
        );
        CREATE TABLE work_contracts (id TEXT PRIMARY KEY, canonical_json TEXT);
        """
    )
    contract = {
        "work": {
            "item_kind": "feature", "work_type": "implementation",
            "title": "Durable memory", "outcome": "Worker gists survive restarts",
            "scope": ["protocol"], "out_of_scope": ["cockpit"],
        }
    }
    conn.execute("INSERT INTO work_contracts VALUES (?, ?)", ("wc-1", json.dumps(contract)))
    conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)", ("t-1", "Fallback", None, "wc-1"))

    identity = functional_identity_for_task(conn, "t-1")

    assert identity is not None
    function_id, title, query = identity
    assert function_id.startswith("function-")
    assert title == "Durable memory"
    assert "Worker gists survive restarts" in query


def test_receipt_presence_rejects_queued_claim_without_envelope(tmp_path, monkeypatch):
    _configured_paths(tmp_path, monkeypatch, create_vault=False)
    receipt = MemoryReceipt.for_gist(
        operation_id="write-exec-123", operation="write", status="queued",
        continue_work=True, task_id="t-memory", run_id=7,
        delegation_id="delegation-7", gist_id="missing-gist",
        executor=_write_request("exec-123", "gist-123").executor,
    )
    assert receipt_is_present(receipt) is False


def test_stored_receipt_presence_is_false_when_vault_config_resolution_fails(
    tmp_path, monkeypatch
):
    _configured_paths(tmp_path, monkeypatch, create_vault=True)
    receipt = write_worker_gist(_write_request("exec-123", "gist-123"))
    assert receipt.status == "stored"

    def fail_config():
        raise OSError("persistent vault config failure")

    monkeypatch.setattr(protocol, "configured_vault_path", fail_config)

    assert receipt_is_present(receipt) is False


def test_old_same_function_gist_without_current_operation_id_rejects_receipt(
    tmp_path, monkeypatch
):
    vault, _outbox = _configured_paths(tmp_path, monkeypatch, create_vault=True)
    request = _write_request("exec-123", "gist-old")
    old_gist = SessionGist(
        gist_id=request.gist_id,
        occurred_at=request.occurred_at,
        agent_id=request.executor.agent_id,
        role=request.executor.hermes_role,
        function_id=request.function_id,
        title=request.title,
        context=request.context,
        summary=request.summary,
        reused=request.reused,
        result=request.result,
        maturity=request.maturity,
        evidence=request.evidence,
        behavior=request.behavior,
        decisions=request.decisions,
        open_loops=request.open_loops,
        executor=request.executor,
    )
    assert append_gist(vault, old_gist) is True
    fabricated = MemoryReceipt.for_gist(
        operation_id=request.operation_id,
        operation="write",
        status="stored",
        continue_work=True,
        task_id=request.task_id,
        run_id=request.run_id,
        delegation_id=request.delegation_id,
        gist_id=request.gist_id,
        executor=request.executor,
    )

    assert receipt_is_present(fabricated) is False
