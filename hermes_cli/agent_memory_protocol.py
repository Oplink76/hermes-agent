"""Strict worker receipts and a durable local outbox for Agent Memory."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Iterator, Mapping

from hermes_constants import get_hermes_home
from hermes_cli import agent_memory_vault as vault_api
from hermes_cli.agent_memory_vault import (
    ExecutorIdentity,
    MemoryMatch,
    SessionGist,
    append_gist,
    configured_vault_path,
    functional_identity_for_task,
    lint_vault,
    recall,
)


_OUTBOX_LOCK_TIMEOUT_SECONDS = 5.0
_OUTBOX_LOCK_POLL_SECONDS = 0.05
_ATTENTION_AFTER = timedelta(hours=24)
_ACK_NAME = ".attention-ack.json"
_WRITE_STATUSES = frozenset({"stored", "already_stored", "queued", "disabled"})
_RECALL_STATUSES = frozenset({"matched", "empty", "unavailable", "disabled"})


def _sorted_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: value[key] for key in sorted(value)}


def _utc_now() -> datetime:
    return datetime.now()


def _datetime_text(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ValueError("Agent Memory timestamp must be a datetime")
    return value.isoformat(timespec="microseconds")


def _datetime_value(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Agent Memory timestamp must be an ISO string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Agent Memory timestamp must be an ISO string") from exc


def _identifier(value: object, name: str) -> str:
    try:
        return vault_api._gist_id(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a bounded opaque identifier") from exc


def _run_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("run_id must be a non-negative integer")
    return value


def _text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > vault_api._MAX_RECORDED_CHARS
    ):
        raise ValueError(f"{name} must be bounded text")
    return value


def _exact_mapping(
    value: object, expected: frozenset[str], name: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} must contain exactly the canonical keys")
    return value


@dataclass(frozen=True)
class MemoryReceipt:
    operation_id: str
    operation: str
    status: str
    continue_work: bool
    task_id: str
    run_id: int
    delegation_id: str
    gist_id: str | None
    executor: ExecutorIdentity
    occurred_at: datetime

    @classmethod
    def from_mapping(cls, value: object) -> "MemoryReceipt":
        mapping = _exact_mapping(
            value,
            frozenset(
                {
                    "continue_work", "delegation_id", "executor", "gist_id",
                    "occurred_at", "operation", "operation_id", "run_id",
                    "status", "task_id",
                }
            ),
            "memory receipt",
        )
        gist_id_value = mapping["gist_id"]
        if gist_id_value is not None:
            gist_id_value = _identifier(gist_id_value, "gist_id")
        continue_work = mapping["continue_work"]
        if not isinstance(continue_work, bool):
            raise ValueError("continue_work must be a boolean")
        receipt = cls(
            operation_id=_identifier(mapping["operation_id"], "operation_id"),
            operation=_text(mapping["operation"], "operation"),
            status=_text(mapping["status"], "status"),
            continue_work=continue_work,
            task_id=_identifier(mapping["task_id"], "task_id"),
            run_id=_run_id(mapping["run_id"]),
            delegation_id=_identifier(mapping["delegation_id"], "delegation_id"),
            gist_id=gist_id_value,
            executor=ExecutorIdentity.from_mapping(mapping["executor"]),
            occurred_at=_datetime_value(mapping["occurred_at"]),
        )
        receipt._validate_status()
        return receipt

    @classmethod
    def for_gist(
        cls,
        operation_id: str,
        operation: str,
        status: str,
        continue_work: bool,
        task_id: str,
        run_id: int,
        delegation_id: str,
        gist_id: str | None,
        executor: ExecutorIdentity,
    ) -> "MemoryReceipt":
        receipt = cls(
            operation_id=_identifier(operation_id, "operation_id"),
            operation=operation,
            status=status,
            continue_work=continue_work,
            task_id=_identifier(task_id, "task_id"),
            run_id=_run_id(run_id),
            delegation_id=_identifier(delegation_id, "delegation_id"),
            gist_id=_identifier(gist_id, "gist_id") if gist_id is not None else None,
            executor=ExecutorIdentity.from_mapping(executor.to_mapping()),
            occurred_at=_utc_now(),
        )
        receipt._validate_status()
        if not isinstance(continue_work, bool):
            raise ValueError("continue_work must be a boolean")
        return receipt

    def _validate_status(self) -> None:
        if self.operation == "write":
            allowed = _WRITE_STATUSES
        elif self.operation == "recall":
            allowed = _RECALL_STATUSES
        else:
            allowed = frozenset()
        if self.status not in allowed:
            raise ValueError("unsupported Agent Memory receipt operation or status")
        if self.operation == "write" and self.gist_id is None:
            raise ValueError("write receipts require gist_id")
        if self.operation == "recall" and self.gist_id is not None:
            raise ValueError("recall receipts cannot contain gist_id")

    def to_mapping(self) -> dict[str, object]:
        self._validate_status()
        return _sorted_mapping(
            {
                "operation_id": self.operation_id,
                "operation": self.operation,
                "status": self.status,
                "continue_work": self.continue_work,
                "task_id": self.task_id,
                "run_id": self.run_id,
                "delegation_id": self.delegation_id,
                "gist_id": self.gist_id,
                "executor": self.executor.to_mapping(),
                "occurred_at": _datetime_text(self.occurred_at),
            }
        )


@dataclass(frozen=True)
class WorkerRecallRequest:
    operation_id: str
    task_id: str
    run_id: int
    delegation_id: str
    function_id: str
    title: str
    query: str
    executor: ExecutorIdentity

    @classmethod
    def from_mapping(cls, value: object) -> "WorkerRecallRequest":
        mapping = _exact_mapping(value, _RECALL_REQUEST_KEYS, "worker recall request")
        return cls(
            operation_id=_identifier(mapping["operation_id"], "operation_id"),
            task_id=_identifier(mapping["task_id"], "task_id"),
            run_id=_run_id(mapping["run_id"]),
            delegation_id=_identifier(mapping["delegation_id"], "delegation_id"),
            function_id=_identifier(mapping["function_id"], "function_id"),
            title=_text(mapping["title"], "title"),
            query=_text(mapping["query"], "query"),
            executor=ExecutorIdentity.from_mapping(mapping["executor"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return _sorted_mapping(
            {
                "operation_id": self.operation_id, "task_id": self.task_id,
                "run_id": self.run_id, "delegation_id": self.delegation_id,
                "function_id": self.function_id, "title": self.title,
                "query": self.query, "executor": self.executor.to_mapping(),
            }
        )


_RECALL_REQUEST_KEYS = frozenset(
    {
        "operation_id",
        "task_id",
        "run_id",
        "delegation_id",
        "function_id",
        "title",
        "query",
        "executor",
    }
)


@dataclass
class WorkerWriteRequest:
    operation_id: str
    task_id: str
    run_id: int
    delegation_id: str
    gist_id: str
    occurred_at: datetime
    function_id: str
    title: str
    context: str
    summary: str
    reused: str
    result: str
    maturity: str
    evidence: str
    behavior: str
    decisions: str
    open_loops: str
    executor: ExecutorIdentity

    @classmethod
    def from_mapping(cls, value: object) -> "WorkerWriteRequest":
        mapping = _exact_mapping(value, _WRITE_REQUEST_KEYS, "worker write request")
        return cls(
            operation_id=_identifier(mapping["operation_id"], "operation_id"),
            task_id=_identifier(mapping["task_id"], "task_id"),
            run_id=_run_id(mapping["run_id"]),
            delegation_id=_identifier(mapping["delegation_id"], "delegation_id"),
            gist_id=_identifier(mapping["gist_id"], "gist_id"),
            occurred_at=_datetime_value(mapping["occurred_at"]),
            function_id=_identifier(mapping["function_id"], "function_id"),
            title=_text(mapping["title"], "title"),
            context=_text(mapping["context"], "context"),
            summary=_text(mapping["summary"], "summary"),
            reused=_text(mapping["reused"], "reused"),
            result=_text(mapping["result"], "result"),
            maturity=_text(mapping["maturity"], "maturity"),
            evidence=_text(mapping["evidence"], "evidence"),
            behavior=_text(mapping["behavior"], "behavior"),
            decisions=_text(mapping["decisions"], "decisions"),
            open_loops=_text(mapping["open_loops"], "open_loops"),
            executor=ExecutorIdentity.from_mapping(mapping["executor"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return _sorted_mapping(
            {
                "operation_id": self.operation_id, "task_id": self.task_id,
                "run_id": self.run_id, "delegation_id": self.delegation_id,
                "gist_id": self.gist_id, "occurred_at": _datetime_text(self.occurred_at),
                "function_id": self.function_id, "title": self.title,
                "context": self.context, "summary": self.summary,
                "reused": self.reused, "result": self.result,
                "maturity": self.maturity, "evidence": self.evidence,
                "behavior": self.behavior, "decisions": self.decisions,
                "open_loops": self.open_loops, "executor": self.executor.to_mapping(),
            }
        )

    def to_session_gist(self) -> SessionGist:
        validated = WorkerWriteRequest.from_mapping(self.to_mapping())
        return SessionGist(
            gist_id=validated.gist_id,
            occurred_at=validated.occurred_at,
            agent_id=validated.executor.agent_id,
            role=validated.executor.hermes_role,
            function_id=validated.function_id,
            title=validated.title,
            context=validated.context,
            summary=validated.summary,
            reused=validated.reused,
            result=validated.result,
            maturity=validated.maturity,
            evidence=validated.evidence,
            behavior=validated.behavior,
            decisions=validated.decisions,
            open_loops=validated.open_loops,
            executor=validated.executor,
        )


_WRITE_REQUEST_KEYS = frozenset(
    {
        "operation_id", "task_id", "run_id", "delegation_id", "gist_id",
        "occurred_at", "function_id", "title", "context", "summary", "reused",
        "result", "maturity", "evidence", "behavior", "decisions", "open_loops",
        "executor",
    }
)


@dataclass(frozen=True)
class ReconcileReport:
    moved: int
    closed_incidents: int
    pending: int
    corrupt: int
    vault_available: bool

    def to_mapping(self) -> dict[str, object]:
        return _sorted_mapping(self.__dict__)


@dataclass(frozen=True)
class OutboxStatus:
    enabled: bool
    vault_available: bool
    pending: int
    oldest_pending_hours: float
    attention_required: bool
    reason: str
    fingerprint: str
    notify_ole: bool

    def to_mapping(self) -> dict[str, object]:
        return _sorted_mapping(self.__dict__)


def configured_outbox_path(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    configured = (environment.get("HERMES_AGENT_MEMORY_OUTBOX") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("Agent Memory outbox path must be absolute")
        return path.resolve(strict=False)
    return (get_hermes_home() / "agent-memory" / "outbox").resolve(strict=False)


def write_worker_gist(request: WorkerWriteRequest) -> MemoryReceipt:
    request = WorkerWriteRequest.from_mapping(request.to_mapping())
    return store_gist_or_queue(
        request.to_session_gist(), operation_id=request.operation_id,
        task_id=request.task_id, run_id=request.run_id,
        delegation_id=request.delegation_id, executor=request.executor,
    )


def store_gist_or_queue(
    gist: SessionGist,
    *,
    operation_id: str,
    task_id: str,
    run_id: int,
    delegation_id: str,
    executor: ExecutorIdentity,
) -> MemoryReceipt:
    gist = replace(gist, executor=executor)
    _validated_gist_mapping(gist)
    outbox = configured_outbox_path()
    try:
        vault = configured_vault_path()
        if vault is not None and vault.is_dir():
            stored = append_gist(vault, gist)
            lint_vault(vault)
            return MemoryReceipt.for_gist(
                operation_id=operation_id, task_id=task_id, run_id=run_id,
                delegation_id=delegation_id, executor=executor, operation="write",
                status="stored" if stored else "already_stored", continue_work=True,
                gist_id=gist.gist_id,
            )
    except Exception:
        # A validated gist still needs a durable handover when the external
        # vault or its configuration is temporarily unavailable.
        pass
    receipt = MemoryReceipt.for_gist(
        operation_id=operation_id, task_id=task_id, run_id=run_id,
        delegation_id=delegation_id, executor=executor, operation="write",
        status="queued", continue_work=True, gist_id=gist.gist_id,
    )
    envelope = _gist_envelope(gist, receipt)
    return _queue_envelope(outbox, envelope)


def recall_for_worker(request: WorkerRecallRequest) -> tuple[list[MemoryMatch], MemoryReceipt]:
    request = WorkerRecallRequest.from_mapping(request.to_mapping())
    try:
        vault = configured_vault_path()
        if vault is None:
            return [], _recall_receipt(request, "disabled")
        if not vault.is_dir():
            return [], _unavailable_recall(request)
        matches = recall(vault, request.query, limit=5)
    except Exception:
        return [], _unavailable_recall(request)
    return matches, _recall_receipt(request, "matched" if matches else "empty")


def _unavailable_recall(request: WorkerRecallRequest) -> MemoryReceipt:
    """Return fail-open recall status after best-effort incident recording."""
    receipt = _recall_receipt(request, "unavailable")
    try:
        return _queue_envelope(
            configured_outbox_path(), _recall_incident(request, receipt)
        )
    except Exception:
        return receipt


def _recall_receipt(request: WorkerRecallRequest, status: str) -> MemoryReceipt:
    return MemoryReceipt.for_gist(
        operation_id=request.operation_id, operation="recall", status=status,
        continue_work=True, task_id=request.task_id, run_id=request.run_id,
        delegation_id=request.delegation_id, gist_id=None, executor=request.executor,
    )


def _validated_gist_mapping(gist: SessionGist) -> dict[str, object]:
    rendered = vault_api._render_gist(gist, vault_api._gist_id(gist.gist_id))
    match = vault_api._GIST_RE.fullmatch(rendered)
    parsed = vault_api._parse_gist(match, Path("outbox")) if match is not None else None
    if parsed is None:
        raise ValueError("Session Gist does not satisfy the vault schema")
    function_id, _, title = parsed.fields["Function"].partition(" | ")
    executor = ExecutorIdentity.from_mapping(json.loads(parsed.fields["Executor"]))
    return _sorted_mapping(
        {
            "gist_id": parsed.gist_id,
            "occurred_at": _datetime_text(gist.occurred_at),
            "agent_id": vault_api._recorded_text(gist.agent_id),
            "role": vault_api._recorded_text(gist.role),
            "function_id": function_id,
            "title": title,
            "context": parsed.fields["Context"],
            "summary": parsed.fields["Summary"],
            "reused": parsed.fields["Reused"],
            "result": parsed.fields["Result"],
            "maturity": parsed.fields["Maturity"],
            "evidence": parsed.fields["Evidence"],
            "behavior": parsed.fields["Behavior"],
            "decisions": parsed.fields["Decisions"],
            "open_loops": parsed.fields["Open loops"],
            "executor": executor.to_mapping(),
        }
    )


_GIST_KEYS = frozenset(
    {
        "gist_id",
        "occurred_at",
        "agent_id",
        "role",
        "function_id",
        "title",
        "context",
        "summary",
        "reused",
        "result",
        "maturity",
        "evidence",
        "behavior",
        "decisions",
        "open_loops",
        "executor",
    }
)


def _gist_from_mapping(value: object) -> SessionGist:
    mapping = _exact_mapping(value, _GIST_KEYS, "queued gist")
    gist = SessionGist(
        gist_id=_identifier(mapping["gist_id"], "gist_id"),
        occurred_at=_datetime_value(mapping["occurred_at"]),
        agent_id=_text(mapping["agent_id"], "agent_id"),
        role=_text(mapping["role"], "role"),
        function_id=_identifier(mapping["function_id"], "function_id"),
        title=_text(mapping["title"], "title"),
        context=_text(mapping["context"], "context"),
        summary=_text(mapping["summary"], "summary"),
        reused=_text(mapping["reused"], "reused"),
        result=_text(mapping["result"], "result"),
        maturity=_text(mapping["maturity"], "maturity"),
        evidence=_text(mapping["evidence"], "evidence"),
        behavior=_text(mapping["behavior"], "behavior"),
        decisions=_text(mapping["decisions"], "decisions"),
        open_loops=_text(mapping["open_loops"], "open_loops"),
        executor=ExecutorIdentity.from_mapping(mapping["executor"]),
    )
    _validated_gist_mapping(gist)
    return gist


def _gist_envelope(gist: SessionGist, receipt: MemoryReceipt) -> dict[str, object]:
    return _sorted_mapping(
        {
            "version": 1,
            "kind": "gist",
            "identity": f"gist:{gist.gist_id}",
            "queued_at": _datetime_text(_utc_now()),
            "gist": _validated_gist_mapping(gist),
            "receipt": receipt.to_mapping(),
        }
    )


def _recall_incident(
    request: WorkerRecallRequest, receipt: MemoryReceipt
) -> dict[str, object]:
    return _sorted_mapping(
        {
            "version": 1,
            "kind": "recall",
            "identity": f"recall:{request.operation_id}",
            "queued_at": _datetime_text(_utc_now()),
            "receipt": receipt.to_mapping(),
        }
    )


def _envelope_name(envelope: Mapping[str, object]) -> str:
    kind = envelope.get("kind")
    if kind == "gist":
        gist = _gist_from_mapping(envelope.get("gist"))
        return f"gist-{gist.gist_id}.json"
    if kind == "recall":
        receipt = MemoryReceipt.from_mapping(envelope.get("receipt"))
        return f"recall-{receipt.operation_id}.json"
    raise ValueError("unsupported Agent Memory outbox envelope kind")


def _validate_envelope(value: object, path: Path | None = None) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        raise ValueError("unsupported Agent Memory outbox envelope")
    kind = value.get("kind")
    expected = (
        frozenset({"version", "kind", "identity", "queued_at", "gist", "receipt"})
        if kind == "gist"
        else frozenset({"version", "kind", "identity", "queued_at", "receipt"})
        if kind == "recall"
        else frozenset()
    )
    mapping = _exact_mapping(value, expected, "outbox envelope")
    _datetime_value(mapping["queued_at"])
    receipt = MemoryReceipt.from_mapping(mapping["receipt"])
    if kind == "gist":
        gist = _gist_from_mapping(mapping["gist"])
        expected_identity = f"gist:{gist.gist_id}"
        if (
            receipt.operation != "write"
            or receipt.status != "queued"
            or receipt.gist_id != gist.gist_id
        ):
            raise ValueError("queued gist receipt does not match its envelope")
    else:
        expected_identity = f"recall:{receipt.operation_id}"
        if receipt.operation != "recall" or receipt.status != "unavailable":
            raise ValueError("recall receipt does not match its incident")
    if mapping["identity"] != expected_identity:
        raise ValueError("outbox envelope identity does not match its payload")
    canonical = _sorted_mapping(dict(mapping))
    if path is not None and path.name != _envelope_name(canonical):
        raise ValueError("outbox envelope filename does not match its payload")
    return canonical


def _queue_envelope(outbox: Path, envelope: Mapping[str, object]) -> MemoryReceipt:
    validated = _validate_envelope(envelope)
    target = outbox / _envelope_name(validated)
    canonical = (
        json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    with _outbox_lock(outbox):
        if target.exists():
            existing = _load_envelope(target)
            if not _same_operation_envelope(existing, validated):
                raise ValueError("conflicting Agent Memory outbox envelope identity")
            _fsync_directory(outbox)
            return MemoryReceipt.from_mapping(existing["receipt"])
        _atomic_write(outbox, target, canonical.encode("utf-8"))
        if not target.is_file():
            raise OSError("Agent Memory outbox envelope was not durably created")
    return MemoryReceipt.from_mapping(validated["receipt"])


def _same_operation_envelope(
    existing: Mapping[str, object], candidate: Mapping[str, object]
) -> bool:
    if existing.get("kind") != candidate.get("kind") or existing.get(
        "identity"
    ) != candidate.get("identity"):
        return False
    if existing.get("kind") == "gist" and existing.get("gist") != candidate.get(
        "gist"
    ):
        return False
    existing_receipt = dict(existing.get("receipt", {}))
    candidate_receipt = dict(candidate.get("receipt", {}))
    existing_receipt.pop("occurred_at", None)
    candidate_receipt.pop("occurred_at", None)
    return existing_receipt == candidate_receipt


def _load_envelope(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe Agent Memory outbox envelope")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("corrupt Agent Memory outbox envelope") from exc
    return _validate_envelope(value, path)


@contextlib.contextmanager
def _outbox_lock(outbox: Path) -> Iterator[None]:
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(outbox, 0o700)
    except OSError:
        pass
    lock_path = outbox / ".agent-memory.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "r+b")
    except Exception:
        os.close(descriptor)
        raise
    acquired = False
    try:
        deadline = time.monotonic() + _OUTBOX_LOCK_TIMEOUT_SECONDS
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring Agent Memory outbox lock: {lock_path}"
                    )
                time.sleep(_OUTBOX_LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_write(outbox: Path, target: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=outbox, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(outbox)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Durably persist a replaced directory entry when the platform supports it."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    unsupported = {
        errno.EBADF,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


def reconcile_configured_outbox(now: datetime | None = None) -> ReconcileReport:
    current = _utc_now() if now is None else now
    outbox = configured_outbox_path()
    vault = configured_vault_path()
    vault_available = vault is not None and vault.is_dir()
    if not outbox.exists():
        return ReconcileReport(0, 0, 0, 0, vault_available)
    moved = 0
    closed = 0
    with _outbox_lock(outbox):
        for path in _pending_paths(outbox):
            try:
                envelope = _load_envelope(path)
            except ValueError:
                continue
            if not vault_available:
                continue
            if envelope["kind"] == "recall":
                try:
                    recall(vault, "agent-memory-health-probe", limit=1)
                except (OSError, TimeoutError, ValueError):
                    continue
                path.unlink()
                closed += 1
                continue
            gist = _gist_from_mapping(envelope["gist"])
            try:
                append_gist(vault, gist)
                lint_vault(vault)
                matches = recall(vault, gist.gist_id, limit=1)
            except (OSError, TimeoutError, ValueError):
                continue
            if not matches or matches[0].gist_id != gist.gist_id:
                continue
            path.unlink()
            moved += 1
        pending, corrupt, _, _ = _pending_state(outbox, fallback_now=current)
    return ReconcileReport(moved, closed, pending, corrupt, vault_available)


def _pending_paths(outbox: Path) -> list[Path]:
    return sorted(path for path in outbox.glob("*.json") if path.name != _ACK_NAME)


def _pending_state(
    outbox: Path, *, fallback_now: datetime | None = None
) -> tuple[int, int, datetime | None, list[str]]:
    corrupt = 0
    oldest = None
    fingerprints = []
    paths = _pending_paths(outbox)
    for path in paths:
        try:
            envelope = _load_envelope(path)
            queued_at = _datetime_value(envelope["queued_at"])
            identity = str(envelope["identity"])
            reason = "pending"
        except ValueError:
            corrupt += 1
            try:
                raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                modified = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                raw_hash = "unreadable"
                modified = _utc_now() if fallback_now is None else fallback_now
            queued_at = modified
            identity = f"corrupt:{path.name}:{raw_hash}"
            reason = "corrupt_or_unsafe"
        oldest = queued_at if oldest is None or queued_at < oldest else oldest
        fingerprints.append(f"{identity}|{reason}")
    return len(paths), corrupt, oldest, sorted(fingerprints)


def configured_outbox_status(now: datetime | None = None) -> OutboxStatus:
    current = _utc_now() if now is None else now
    outbox = configured_outbox_path()
    vault = configured_vault_path()
    vault_available = vault is not None and vault.is_dir()
    if outbox.exists():
        with _outbox_lock(outbox):
            pending, corrupt, oldest, identities = _pending_state(
                outbox, fallback_now=current
            )
    else:
        pending, corrupt, oldest, identities = 0, 0, None, []
    oldest_hours = (
        max(0.0, (current - oldest).total_seconds() / 3600)
        if oldest is not None
        else 0.0
    )
    if corrupt:
        attention = True
        reason = "corrupt_or_unsafe"
    elif pending and oldest is not None and current - oldest >= _ATTENTION_AFTER:
        attention = True
        reason = "pending_for_24_hours"
    else:
        attention = False
        reason = "none"
    fingerprint = hashlib.sha256(
        json.dumps(identities, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    acknowledged = _read_ack(outbox)
    return OutboxStatus(
        enabled=vault is not None,
        vault_available=vault_available,
        pending=pending,
        oldest_pending_hours=oldest_hours,
        attention_required=attention,
        reason=reason,
        fingerprint=fingerprint,
        notify_ole=attention and acknowledged != fingerprint,
    )


def _read_ack(outbox: Path) -> str | None:
    path = outbox / _ACK_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        isinstance(value, dict)
        and set(value) == {"fingerprint"}
        and isinstance(value["fingerprint"], str)
    ):
        return value["fingerprint"]
    return None


def acknowledge_attention(fingerprint: str) -> None:
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError("attention fingerprint must be a SHA-256 digest")
    outbox = configured_outbox_path()
    with _outbox_lock(outbox):
        _atomic_write(
            outbox,
            outbox / _ACK_NAME,
            (
                json.dumps(
                    {"fingerprint": fingerprint},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )


def receipt_is_present(receipt: MemoryReceipt) -> bool:
    try:
        receipt = MemoryReceipt.from_mapping(receipt.to_mapping())
    except (TypeError, ValueError):
        return False
    if receipt.operation != "write" or receipt.status not in {
        "stored",
        "already_stored",
        "queued",
    }:
        return False
    if receipt.status == "queued":
        path = configured_outbox_path() / f"gist-{receipt.gist_id}.json"
        try:
            envelope = _load_envelope(path)
            stored = MemoryReceipt.from_mapping(envelope["receipt"])
        except (OSError, ValueError):
            return False
        return stored.to_mapping() == receipt.to_mapping()
    vault = configured_vault_path()
    if vault is None or not vault.is_dir():
        return False
    try:
        return any(
            match.gist_id == receipt.gist_id
            for match in recall(vault, receipt.gist_id or "", limit=1)
        )
    except (OSError, TimeoutError, ValueError):
        return False
