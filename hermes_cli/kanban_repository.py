"""Validated repository policy for governed Kanban boards.

The repository contract is deliberately data-only.  It describes the refs and
commands that a board is allowed to use; the lifecycle coordinator owns the
SQLite state transitions that consume it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast


FULL_SHA = re.compile(r"[0-9a-f]{40}")
FULL_DIGEST = re.compile(r"[0-9a-f]{64}")
RELEASE_CANDIDATE_REF_PREFIX = "refs/hermes/release-candidates/"

_REPOSITORY_KEYS = frozenset(
    {
        "base_ref",
        "target_branch",
        "verification_profiles",
        "ci_observation",
        "boundary_evidence",
    }
)
_VERIFICATION_PROFILE_KEYS = frozenset({"commands"})
_VERIFICATION_COMMAND_KEYS = frozenset({"argv", "workdir", "timeout_seconds"})
_CI_OBSERVATION_KEYS = frozenset({"provider", "required_workflows"})
_BOUNDARY_EVIDENCE_KEYS = frozenset(
    {"test_globs", "fixture_globs", "generated_paths"}
)
_MAX_TIMEOUT_SECONDS = 86_400


class RepositoryConfigurationError(ValueError):
    """A repository contract cannot safely be used.

    ``code`` is intentionally stable so callers can distinguish operator
    configuration failures from test or infrastructure failures without
    parsing an exception message.
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class VerificationCommand:
    argv: tuple[str, ...]
    workdir: PurePosixPath
    timeout_seconds: int


@dataclass(frozen=True)
class VerificationProfile:
    commands: tuple[VerificationCommand, ...]


@dataclass(frozen=True)
class VerificationReceiptKey:
    candidate_sha: str
    contract_digest: str
    command_set_digest: str
    runtime_toolchain_digest: str
    generated_policy_digest: str
    gate_kind: str
    executor_policy: str
    digest: str


@dataclass(frozen=True)
class VerificationReceipt:
    key: VerificationReceiptKey
    result_digest: str
    created_at: int


@dataclass(frozen=True)
class VerificationStepResult:
    """Bounded evidence for one configured verification command."""

    argv: tuple[str, ...]
    workdir: PurePosixPath
    status: Literal["passed", "failed", "configuration_error", "infrastructure_error"]
    returncode: int | None
    duration_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    @property
    def stdout(self) -> str:
        """Compatibility alias for consumers that use the shorter field name."""

        return self.stdout_tail

    @property
    def stderr(self) -> str:
        """Compatibility alias for consumers that use the shorter field name."""

        return self.stderr_tail


@dataclass(frozen=True)
class VerificationResult:
    """Typed outcome of running one repository verification profile."""

    status: Literal["passed", "failed", "configuration_error", "infrastructure_error"]
    source_sha: str
    candidate_sha: str
    contract_digest: str
    profile: str
    steps: tuple[VerificationStepResult, ...]
    key: VerificationReceiptKey
    error: str | None = None
    reused: bool = False


@dataclass(frozen=True)
class PreparedRefCASResult:
    """Typed outcome of the sole prepared-candidate target-ref CAS path."""

    kind: Literal["advanced", "reflected", "checked_out", "target_moved"]
    current_sha: str | None


@dataclass(frozen=True)
class PreparedRefRecoveryResult:
    """Relationship between a prepared candidate and the current target tip."""

    kind: Literal["preimage", "candidate", "descendant", "diverged"]
    current_sha: str | None


_VERIFICATION_OUTPUT_TAIL_CHARS = 4096
_VERIFICATION_ENV_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONHASHSEED",
    "TZ",
)
_VERIFICATION_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|password|secret|token)\b\s*[:=]\s*)([^\s,;]+)"
)
_VERIFICATION_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([^\s,;]+)")


def _verification_tail(value: object) -> str:
    """Return only the bounded tail of subprocess output."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = _VERIFICATION_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _VERIFICATION_BEARER_RE.sub(r"\1[REDACTED]", text)
    return text[-_VERIFICATION_OUTPUT_TAIL_CHARS:]


def _verification_result(
    *,
    status: Literal["passed", "failed", "configuration_error", "infrastructure_error"],
    source_sha: str,
    candidate_sha: str,
    contract_digest: str,
    profile: str,
    steps: list[VerificationStepResult],
    key: VerificationReceiptKey,
    error: str | None = None,
) -> VerificationResult:
    return VerificationResult(
        status=status,
        source_sha=source_sha,
        candidate_sha=candidate_sha,
        contract_digest=contract_digest,
        profile=profile,
        steps=tuple(steps),
        error=error,
        key=key,
    )


def _verification_workdir(candidate_root: Path, workdir: PurePosixPath) -> Path | None:
    if workdir.is_absolute() or ".." in workdir.parts:
        return None
    resolved_root = candidate_root.resolve(strict=False)
    resolved = (resolved_root / Path(*workdir.parts)).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved if resolved.is_dir() else None


def _verification_executable(
    argv0: str, *, candidate_root: Path, workdir: Path, path_value: str
) -> str | None:
    """Resolve a configured executable without invoking a shell."""

    if not isinstance(argv0, str) or not argv0 or "\x00" in argv0:
        return None
    requested = Path(argv0)
    local_requested = requested if requested.is_absolute() else workdir / requested
    if (
        requested.is_absolute()
        or "/" in argv0
        or "\\" in argv0
        or (local_requested.is_file() and os.access(local_requested, os.X_OK))
    ):
        requested = local_requested
        resolved = requested.resolve(strict=False)
        try:
            resolved.relative_to(candidate_root.resolve(strict=False))
        except ValueError:
            # Absolute tools such as ``/usr/bin/python3`` are valid; only
            # relative configured paths must remain inside the candidate.
            if not Path(argv0).is_absolute():
                return None
        return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else None
    return shutil.which(argv0, path=path_value)


def _verification_environment(
    candidate_root: Path, *, home_dir: Path | None = None
) -> dict[str, str]:
    """Build the intentionally small environment passed to candidate tools."""

    host_path = os.environ.get("PATH") or os.defpath
    values = {
        "PATH": host_path,
        "HOME": str((home_dir or candidate_root).resolve(strict=False)),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
    }
    return {key: values[key] for key in _VERIFICATION_ENV_KEYS}


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _verification_runtime_toolchain(
    profile: VerificationProfile | None, candidate_root: Path
) -> dict[str, object]:
    path_value = os.environ.get("PATH") or os.defpath
    executables: list[dict[str, str]] = []
    commands = profile.commands if isinstance(profile, VerificationProfile) else ()
    for command in commands:
        argv0 = command.argv[0] if isinstance(command, VerificationCommand) and command.argv else ""
        workdir = (
            _verification_workdir(candidate_root, command.workdir)
            if isinstance(command, VerificationCommand)
            else None
        )
        executable = (
            _verification_executable(
                argv0,
                candidate_root=candidate_root,
                workdir=workdir,
                path_value=path_value,
            )
            if workdir is not None
            else None
        )
        if executable is None:
            executables.append(
                {"path": f"missing:{argv0}", "sha256": f"missing:{argv0}"}
            )
            continue
        try:
            executable_digest = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        except OSError:
            executable_digest = f"missing:{argv0}"
        executables.append(
            {
                "path": str(Path(executable).resolve(strict=False)),
                "sha256": executable_digest,
            }
        )
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "executables": executables,
    }
def build_verification_receipt_key(
    profile: VerificationProfile | None,
    candidate_root: Path,
    *,
    candidate_sha: str,
    contract_digest: str,
    generated_policy_digest: str,
    gate_kind: str,
    profile_name: str,
) -> VerificationReceiptKey:
    """Build the canonical meaning key for one configured verification run."""

    commands: list[dict[str, object]] = []
    if isinstance(profile, VerificationProfile):
        for command in profile.commands:
            if isinstance(command, VerificationCommand):
                commands.append(
                    {
                        "argv": list(command.argv),
                        "workdir": command.workdir.as_posix(),
                        "timeout_seconds": int(command.timeout_seconds),
                    }
                )
            else:
                commands.append({"invalid": str(command)})
    command_set_digest = _canonical_digest(commands)
    runtime_toolchain_digest = _canonical_digest(
        _verification_runtime_toolchain(
            profile, Path(candidate_root).expanduser().resolve(strict=False)
        )
    )
    executor_policy = f"hermes_repository_verifier:v1:{profile_name}"
    fields = {
        "candidate_sha": candidate_sha,
        "contract_digest": contract_digest,
        "command_set_digest": command_set_digest,
        "runtime_toolchain_digest": runtime_toolchain_digest,
        "generated_policy_digest": generated_policy_digest,
        "gate_kind": gate_kind,
        "executor_policy": executor_policy,
    }
    return VerificationReceiptKey(**fields, digest=_canonical_digest(fields))
def _verification_result_envelope(
    result: VerificationResult, *, subject_id: str | None = None
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_sha": result.source_sha,
        "candidate_sha": result.candidate_sha,
        "contract_digest": result.contract_digest,
        "profile": result.profile,
        "subject_id": result.subject_id if subject_id is None else subject_id,
        "error": result.error,
        "steps": [
            {
                "argv": list(step.argv),
                "workdir": step.workdir.as_posix(),
                "status": step.status,
                "returncode": step.returncode,
                "duration_seconds": step.duration_seconds,
                "stdout_tail": step.stdout_tail,
                "stderr_tail": step.stderr_tail,
                "error": step.error,
            }
            for step in result.steps
        ],
    }
def build_verification_receipt(
    result: VerificationResult, *, subject_id: str, created_at: int
) -> VerificationReceipt:
    if result.status != "passed":
        raise ValueError("verification receipt requires a passed result")
    if not isinstance(result.key, VerificationReceiptKey):
        raise ValueError("verification result key is missing")
    return VerificationReceipt(
        key=result.key,
        result_digest=_canonical_digest(
            _verification_result_envelope(result, subject_id=subject_id)
        ),
        created_at=int(created_at),
    )


def verification_result_payload(
    result: VerificationResult,
    *,
    scope: str,
    subject_id: str,
    created_at: int | None = None,
) -> dict[str, Any]:
    """Serialize bounded verification evidence with an exact passed receipt."""

    payload: dict[str, Any] = {
        "scope": scope,
        "subject_id": subject_id,
        "status": result.status,
        "source_sha": result.source_sha,
        "candidate_sha": result.candidate_sha,
        "contract_digest": result.contract_digest,
        "profile": result.profile,
        "error": result.error,
        "rework_eligible": result.status == "failed",
        "steps": [
            {
                "argv": list(step.argv),
                "workdir": str(step.workdir),
                "status": step.status,
                "returncode": step.returncode,
                "duration_seconds": step.duration_seconds,
                "stdout_tail": step.stdout_tail,
                "stderr_tail": step.stderr_tail,
                "error": step.error,
            }
            for step in result.steps
        ],
    }
    if result.status == "passed":
        receipt = build_verification_receipt(
            result,
            subject_id=subject_id,
            created_at=int(time.time()) if created_at is None else int(created_at),
        )
        payload["receipt"] = {
            "key": {
                "candidate_sha": receipt.key.candidate_sha,
                "contract_digest": receipt.key.contract_digest,
                "command_set_digest": receipt.key.command_set_digest,
                "runtime_toolchain_digest": receipt.key.runtime_toolchain_digest,
                "generated_policy_digest": receipt.key.generated_policy_digest,
                "gate_kind": receipt.key.gate_kind,
                "executor_policy": receipt.key.executor_policy,
                "digest": receipt.key.digest,
            },
            "result_digest": receipt.result_digest,
            "created_at": receipt.created_at,
        }
    return payload


def verification_receipt_from_payload(
    payload: Mapping[str, object],
) -> VerificationReceipt | None:
    try:
        if not isinstance(payload, Mapping) or payload.get("status") != "passed":
            return None
        if set(payload) != {
            "scope", "subject_id", "status", "source_sha", "candidate_sha",
            "contract_digest", "profile", "error", "rework_eligible", "steps", "receipt"
        }:
            return None
        receipt_payload = payload.get("receipt")
        key_payload = receipt_payload.get("key") if isinstance(receipt_payload, Mapping) else None
        if not isinstance(receipt_payload, Mapping) or not isinstance(key_payload, Mapping):
            return None
        if set(receipt_payload) != {"key", "result_digest", "created_at"} or set(key_payload) != {
            "candidate_sha", "contract_digest", "command_set_digest",
            "runtime_toolchain_digest", "generated_policy_digest", "gate_kind",
            "executor_policy", "digest"
        }:
            return None
        if (not isinstance(payload["scope"], str)
            or not isinstance(payload["subject_id"], str)
            or not isinstance(payload["source_sha"], str)
            or not isinstance(payload["candidate_sha"], str)
            or not isinstance(payload["contract_digest"], str)
            or not isinstance(payload["profile"], str)
            or (payload["error"] is not None and not isinstance(payload["error"], str))
            or not isinstance(payload["rework_eligible"], bool)):
            return None
        fields = {
            name: key_payload.get(name)
            for name in (
                "candidate_sha",
                "contract_digest",
                "command_set_digest",
                "runtime_toolchain_digest",
                "generated_policy_digest",
                "gate_kind",
                "executor_policy",
            )
        }
        if (
            not isinstance(fields["candidate_sha"], str)
            or not FULL_SHA.fullmatch(fields["candidate_sha"])
            or any(
                not isinstance(fields[name], str)
                or not FULL_DIGEST.fullmatch(fields[name])
                for name in (
                    "contract_digest",
                    "command_set_digest",
                    "runtime_toolchain_digest",
                    "generated_policy_digest",
                )
            )
            or not isinstance(fields["gate_kind"], str)
            or not isinstance(fields["executor_policy"], str)
            or not isinstance(key_payload.get("digest"), str)
            or key_payload["digest"] != _canonical_digest(fields)
            or fields["candidate_sha"] != payload.get("candidate_sha")
            or fields["contract_digest"] != payload.get("contract_digest")
            or fields["gate_kind"] != payload.get("scope")
            or fields["executor_policy"]
            != f"hermes_repository_verifier:v1:{payload.get('profile')}"
        ):
            return None
        steps = payload.get("steps")
        if not isinstance(steps, list):
            return None
        step_keys = {"argv", "workdir", "status", "returncode", "duration_seconds", "stdout_tail", "stderr_tail", "error"}
        if any(not isinstance(step, Mapping)
            or set(step) != step_keys
            or not isinstance(step["argv"], list)
            or not all(isinstance(arg, str) for arg in step["argv"])
            or not isinstance(step["workdir"], str)
            or not isinstance(step["status"], str)
            or step["status"] not in {"passed", "failed", "configuration_error", "infrastructure_error"}
            or (step["returncode"] is not None and (isinstance(step["returncode"], bool) or not isinstance(step["returncode"], int)))
            or isinstance(step["duration_seconds"], bool)
            or not isinstance(step["duration_seconds"], (int, float))
            or not isinstance(step["stdout_tail"], str)
            or not isinstance(step["stderr_tail"], str)
            or (step["error"] is not None and not isinstance(step["error"], str))
            for step in steps):
            return None
        result = VerificationResult(
            status="passed",
            source_sha=payload["source_sha"],
            candidate_sha=payload["candidate_sha"],
            contract_digest=payload["contract_digest"],
            profile=payload["profile"],
            steps=tuple(
                VerificationStepResult(
                    argv=tuple(step["argv"]),
                    workdir=PurePosixPath(step["workdir"]),
                    status=step["status"],
                    returncode=step["returncode"],
                    duration_seconds=step["duration_seconds"],
                    stdout_tail=step["stdout_tail"],
                    stderr_tail=step["stderr_tail"],
                    error=step["error"],
                )
                for step in steps
                if isinstance(step, Mapping)
            ),
            key=VerificationReceiptKey(**fields, digest=key_payload["digest"]),
            error=payload["error"],
        )
        result_digest = receipt_payload.get("result_digest")
        created_at = receipt_payload.get("created_at")
        if (
            not isinstance(result_digest, str)
            or not FULL_DIGEST.fullmatch(result_digest)
            or isinstance(created_at, bool)
            or not isinstance(created_at, int)
            or result_digest != _canonical_digest(
                _verification_result_envelope(result, subject_id=payload["subject_id"])
            )
        ):
            return None
        return VerificationReceipt(result.key, result_digest, created_at)
    except (KeyError, TypeError, ValueError):
        return None


def verification_receipt_matches(
    payload: Mapping[str, object],
    *,
    source_sha: str,
    candidate_sha: str,
    contract_digest: str,
    gate_kind: str,
    subject_id: str,
    profile_name: str,
) -> bool:
    """Whether a persisted passed receipt exactly identifies one verification."""

    receipt = verification_receipt_from_payload(payload)
    return bool(
        receipt is not None
        and payload.get("source_sha") == source_sha
        and payload.get("candidate_sha") == candidate_sha
        and payload.get("contract_digest") == contract_digest
        and payload.get("scope") == gate_kind
        and payload.get("subject_id") == subject_id
        and payload.get("profile") == profile_name
        and receipt.key.candidate_sha == candidate_sha
        and receipt.key.contract_digest == contract_digest
        and receipt.key.gate_kind == gate_kind
        and receipt.key.executor_policy
        == f"hermes_repository_verifier:v1:{profile_name}"
    )


def run_verification(
    profile: VerificationProfile | None,
    candidate_path: Path,
    *,
    source_sha: str,
    candidate_sha: str,
    contract_digest: str,
    scope: str,
    subject_id: str,
    profile_name: str | None = None,
    generated_policy_digest: str = "",
) -> VerificationResult:
    """Run an operator-configured profile in a bounded isolated candidate.

    Commands are resolved individually, run with explicit argv and
    ``shell=False``, and receive only the small deterministic environment
    needed for ordinary test/build tools.  The first non-passing step ends the
    profile; a non-zero exit is a product failure, while missing configuration
    and process/timeout errors stay distinct.
    """

    result_profile = profile_name or scope
    candidate_root = Path(candidate_path).expanduser().resolve(strict=False)
    key = build_verification_receipt_key(
        profile,
        candidate_root,
        candidate_sha=candidate_sha,
        contract_digest=contract_digest,
        generated_policy_digest=generated_policy_digest,
        gate_kind=scope,
        profile_name=result_profile,
    )
    if profile is None:
        return _verification_result(
            status="configuration_error",
            source_sha=source_sha,
            candidate_sha=candidate_sha,
            contract_digest=contract_digest,
            profile=result_profile,
            key=key,
            steps=[],
            error="missing_profile",
        )
    if not candidate_root.is_dir() or not isinstance(profile, VerificationProfile):
        return _verification_result(
            status="configuration_error",
            source_sha=source_sha,
            candidate_sha=candidate_sha,
            contract_digest=contract_digest,
            profile=result_profile,
            key=key,
            steps=[],
            error="invalid_profile_or_candidate",
        )
    if not profile.commands:
        return _verification_result(
            status="configuration_error",
            source_sha=source_sha,
            candidate_sha=candidate_sha,
            contract_digest=contract_digest,
            profile=result_profile,
            key=key,
            steps=[],
            error="empty_profile",
        )

    path_value = os.environ.get("PATH") or os.defpath
    verification_home = tempfile.TemporaryDirectory(prefix="hermes-verification-")
    environment = _verification_environment(
        candidate_root, home_dir=Path(verification_home.name)
    )
    steps: list[VerificationStepResult] = []
    for command in profile.commands:
        if not isinstance(command, VerificationCommand) or not command.argv:
            return _verification_result(
                status="configuration_error",
                source_sha=source_sha,
                candidate_sha=candidate_sha,
                contract_digest=contract_digest,
                profile=result_profile,
                key=key,
                steps=steps,
                error="invalid_command",
            )
        workdir = _verification_workdir(candidate_root, command.workdir)
        if workdir is None:
            return _verification_result(
                status="configuration_error",
                source_sha=source_sha,
                candidate_sha=candidate_sha,
                contract_digest=contract_digest,
                profile=result_profile,
                key=key,
                steps=steps,
                error="invalid_workdir",
            )
        executable = _verification_executable(
            command.argv[0],
            candidate_root=candidate_root,
            workdir=workdir,
            path_value=path_value,
        )
        if executable is None:
            return _verification_result(
                status="configuration_error",
                source_sha=source_sha,
                candidate_sha=candidate_sha,
                contract_digest=contract_digest,
                profile=result_profile,
                key=key,
                steps=steps,
                error=f"missing_executable:{command.argv[0]}",
            )

        started = time.monotonic()
        try:
            completed = subprocess.run(
                [executable, *command.argv[1:]],
                cwd=workdir,
                env=environment,
                shell=False,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            steps.append(
                VerificationStepResult(
                    argv=command.argv,
                    workdir=command.workdir,
                    status="infrastructure_error",
                    returncode=None,
                    duration_seconds=time.monotonic() - started,
                    stdout_tail=_verification_tail(getattr(exc, "stdout", None)),
                    stderr_tail=_verification_tail(getattr(exc, "stderr", None)),
                    error="timeout",
                )
            )
            return _verification_result(
                status="infrastructure_error",
                source_sha=source_sha,
                candidate_sha=candidate_sha,
                contract_digest=contract_digest,
                profile=result_profile,
                key=key,
                steps=steps,
                error="timeout",
            )
        except (OSError, subprocess.SubprocessError):
            steps.append(
                VerificationStepResult(
                    argv=command.argv,
                    workdir=command.workdir,
                    status="infrastructure_error",
                    returncode=None,
                    duration_seconds=time.monotonic() - started,
                    error="process_error",
                )
            )
            return _verification_result(
                status="infrastructure_error",
                source_sha=source_sha,
                candidate_sha=candidate_sha,
                contract_digest=contract_digest,
                profile=result_profile,
                key=key,
                steps=steps,
                error="process_error",
            )

        step_status = "passed" if completed.returncode == 0 else "failed"
        steps.append(
            VerificationStepResult(
                argv=command.argv,
                workdir=command.workdir,
                status=step_status,
                returncode=completed.returncode,
                duration_seconds=time.monotonic() - started,
                stdout_tail=_verification_tail(completed.stdout),
                stderr_tail=_verification_tail(completed.stderr),
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        )
        if completed.returncode != 0:
            return _verification_result(
                status="failed",
                source_sha=source_sha,
                candidate_sha=candidate_sha,
                contract_digest=contract_digest,
                profile=result_profile,
                key=key,
                steps=steps,
                error="nonzero_exit",
            )

    return _verification_result(
        status="passed",
        source_sha=source_sha,
        candidate_sha=candidate_sha,
        contract_digest=contract_digest,
        profile=result_profile,
        key=key,
        steps=steps,
    )


@dataclass(frozen=True)
class RepositoryContract:
    repo_root: Path
    base_ref: str
    target_branch: str
    verification: Mapping[str, VerificationProfile]
    generated_paths: tuple[PurePosixPath, ...]
    generated_policy_digest: str
    ci_workflows: tuple[str, ...]
    digest: str


class EvidenceWorkspaceError(RuntimeError):
    """A Test/Review workspace crossed the dispatcher-owned source boundary."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class EvidenceWorkspaceResult:
    """Observed Git state for one pinned Test/Review evidence workspace."""

    branch: str
    branch_head: str
    tracked: tuple[str, ...] = ()
    undeclared_tracked: tuple[str, ...] = ()
    declared_generated: tuple[PurePosixPath, ...] = ()
    untracked: tuple[str, ...] = ()

    @property
    def tracked_paths(self) -> tuple[str, ...]:
        """Compatibility alias for callers that use the explicit name."""

        return self.tracked

    @property
    def untracked_paths(self) -> tuple[str, ...]:
        """Compatibility alias for callers that use the explicit name."""

        return self.untracked

    @property
    def declared_generated_paths(self) -> tuple[PurePosixPath, ...]:
        """Compatibility alias for callers that use the explicit name."""

        return self.declared_generated


@dataclass(frozen=True)
class TargetHeadsObservation:
    """Read-only local and remote target-head observation for a handoff recheck.

    ``local_head`` is ``None`` when the local branch cannot be resolved.
    ``remote_head`` is ``None`` when the remote is unreachable or the
    target branch does not exist on it.  ``remote_available`` is false
    only when ``git ls-remote`` itself failed; a branch that simply
    does not exist on the remote sets ``remote_head=None`` and
    ``remote_available=True``.
    """

    local_head: str | None
    remote_head: str | None
    remote_name: str
    remote_available: bool


def _evidence_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one bounded, read-only or explicit-path Git operation."""

    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceWorkspaceError("git_error", str(exc)) from exc


def _remote_observe_git(
    repo_root: Path, *args: str
) -> subprocess.CompletedProcess[str] | None:
    """Run one bounded, shell-free, strictly read-only Git observation.

    This seam exists so handoff tests can substitute a fake transport and
    prove that the observation path never issues a remote-write verb.
    The only commands routed through it are ``rev-parse`` and
    ``ls-remote``.  Returns ``None`` instead of raising so a failing
    observation reads as "unavailable" rather than crashing the handoff.
    """

    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _evidence_paths(value: object) -> tuple[PurePosixPath, ...]:
    if value is None:
        return ()
    try:
        raw_paths = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise EvidenceWorkspaceError("invalid_generated_path") from exc
    normalized: list[PurePosixPath] = []
    for raw_path in raw_paths:
        path = raw_path if isinstance(raw_path, PurePosixPath) else PurePosixPath(str(raw_path))
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise EvidenceWorkspaceError("invalid_generated_path", str(path))
        normalized.append(path)
    if len(set(normalized)) != len(normalized):
        raise EvidenceWorkspaceError("invalid_generated_path", "duplicate path")
    return tuple(normalized)


def _evidence_status_paths(output: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tracked: set[str] = set()
    untracked: set[str] = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        raw_path = line[3:]
        paths = (
            tuple(part.strip() for part in raw_path.split(" -> ", 1))
            if " -> " in raw_path and status[0] in {"R", "C"}
            else (raw_path,)
        )
        target = untracked if status == "??" else tracked
        target.update(path for path in paths if path)
    return tuple(sorted(tracked)), tuple(sorted(untracked))


def inspect_evidence_workspace(
    workspace: Path | str,
    pinned_sha: str,
    generated_paths: Iterable[PurePosixPath | str] = (),
) -> EvidenceWorkspaceResult:
    """Inspect a pinned evidence checkout without changing its source files.

    Gitignored output is intentionally absent from porcelain status and is
    therefore allowed.  Every non-ignored untracked path remains visible as a
    rejection signal; the caller decides whether to preserve it for diagnosis.
    """

    actual = Path(workspace).expanduser().resolve(strict=False)
    if not actual.is_dir():
        raise EvidenceWorkspaceError("workspace_unavailable", str(actual))
    if not FULL_SHA.fullmatch(str(pinned_sha or "")):
        raise EvidenceWorkspaceError("invalid_pinned_sha")
    branch_result = _evidence_git(actual, "branch", "--show-current")
    head_result = _evidence_git(actual, "rev-parse", "--verify", "HEAD^{commit}")
    status_result = _evidence_git(
        actual,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if branch_result.returncode or head_result.returncode or status_result.returncode:
        detail = (
            head_result.stderr
            or branch_result.stderr
            or status_result.stderr
            or "git inspection failed"
        ).strip()
        raise EvidenceWorkspaceError("git_error", detail[:300])
    tracked, untracked = _evidence_status_paths(status_result.stdout or "")
    allowed = _evidence_paths(generated_paths)
    allowed_names = {path.as_posix() for path in allowed}
    declared = tuple(
        PurePosixPath(path)
        for path in tracked
        if path in allowed_names
    )
    undeclared = tuple(path for path in tracked if path not in allowed_names)
    return EvidenceWorkspaceResult(
        branch=(branch_result.stdout or "").strip(),
        branch_head=(head_result.stdout or "").strip(),
        tracked=tracked,
        undeclared_tracked=undeclared,
        declared_generated=declared,
        untracked=untracked,
    )


def restore_generated_paths(
    workspace: Path | str,
    pinned_sha: str,
    generated_paths: Iterable[PurePosixPath | str],
) -> None:
    """Restore only validated generated paths to one exact pinned commit.

    The explicit pathspec terminator is intentional: this helper never resets,
    cleans, stashes, or accepts an arbitrary directory from an evidence worker.
    """

    actual = Path(workspace).expanduser().resolve(strict=False)
    if not actual.is_dir():
        raise EvidenceWorkspaceError("workspace_unavailable", str(actual))
    if not FULL_SHA.fullmatch(str(pinned_sha or "")):
        raise EvidenceWorkspaceError("invalid_pinned_sha")
    paths = _evidence_paths(generated_paths)
    if not paths:
        return
    for path in paths:
        listed = _evidence_git(
            actual,
            "ls-tree",
            "-r",
            "--name-only",
            pinned_sha,
            "--",
            path.as_posix(),
        )
        if listed.returncode != 0 or path.as_posix() not in {
            line.strip() for line in (listed.stdout or "").splitlines()
        }:
            raise EvidenceWorkspaceError("invalid_generated_path", path.as_posix())
    restored = _evidence_git(
        actual,
        "restore",
        "--source",
        pinned_sha,
        "--staged",
        "--worktree",
        "--",
        *(path.as_posix() for path in paths),
    )
    if restored.returncode != 0:
        detail = (restored.stderr or restored.stdout or "restore failed").strip()
        raise EvidenceWorkspaceError("restore_failed", detail[:300])


@dataclass(frozen=True)
class RefreshRequest:
    """Pinned inputs for one dispatcher-owned story refresh attempt."""

    repo_root: Path
    story_id: str
    story_branch: str
    story_worktree: Path
    story_sha: str
    epic_branch: str
    epic_tip_sha: str


@dataclass(frozen=True)
class RefreshResult:
    """Typed result of an isolated story refresh attempt.

    ``conflict_worktree`` is intentionally retained for a conflict.  It is a
    disposable detached checkout, never the user's story worktree, and gives a
    later Development worker the exact files that need attention.
    """

    kind: str
    before_sha: str | None = None
    after_sha: str | None = None
    current_sha: str | None = None
    current_epic_tip_sha: str | None = None
    dirty_paths: tuple[str, ...] = ()
    conflict_paths: tuple[str, ...] = ()
    conflict_worktree: Path | None = None
    error: str | None = None
    story_id: str | None = None
    story_branch: str | None = None
    story_sha: str | None = None
    epic_branch: str | None = None
    epic_tip_sha: str | None = None

    @property
    def retained_worktree(self) -> Path | None:
        """Compatibility name for the retained conflict evidence checkout."""

        return self.conflict_worktree


def _error(code: str, detail: str | None = None) -> RepositoryConfigurationError:
    return RepositoryConfigurationError(code, detail)


def _require_mapping(value: Any, code: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(code, detail)
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: frozenset[str]
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _error("unknown_key", ", ".join(unknown))


def _require_string(value: Any, code: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(code, field)
    if "\x00" in value or any(char.isspace() for char in value):
        raise _error(code, field)
    return value


def _validate_ref(value: Any, code: str, field: str) -> str:
    ref = _require_string(value, code, field)
    # Refs are passed as one argv item, but revision expressions and malformed
    # ref names would make the configured policy branch-dependent or ambiguous.
    if (
        ref.startswith("-")
        or ref.startswith("/")
        or ref.endswith(("/", "."))
        or "//" in ref
        or ".." in ref
        or "@{" in ref
        or any(char in ref for char in "~^:?*[\\")
    ):
        raise _error(code, field)
    return ref


def _normalize_relative_path(
    value: Any, *, code: str, field: str, allow_dot: bool
) -> PurePosixPath:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(code, field)
    if "\x00" in value or "\\" in value:
        raise _error(code, field)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise _error(code, field)
    if not allow_dot and not path.parts:
        raise _error(code, field)
    return path


def _ensure_inside(root: Path, relative: PurePosixPath, *, code: str) -> Path:
    candidate = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise _error(code, str(relative)) from exc
    return candidate


def _validate_globs(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error("malformed_boundary_evidence", field)
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise _error("malformed_boundary_evidence", field)
        if "\x00" in item or "\\" in item:
            raise _error("malformed_boundary_evidence", field)
        pattern = PurePosixPath(item)
        if pattern.is_absolute() or ".." in pattern.parts:
            raise _error("invalid_path", item)
        normalized.append(item)
    return tuple(normalized)


def _validate_workdir(repo_root: Path, value: Any) -> PurePosixPath:
    workdir = _normalize_relative_path(
        value, code="invalid_workdir", field="workdir", allow_dot=True
    )
    resolved = _ensure_inside(repo_root, workdir, code="invalid_workdir")
    if not resolved.is_dir():
        raise _error("invalid_workdir", str(workdir))
    return workdir


def _validate_command(
    repo_root: Path, value: Any
) -> tuple[VerificationCommand, dict[str, Any]]:
    command = _require_mapping(value, "malformed_command", "command")
    _reject_unknown_keys(command, _VERIFICATION_COMMAND_KEYS)
    if set(command) != _VERIFICATION_COMMAND_KEYS:
        raise _error("malformed_command", "argv, workdir, timeout_seconds are required")

    argv_value = command["argv"]
    if (
        not isinstance(argv_value, list)
        or not argv_value
        or any(
            not isinstance(arg, str) or not arg or "\x00" in arg
            for arg in argv_value
        )
    ):
        raise _error("malformed_command", "argv")
    argv = tuple(argv_value)

    workdir = _validate_workdir(repo_root, command["workdir"])
    timeout = command["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise _error("invalid_timeout", "timeout_seconds")

    normalized = {
        "argv": list(argv),
        "workdir": workdir.as_posix(),
        "timeout_seconds": timeout,
    }
    return VerificationCommand(argv, workdir, timeout), normalized


def _validate_profile(
    repo_root: Path, value: Any
) -> tuple[VerificationProfile, list[dict[str, Any]]]:
    # The compact list form is accepted for board metadata written by the
    # first v2 prototype.  The object form is canonical and makes the profile
    # schema self-describing.
    if isinstance(value, list):
        commands_value = value
    else:
        profile = _require_mapping(value, "malformed_profile", "profile")
        _reject_unknown_keys(profile, _VERIFICATION_PROFILE_KEYS)
        if set(profile) != _VERIFICATION_PROFILE_KEYS:
            raise _error("malformed_profile", "commands")
        commands_value = profile["commands"]

    if not isinstance(commands_value, list) or not commands_value:
        raise _error("malformed_profile", "commands")

    commands: list[VerificationCommand] = []
    normalized: list[dict[str, Any]] = []
    for raw_command in commands_value:
        command, command_json = _validate_command(repo_root, raw_command)
        commands.append(command)
        normalized.append(command_json)
    return VerificationProfile(tuple(commands)), normalized


def _canonical_contract_json(
    *,
    base_ref: str,
    target_branch: str,
    verification_json: Mapping[str, list[dict[str, Any]]],
    ci_provider: str,
    ci_workflows: tuple[str, ...],
    test_globs: tuple[str, ...],
    fixture_globs: tuple[str, ...],
    generated_paths: tuple[PurePosixPath, ...],
) -> dict[str, Any]:
    return {
        "base_ref": base_ref,
        "target_branch": target_branch,
        "verification_profiles": {
            name: {"commands": verification_json[name]}
            for name in sorted(verification_json)
        },
        "ci_observation": {
            "provider": ci_provider,
            "required_workflows": list(ci_workflows),
        },
        "boundary_evidence": {
            "test_globs": list(test_globs),
            "fixture_globs": list(fixture_globs),
            "generated_paths": [path.as_posix() for path in generated_paths],
        },
    }


def load_repository_contract(
    board_metadata: Mapping[str, object], *, repo_root: Path
) -> RepositoryContract:
    """Validate and normalize ``board_metadata['repository']``.

    The returned paths are repository-relative POSIX paths.  The repository
    root itself is resolved once, and every generated path is checked against
    the tracked index so a later evidence phase cannot authorize an arbitrary
    filesystem path.
    """

    metadata = _require_mapping(
        board_metadata, "malformed_repository", "board_metadata"
    )
    if "repository" not in metadata:
        raise _error("missing_repository", "repository")
    repository = _require_mapping(
        metadata["repository"], "malformed_repository", "repository"
    )
    _reject_unknown_keys(repository, _REPOSITORY_KEYS)
    if set(repository) != _REPOSITORY_KEYS:
        missing = _REPOSITORY_KEYS - set(repository)
        if "base_ref" in missing:
            raise _error("missing_base_ref", "base_ref")
        if "target_branch" in missing:
            raise _error("missing_target_branch", "target_branch")
        raise _error("malformed_repository", "required repository fields")

    root = Path(repo_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise _error("invalid_repo_root", str(root))

    base_ref = _validate_ref(repository["base_ref"], "malformed_base_ref", "base_ref")
    target_branch = _validate_ref(
        repository["target_branch"], "malformed_target_branch", "target_branch"
    )
    if target_branch.startswith("refs/"):
        raise _error("malformed_target_branch", "target_branch")

    profiles_value = repository["verification_profiles"]
    profiles_mapping = _require_mapping(
        profiles_value, "malformed_profiles", "verification_profiles"
    )
    if not profiles_mapping:
        raise _error("malformed_profiles", "verification_profiles")

    verification: dict[str, VerificationProfile] = {}
    verification_json: dict[str, list[dict[str, Any]]] = {}
    for name, raw_profile in profiles_mapping.items():
        if not isinstance(name, str) or not name or name != name.strip():
            raise _error("malformed_profiles", "profile name")
        profile, profile_json = _validate_profile(root, raw_profile)
        verification[name] = profile
        verification_json[name] = profile_json

    ci_observation = _require_mapping(
        repository["ci_observation"], "malformed_ci_observation", "ci_observation"
    )
    _reject_unknown_keys(ci_observation, _CI_OBSERVATION_KEYS)
    if "required_workflows" not in ci_observation:
        raise _error("missing_ci_workflows", "required_workflows")
    ci_provider = ""
    if "provider" in ci_observation:
        ci_provider = _require_string(
            ci_observation["provider"], "malformed_ci_observation", "provider"
        )
    workflows_value = ci_observation["required_workflows"]
    if not isinstance(workflows_value, list) or not workflows_value:
        raise _error("missing_ci_workflows", "required_workflows")
    if any(
        not isinstance(workflow, str)
        or not workflow
        or workflow != workflow.strip()
        or "\x00" in workflow
        for workflow in workflows_value
    ):
        raise _error("malformed_ci_workflows", "required_workflows")
    ci_workflows = tuple(workflows_value)
    if len(set(ci_workflows)) != len(ci_workflows):
        raise _error("malformed_ci_workflows", "duplicate workflow")

    boundary = _require_mapping(
        repository["boundary_evidence"], "malformed_boundary_evidence", "boundary_evidence"
    )
    _reject_unknown_keys(boundary, _BOUNDARY_EVIDENCE_KEYS)
    if set(boundary) != _BOUNDARY_EVIDENCE_KEYS:
        raise _error(
            "malformed_boundary_evidence",
            "test_globs, fixture_globs, generated_paths",
        )
    test_globs = _validate_globs(boundary["test_globs"], "test_globs")
    fixture_globs = _validate_globs(boundary["fixture_globs"], "fixture_globs")

    generated_value = boundary["generated_paths"]
    if not isinstance(generated_value, list):
        raise _error("malformed_boundary_evidence", "generated_paths")
    generated_paths: list[PurePosixPath] = []
    for raw_path in generated_value:
        path = _normalize_relative_path(
            raw_path,
            code="invalid_path",
            field="generated_paths",
            allow_dot=False,
        )
        _ensure_inside(root, path, code="invalid_path")
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--error-unmatch",
                "--",
                path.as_posix(),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise _error("untracked_path", path.as_posix())
        generated_paths.append(path)
    normalized_generated_paths = tuple(generated_paths)
    if len(set(normalized_generated_paths)) != len(normalized_generated_paths):
        raise _error("malformed_boundary_evidence", "duplicate generated path")

    canonical = _canonical_contract_json(
        base_ref=base_ref,
        target_branch=target_branch,
        verification_json=verification_json,
        ci_provider=ci_provider,
        ci_workflows=ci_workflows,
        test_globs=test_globs,
        fixture_globs=fixture_globs,
        generated_paths=normalized_generated_paths,
    )
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    generated_policy_digest = _canonical_digest(
        [path.as_posix() for path in normalized_generated_paths]
    )

    return RepositoryContract(
        repo_root=root,
        base_ref=base_ref,
        target_branch=target_branch,
        verification=MappingProxyType(verification),
        generated_paths=normalized_generated_paths,
        generated_policy_digest=generated_policy_digest,
        ci_workflows=ci_workflows,
        digest=digest,
    )


def resolve_commit(repo_root: Path, ref: str) -> str:
    """Resolve a configured ref to one full commit SHA.

    Ambiguous short names are rejected even when Git happens to return a
    result, because accepting them would make a board depend on local ref
    layout.  No shell is involved and no ref is written.
    """

    if not isinstance(ref, str) or not ref or ref != ref.strip() or "\x00" in ref:
        raise _error("missing_ref", "invalid ref")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(Path(repo_root).expanduser().resolve(strict=False)),
            "rev-parse",
            "--verify",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = completed.stdout.strip()
    if (
        completed.returncode != 0
        or "ambiguous" in completed.stderr.lower()
        or not FULL_SHA.fullmatch(sha)
    ):
        raise _error("missing_ref", ref)
    return sha


def commit_contains(
    repo_root: Path,
    descendant_sha: str,
    ancestor_sha: str,
) -> bool:
    """Return whether one exact commit contains another, including equality."""

    for field, value in (
        ("descendant_sha", descendant_sha),
        ("ancestor_sha", ancestor_sha),
    ):
        if not isinstance(value, str) or FULL_SHA.fullmatch(value) is None:
            raise RepositoryConfigurationError(f"malformed_{field}")
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(Path(repo_root).expanduser().resolve(strict=False)),
                "merge-base",
                "--is-ancestor",
                ancestor_sha,
                descendant_sha,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryConfigurationError("ancestry_check_failed") from exc
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise RepositoryConfigurationError("ancestry_check_failed")


def _prepared_ref_git(
    repo_root: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run one bounded, shell-free Git command for prepared-ref CAS."""

    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _prepared_ref_sha(repo_root: Path, ref: str) -> str | None:
    completed = _prepared_ref_git(
        repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}"
    )
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and FULL_SHA.fullmatch(value) else None


def _prepared_target_is_checked_out(repo_root: Path, target_ref: str) -> bool:
    listed = _prepared_ref_git(repo_root, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        raise RepositoryConfigurationError("worktree_list_failed")
    for block in (listed.stdout or "").strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if fields.get("branch") == target_ref and fields.get("worktree"):
            return True
    return False


def advance_prepared_candidate_ref(
    repo_root: Path,
    *,
    target_ref: str,
    candidate_ref: str,
    pre_sha: str,
    candidate_sha: str,
) -> PreparedRefCASResult:
    """Advance one exact unchecked target ref by one preimage-protected CAS.

    The retained candidate ref is validated before any target operation.  A
    target already at the exact candidate is a reflected successful CAS; every
    other preimage mismatch is typed as target movement.  Checked-out targets
    are always refused, even when their worktree is clean.  This function has
    no merge/read-tree/reset/clean/stash path and never deletes the retained
    candidate ref.
    """

    root = Path(repo_root).expanduser().resolve(strict=False)
    if (
        not isinstance(target_ref, str)
        or not target_ref.startswith("refs/heads/")
        or target_ref != target_ref.strip()
        or "\x00" in target_ref
    ):
        raise RepositoryConfigurationError("malformed_target_ref")
    if (
        not isinstance(candidate_ref, str)
        or not candidate_ref.startswith("refs/hermes/integration-candidates/")
        or candidate_ref != candidate_ref.strip()
        or "\x00" in candidate_ref
    ):
        raise RepositoryConfigurationError("malformed_candidate_ref")
    if not isinstance(pre_sha, str) or FULL_SHA.fullmatch(pre_sha) is None:
        raise RepositoryConfigurationError("malformed_pre_sha")
    if not isinstance(candidate_sha, str) or FULL_SHA.fullmatch(candidate_sha) is None:
        raise RepositoryConfigurationError("malformed_candidate_sha")

    retained_sha = _prepared_ref_sha(root, candidate_ref)
    if retained_sha != candidate_sha:
        raise RepositoryConfigurationError("candidate_ref_mismatch")
    current_sha = _prepared_ref_sha(root, target_ref)
    if current_sha == candidate_sha:
        return PreparedRefCASResult("reflected", current_sha)
    if current_sha != pre_sha:
        return PreparedRefCASResult("target_moved", current_sha)
    if _prepared_target_is_checked_out(root, target_ref):
        return PreparedRefCASResult("checked_out", current_sha)

    applied = _prepared_ref_git(
        root, "update-ref", target_ref, candidate_sha, pre_sha
    )
    reflected_sha = _prepared_ref_sha(root, target_ref)
    if applied.returncode == 0 and reflected_sha == candidate_sha:
        return PreparedRefCASResult("advanced", reflected_sha)
    if reflected_sha == candidate_sha:
        return PreparedRefCASResult("reflected", reflected_sha)
    return PreparedRefCASResult("target_moved", reflected_sha)


def inspect_prepared_candidate_ref(
    repo_root: Path,
    *,
    target_ref: str,
    pre_sha: str,
    candidate_sha: str,
) -> PreparedRefRecoveryResult:
    """Classify the four deterministic recovery states of a prepared CAS."""

    root = Path(repo_root).expanduser().resolve(strict=False)
    if (
        not isinstance(target_ref, str)
        or not target_ref.startswith("refs/heads/")
        or target_ref != target_ref.strip()
        or "\x00" in target_ref
    ):
        raise RepositoryConfigurationError("malformed_target_ref")
    if not isinstance(pre_sha, str) or FULL_SHA.fullmatch(pre_sha) is None:
        raise RepositoryConfigurationError("malformed_pre_sha")
    if not isinstance(candidate_sha, str) or FULL_SHA.fullmatch(candidate_sha) is None:
        raise RepositoryConfigurationError("malformed_candidate_sha")

    current_sha = _prepared_ref_sha(root, target_ref)
    if current_sha == pre_sha:
        return PreparedRefRecoveryResult("preimage", current_sha)
    if current_sha == candidate_sha:
        return PreparedRefRecoveryResult("candidate", current_sha)
    if current_sha is None:
        return PreparedRefRecoveryResult("diverged", None)
    ancestor = _prepared_ref_git(
        root, "merge-base", "--is-ancestor", candidate_sha, current_sha
    )
    if ancestor.returncode == 0:
        return PreparedRefRecoveryResult("descendant", current_sha)
    if ancestor.returncode == 1:
        return PreparedRefRecoveryResult("diverged", current_sha)
    raise RepositoryConfigurationError("ancestry_check_failed")


def delete_prepared_candidate_ref(
    repo_root: Path,
    *,
    candidate_ref: str,
    candidate_sha: str,
) -> bool:
    """Delete only the retained candidate ref that still pins the exact SHA."""

    root = Path(repo_root).expanduser().resolve(strict=False)
    if (
        not isinstance(candidate_ref, str)
        or not candidate_ref.startswith("refs/hermes/integration-candidates/")
        or candidate_ref != candidate_ref.strip()
        or "\x00" in candidate_ref
    ):
        raise RepositoryConfigurationError("malformed_candidate_ref")
    if not isinstance(candidate_sha, str) or FULL_SHA.fullmatch(candidate_sha) is None:
        raise RepositoryConfigurationError("malformed_candidate_sha")

    retained_sha = _prepared_ref_sha(root, candidate_ref)
    if retained_sha is None:
        return True
    if retained_sha != candidate_sha:
        return False
    deleted = _prepared_ref_git(
        root, "update-ref", "-d", candidate_ref, candidate_sha
    )
    return deleted.returncode == 0 and _prepared_ref_sha(root, candidate_ref) is None


def validate_release_candidate_ref(candidate_ref: str) -> str:
    """Validate the release-only retained-candidate namespace."""

    if (
        not isinstance(candidate_ref, str)
        or not candidate_ref.startswith(RELEASE_CANDIDATE_REF_PREFIX)
        or candidate_ref != candidate_ref.strip()
        or "\x00" in candidate_ref
        or candidate_ref == RELEASE_CANDIDATE_REF_PREFIX
    ):
        raise RepositoryConfigurationError("malformed_release_candidate_ref")
    return candidate_ref


def delete_release_candidate_ref(
    repo_root: Path,
    *,
    candidate_ref: str,
    candidate_sha: str,
) -> bool:
    """Delete a release candidate only when the exact old SHA still matches."""

    root = Path(repo_root).expanduser().resolve(strict=False)
    validate_release_candidate_ref(candidate_ref)
    if not isinstance(candidate_sha, str) or FULL_SHA.fullmatch(candidate_sha) is None:
        raise RepositoryConfigurationError("malformed_candidate_sha")

    retained_sha = _prepared_ref_sha(root, candidate_ref)
    if retained_sha is None:
        return True
    if retained_sha != candidate_sha:
        return False
    deleted = _prepared_ref_git(
        root, "update-ref", "-d", candidate_ref, candidate_sha
    )
    return deleted.returncode == 0 and _prepared_ref_sha(root, candidate_ref) is None


def observe_target_heads(
    repo_root: Path, *, target_branch: str, base_ref: str
) -> TargetHeadsObservation:
    """Observe the exact local and remote heads of the release target branch.

    Local head:  ``git rev-parse --verify refs/heads/<target>^{commit}``.
    Remote head: ``git ls-remote <remote> refs/heads/<target>`` — strictly
    read-only; no fetch, no remote-write verb, and no local ref update.
    The remote name is derived from the configured ``base_ref``
    (``refs/remotes/<remote>/<branch>``) so no extra configuration key is
    needed.  Both results are reported even when unavailable so the caller
    can truthfully refuse rather than guess.
    """

    root = Path(repo_root).expanduser().resolve(strict=False)
    if (
        not isinstance(target_branch, str)
        or not target_branch
        or target_branch != target_branch.strip()
        or "\x00" in target_branch
    ):
        raise _error("malformed_target_branch")
    if (
        not isinstance(base_ref, str)
        or not base_ref.startswith("refs/remotes/")
        or base_ref != base_ref.strip()
        or "\x00" in base_ref
    ):
        raise _error("malformed_base_ref")
    remote_path = base_ref[len("refs/remotes/") :]
    if (
        not remote_path
        or "/" not in remote_path
        or remote_path.split("/", 1)[0] in {"", "."}
    ):
        raise _error("malformed_base_ref")

    remote_name = remote_path.split("/", 1)[0]

    local_head: str | None = None
    local = _remote_observe_git(
        root, "rev-parse", "--verify", f"refs/heads/{target_branch}^{{commit}}"
    )
    if local is not None:
        value = (local.stdout or "").strip()
        if local.returncode == 0 and FULL_SHA.fullmatch(value):
            local_head = value

    remote_head: str | None = None
    remote_available = False
    remote = _remote_observe_git(
        root, "ls-remote", "--heads", remote_name, f"refs/heads/{target_branch}"
    )
    if remote is None:
        remote_available = False
    elif remote.returncode != 0:
        remote_available = False
    else:
        remote_available = True
        for line in (remote.stdout or "").splitlines():
            parts = line.split()
            if parts and FULL_SHA.fullmatch(parts[0]):
                remote_head = parts[0]
                break

    return TargetHeadsObservation(
        local_head=local_head,
        remote_head=remote_head,
        remote_name=remote_name,
        remote_available=remote_available,
    )


def _http_observe_get(url: str, *, timeout: int = 30) -> dict[str, object] | None:
    """Run one bounded, strictly read-only HTTP GET observation.

    This seam exists so CI-observation tests can substitute a fake
    transport and prove that the observation path never issues a write
    verb (GET only, no POST/PATCH/DELETE/PUT).  Returns the parsed JSON
    dict or ``None`` on any failure (timeout, non-200, malformed JSON,
    network error) — callers treat ``None`` as "unavailable".
    """

    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError, LookupError):
        return None
    if not isinstance(data, dict):
        return None
    return cast(dict[str, object], data)


def observe_ci_workflow_runs(
    repo_root: Path,
    *,
    base_ref: str,
    workflows: tuple[str, ...],
    head_sha: str,
) -> dict[str, str | None] | None:
    """Observe the latest GitHub Actions run conclusion for each required
    workflow at an exact head SHA.

    The GitHub repository owner and name are derived from the configured
    remote URL via ``git remote get-url`` — strictly read-only; no remote
    write verb is ever issued.  Each workflow name is matched against the
    ``workflow_runs`` returned by the GET to
    ``/repos/{owner}/{repo}/actions/runs?head_sha=<sha>`` and the latest
    run's ``conclusion`` is returned (``success``, ``failure``,
    ``cancelled``, ``timed_out``, ``None`` for queued/in_progress, or
    the key is absent when no run exists for that workflow).

    Returns ``None`` (not a dict) when the remote URL or CI provider
    cannot be reached — callers treat this as "unavailable".
    """

    root = Path(repo_root).expanduser().resolve(strict=False)
    if not isinstance(head_sha, str) or FULL_SHA.fullmatch(head_sha) is None:
        raise _error("malformed_head_sha")
    if (
        not isinstance(base_ref, str)
        or not base_ref.startswith("refs/remotes/")
        or base_ref != base_ref.strip()
        or "\x00" in base_ref
    ):
        raise _error("malformed_base_ref")
    remote_path = base_ref[len("refs/remotes/") :]
    if (
        not remote_path
        or "/" not in remote_path
        or remote_path.split("/", 1)[0] in {"", "."}
    ):
        raise _error("malformed_base_ref")

    remote_name = remote_path.split("/", 1)[0]
    remote_url: str | None = None
    result = _remote_observe_git(root, "remote", "get-url", remote_name)
    if result is not None and result.returncode == 0:
        url_text = (result.stdout or "").strip()
        if url_text:
            remote_url = url_text

    if remote_url is None:
        return None

    owner: str | None = None
    repo: str | None = None
    # Parse GitHub URLs: git@github.com:owner/repo.git or https://github.com/owner/repo
    ssh_match = re.match(r"^git@github\.com:([^/]+)/([^/\s]+?)(\.git)?$", remote_url)
    https_match = re.match(
        r"^https://github\.com/([^/]+)/([^/\s]+?)(\.git)?$", remote_url
    )
    if ssh_match:
        owner = ssh_match.group(1)
        repo = ssh_match.group(2)
    elif https_match:
        owner = https_match.group(1)
        repo = https_match.group(2)

    if not owner or not repo:
        return None

    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
        f"?head_sha={head_sha}&per_page=100"
    )
    data = _http_observe_get(api_url)
    if data is None:
        return None

    workflow_runs = data.get("workflow_runs")
    if not isinstance(workflow_runs, list):
        return None

    conclusions: dict[str, str | None] = {}
    for workflow in workflows:
        if not workflow or not isinstance(workflow, str):
            continue
        # The API returns runs newest-first, so the first name match is the
        # latest run for that workflow.  An absent workflow keeps ``None``
        # (treated by callers as queued/no-run, i.e. still pending).
        latest: dict[str, object] | None = None
        for run in workflow_runs:
            if isinstance(run, dict) and run.get("name") == workflow:
                latest = run
                break
        if latest is None:
            conclusions[workflow] = None
        else:
            conclusion = latest.get("conclusion")
            conclusions[workflow] = (
                str(conclusion) if isinstance(conclusion, str) else None
            )

    return conclusions


def _refresh_git(
    path: Path, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git operation without invoking a shell."""

    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=check,
    )


def _refresh_sha(path: Path, ref: str) -> str | None:
    completed = _refresh_git(path, "rev-parse", "--verify", f"{ref}^{{commit}}")
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and FULL_SHA.fullmatch(value) else None


def _refresh_status_paths(path: Path) -> tuple[str, ...] | None:
    completed = _refresh_git(
        path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if completed.returncode != 0:
        return None
    paths: list[str] = []
    for line in (completed.stdout or "").splitlines():
        if len(line) >= 4:
            paths.append(line[3:])
    return tuple(paths)


def _refresh_conflict_paths(path: Path) -> tuple[str, ...]:
    completed = _refresh_git(path, "diff", "--name-only", "--diff-filter=U")
    return tuple(
        line.strip()
        for line in (completed.stdout or "").splitlines()
        if line.strip()
    )


def _remove_refresh_worktree(repo_root: Path, worktree: Path, parent: Path) -> None:
    """Best-effort cleanup for a successful or failed disposable checkout."""

    try:
        _refresh_git(repo_root, "worktree", "remove", "--force", str(worktree))
    except (OSError, subprocess.SubprocessError):
        pass
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
        except OSError:
            pass
    try:
        parent.rmdir()
    except OSError:
        pass


def _refresh_story_branch(request: RefreshRequest) -> RefreshResult:
    """Refresh a clean story branch from a pinned Epic tip in isolation.

    The caller pins both source refs immediately before dispatch.  This
    function rechecks both refs, refuses dirty user work, builds the merge in a
    detached disposable worktree, and only then advances the story ref with
    ``git update-ref <new> <old>``.  A conflict leaves that disposable worktree
    in place as evidence; the original story checkout and branch are untouched.
    """

    repo_root = Path(request.repo_root).expanduser().resolve(strict=False)
    story_worktree = Path(request.story_worktree).expanduser().resolve(strict=False)
    before_sha = str(request.story_sha or "").strip()
    pinned_epic_sha = str(request.epic_tip_sha or "").strip()
    if not FULL_SHA.fullmatch(before_sha):
        return RefreshResult("error", error="invalid_story_sha")
    if not FULL_SHA.fullmatch(pinned_epic_sha):
        return RefreshResult("error", error="invalid_epic_tip_sha")

    root = _refresh_git(story_worktree, "rev-parse", "--show-toplevel")
    resolved_root = (root.stdout or "").strip()
    if root.returncode != 0 or not resolved_root:
        return RefreshResult("error", before_sha=before_sha, error="story_worktree_not_git")
    if Path(resolved_root).expanduser().resolve(strict=False) != repo_root:
        return RefreshResult("error", before_sha=before_sha, error="repository_mismatch")

    current_story_sha = _refresh_sha(
        repo_root, f"refs/heads/{request.story_branch}"
    )
    current_epic_sha = _refresh_sha(repo_root, f"refs/heads/{request.epic_branch}")
    if current_story_sha is None or current_epic_sha is None:
        return RefreshResult("error", before_sha=before_sha, error="source_ref_missing")
    if current_story_sha != before_sha or current_epic_sha != pinned_epic_sha:
        return RefreshResult(
            "source_moved",
            before_sha=before_sha,
            current_sha=current_story_sha,
            current_epic_tip_sha=current_epic_sha,
        )

    dirty_paths = _refresh_status_paths(story_worktree)
    if dirty_paths is None:
        return RefreshResult("error", before_sha=before_sha, error="status_failed")
    if dirty_paths:
        return RefreshResult(
            "dirty",
            before_sha=before_sha,
            current_sha=current_story_sha,
            dirty_paths=dirty_paths,
        )

    ancestry = _refresh_git(
        repo_root,
        "merge-base",
        "--is-ancestor",
        pinned_epic_sha,
        before_sha,
    )
    if ancestry.returncode == 0:
        return RefreshResult("unchanged", before_sha=before_sha, after_sha=before_sha)
    if ancestry.returncode != 1:
        return RefreshResult("error", before_sha=before_sha, error="ancestry_check_failed")

    parent = Path(tempfile.mkdtemp(prefix="hermes-story-refresh-"))
    candidate = parent / f"candidate-{uuid.uuid4().hex[:12]}"
    try:
        parent.rmdir()
    except OSError:
        pass
    added = _refresh_git(
        repo_root,
        "worktree",
        "add",
        "--detach",
        str(candidate),
        before_sha,
    )
    if added.returncode != 0:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="candidate_create_failed")

    merged = _refresh_git(candidate, "merge", "--no-ff", "--no-edit", pinned_epic_sha)
    if merged.returncode != 0:
        conflict_paths = _refresh_conflict_paths(candidate)
        if conflict_paths:
            return RefreshResult(
                "conflict",
                before_sha=before_sha,
                conflict_paths=conflict_paths,
                conflict_worktree=candidate,
            )
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="candidate_merge_failed")

    candidate_sha = _refresh_sha(candidate, "HEAD")
    if candidate_sha is None:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="candidate_head_missing")

    # Recheck both pins immediately before the CAS.  A source move never
    # overwrites the story branch and never leaves a disposable checkout behind.
    current_story_sha = _refresh_sha(
        repo_root, f"refs/heads/{request.story_branch}"
    )
    current_epic_sha = _refresh_sha(repo_root, f"refs/heads/{request.epic_branch}")
    if current_story_sha != before_sha or current_epic_sha != pinned_epic_sha:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult(
            "source_moved",
            before_sha=before_sha,
            current_sha=current_story_sha,
            current_epic_tip_sha=current_epic_sha,
        )

    # The isolated merge can take long enough for an operator to edit the
    # original checkout after the first clean-status check.  Recheck immediately
    # before the branch CAS so read-tree can never overwrite newly-created user
    # work, and leave the story branch untouched when it does.
    latest_dirty_paths = _refresh_status_paths(story_worktree)
    if latest_dirty_paths is None:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="status_failed")
    if latest_dirty_paths:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult(
            "dirty",
            before_sha=before_sha,
            current_sha=current_story_sha,
            dirty_paths=latest_dirty_paths,
        )

    updated = _refresh_git(
        repo_root,
        "update-ref",
        f"refs/heads/{request.story_branch}",
        candidate_sha,
        before_sha,
    )
    if updated.returncode != 0:
        current_story_sha = _refresh_sha(
            repo_root, f"refs/heads/{request.story_branch}"
        )
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult(
            "source_moved",
            before_sha=before_sha,
            current_sha=current_story_sha,
            current_epic_tip_sha=current_epic_sha,
        )

    # ``update-ref`` changes the branch atomically, while ``read-tree`` brings
    # the already-verified clean checkout along without reset/clean/stash.
    checked_out = _refresh_git(story_worktree, "read-tree", "-mu", candidate_sha)
    if checked_out.returncode != 0:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, after_sha=candidate_sha, error="story_checkout_update_failed")
    _remove_refresh_worktree(repo_root, candidate, parent)
    return RefreshResult("refreshed", before_sha=before_sha, after_sha=candidate_sha)


def refresh_story_branch(request: RefreshRequest) -> RefreshResult:
    """Run :func:`_refresh_story_branch` and attach the pinned lineage facts."""

    result = _refresh_story_branch(request)
    return replace(
        result,
        story_id=request.story_id,
        story_branch=request.story_branch,
        story_sha=result.after_sha or result.before_sha,
        epic_branch=request.epic_branch,
        epic_tip_sha=request.epic_tip_sha,
    )
