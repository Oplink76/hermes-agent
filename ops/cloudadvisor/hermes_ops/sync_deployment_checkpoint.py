"""Durable authority for resuming a protected merge before deployment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


SCHEMA_VERSION = 1
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT = re.compile(r"pending-deployment-(?P<digest>[0-9a-f]{64})\.json\Z")
_FIELDS = {
    "schema_version",
    "repo_slug",
    "stage",
    "candidate_sha",
    "candidate_tree_sha",
    "pr_number",
    "pr_head_sha",
    "base_sha",
    "upstream_sha",
    "premerge_receipt_path",
    "premerge_receipt_sha256",
    "merge_sha",
    "final_receipt_path",
    "final_receipt_sha256",
    "install_root",
    "previous_installed_sha",
    "terminal_reason",
    "terminal_reason_code",
    "terminal_failed_gate",
    "rollback_state",
    "rollback_sha",
    "revert_state",
    "revert_sha",
}
_POINTER_FIELDS = {"schema_version", "repo_slug", "artifact_sha256"}


class SyncDeploymentCheckpointError(ValueError):
    """Pending deployment evidence is missing, stale, or untrusted."""


@dataclass(frozen=True)
class PendingDeploymentCheckpoint:
    schema_version: int
    repo_slug: str
    stage: str
    candidate_sha: str
    candidate_tree_sha: str
    pr_number: int
    pr_head_sha: str
    base_sha: str
    upstream_sha: str
    premerge_receipt_path: str
    premerge_receipt_sha256: str
    merge_sha: str | None
    final_receipt_path: str | None
    final_receipt_sha256: str | None
    install_root: str
    previous_installed_sha: str
    terminal_reason: str | None
    terminal_reason_code: str | None
    terminal_failed_gate: str | None
    rollback_state: str | None
    rollback_sha: str | None
    revert_state: str | None
    revert_sha: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SyncDeploymentCheckpointArtifact:
    path: Path
    sha256: str


def deployment_checkpoint_sha256(
    checkpoint: PendingDeploymentCheckpoint,
) -> str:
    _validate(checkpoint)
    return hashlib.sha256(_canonical_json(checkpoint.to_dict())).hexdigest()


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _valid_repo_slug(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and value.count("/") == 1
        and all(value.split("/"))
    )


def _terminal_values(
    checkpoint: PendingDeploymentCheckpoint,
) -> tuple[str | None, ...]:
    return (
        checkpoint.terminal_reason,
        checkpoint.terminal_reason_code,
        checkpoint.terminal_failed_gate,
        checkpoint.rollback_state,
        checkpoint.rollback_sha,
        checkpoint.revert_state,
        checkpoint.revert_sha,
    )


def _validate_common(checkpoint: PendingDeploymentCheckpoint) -> None:
    if checkpoint.schema_version != SCHEMA_VERSION:
        raise SyncDeploymentCheckpointError("deployment checkpoint schema is unsupported")
    if not _valid_repo_slug(checkpoint.repo_slug):
        raise SyncDeploymentCheckpointError("deployment repository is invalid")
    shas: tuple[object, ...] = (
        checkpoint.candidate_sha,
        checkpoint.candidate_tree_sha,
        checkpoint.pr_head_sha,
        checkpoint.base_sha,
        checkpoint.upstream_sha,
        checkpoint.previous_installed_sha,
    )
    if not all(isinstance(value, str) and _FULL_SHA.fullmatch(value) for value in shas):
        raise SyncDeploymentCheckpointError("deployment identity is invalid")
    if checkpoint.candidate_sha != checkpoint.pr_head_sha:
        raise SyncDeploymentCheckpointError("deployment candidate identity is crossed")
    if type(checkpoint.pr_number) is not int or checkpoint.pr_number < 1:
        raise SyncDeploymentCheckpointError("deployment PR identity is invalid")
    if _DIGEST.fullmatch(checkpoint.premerge_receipt_sha256) is None:
        raise SyncDeploymentCheckpointError("deployment premerge digest is invalid")
    for name, value in (
        ("premerge receipt", checkpoint.premerge_receipt_path),
        ("install", checkpoint.install_root),
    ):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise SyncDeploymentCheckpointError(
                f"deployment {name} path is invalid"
            )


def _validate_merge_intent(checkpoint: PendingDeploymentCheckpoint) -> None:
    crossed = (
        checkpoint.merge_sha is not None
        or checkpoint.final_receipt_path is not None
        or checkpoint.final_receipt_sha256 is not None
        or any(value is not None for value in _terminal_values(checkpoint))
    )
    if crossed:
        raise SyncDeploymentCheckpointError(
            "deployment merge intent evidence is crossed"
        )


def _validate_merged(checkpoint: PendingDeploymentCheckpoint) -> None:
    if not isinstance(checkpoint.merge_sha, str):
        raise SyncDeploymentCheckpointError("deployment merged evidence is invalid")
    if _FULL_SHA.fullmatch(checkpoint.merge_sha) is None:
        raise SyncDeploymentCheckpointError("deployment merged evidence is invalid")
    if not isinstance(checkpoint.final_receipt_path, str):
        raise SyncDeploymentCheckpointError("deployment merged evidence is invalid")
    if not Path(checkpoint.final_receipt_path).is_absolute():
        raise SyncDeploymentCheckpointError("deployment merged evidence is invalid")
    if not isinstance(checkpoint.final_receipt_sha256, str):
        raise SyncDeploymentCheckpointError("deployment merged evidence is invalid")
    if _DIGEST.fullmatch(checkpoint.final_receipt_sha256) is None:
        raise SyncDeploymentCheckpointError("deployment merged evidence is invalid")


def _validate_terminal(checkpoint: PendingDeploymentCheckpoint) -> None:
    if (
        not isinstance(checkpoint.terminal_reason, str)
        or not checkpoint.terminal_reason.strip()
        or not isinstance(checkpoint.terminal_reason_code, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", checkpoint.terminal_reason_code) is None
        or not isinstance(checkpoint.terminal_failed_gate, str)
        or re.fullmatch(r"[a-z][a-z0-9_]*", checkpoint.terminal_failed_gate) is None
        or not isinstance(checkpoint.rollback_state, str)
        or not checkpoint.rollback_state
        or not isinstance(checkpoint.revert_state, str)
        or not checkpoint.revert_state
    ):
        raise SyncDeploymentCheckpointError(
            "deployment terminal evidence is invalid"
        )
    for value in (checkpoint.rollback_sha, checkpoint.revert_sha):
        if value is not None and (
            not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None
        ):
            raise SyncDeploymentCheckpointError(
                "deployment terminal SHA is invalid"
            )


def _validate(checkpoint: PendingDeploymentCheckpoint) -> None:
    _validate_common(checkpoint)
    if checkpoint.stage == "merge_intent":
        _validate_merge_intent(checkpoint)
        return
    if checkpoint.stage not in {"merged_pending_deploy", "failed_terminal"}:
        raise SyncDeploymentCheckpointError("deployment merged evidence is invalid")
    _validate_merged(checkpoint)
    if checkpoint.stage == "merged_pending_deploy":
        if any(value is not None for value in _terminal_values(checkpoint)):
            raise SyncDeploymentCheckpointError(
                "deployment pending evidence contains terminal state"
            )
        return
    _validate_terminal(checkpoint)


def _root(receipt_root: Path, *, create: bool = False) -> Path:
    root = Path(receipt_root).expanduser().resolve(strict=False) / "deployment"
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        if not create:
            return root
        root.mkdir(parents=True, mode=0o700)
        metadata = root.lstat()
    except OSError as exc:
        raise SyncDeploymentCheckpointError(
            "deployment checkpoint scope is unreadable"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SyncDeploymentCheckpointError("deployment checkpoint scope is untrusted")
    return root


def _fsync_directory(root: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(root: Path, path: Path, content: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        os.replace(temporary, path)
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _write_pointer(root: Path, *, repo_slug: str, digest: str) -> None:
    _atomic_write(
        root,
        root / "pending.json",
        _canonical_json(
            {
                "artifact_sha256": digest,
                "repo_slug": repo_slug,
                "schema_version": SCHEMA_VERSION,
            }
        ),
        mode=0o600,
    )


def write_pending_deployment(
    receipt_root: Path,
    checkpoint: PendingDeploymentCheckpoint,
) -> SyncDeploymentCheckpointArtifact:
    _validate(checkpoint)
    root = _root(receipt_root, create=True)
    content = _canonical_json(checkpoint.to_dict())
    digest = hashlib.sha256(content).hexdigest()
    target = root / f"pending-deployment-{digest}.json"
    if target.exists():
        if _load_artifact(target) != checkpoint:
            raise SyncDeploymentCheckpointError("deployment digest collision")
    else:
        _atomic_write(root, target, content, mode=0o400)
        _load_artifact(target)
    _write_pointer(root, repo_slug=checkpoint.repo_slug, digest=digest)
    return SyncDeploymentCheckpointArtifact(path=target, sha256=digest)


def terminalize_pending_deployment(
    receipt_root: Path,
    checkpoint: PendingDeploymentCheckpoint,
    *,
    reason: str,
    reason_code: str,
    failed_gate: str,
    rollback_state: str,
    rollback_sha: str | None,
    revert_state: str,
    revert_sha: str | None,
) -> SyncDeploymentCheckpointArtifact:
    if checkpoint.stage != "merged_pending_deploy":
        raise SyncDeploymentCheckpointError(
            "only a merged pending deployment can become terminal"
        )
    return write_pending_deployment(
        receipt_root,
        PendingDeploymentCheckpoint(
            **{
                **checkpoint.to_dict(),
                "stage": "failed_terminal",
                "terminal_reason": reason,
                "terminal_reason_code": reason_code,
                "terminal_failed_gate": failed_gate,
                "rollback_state": rollback_state,
                "rollback_sha": rollback_sha,
                "revert_state": revert_state,
                "revert_sha": revert_sha,
            }
        ),
    )


def _load_artifact(path: Path) -> PendingDeploymentCheckpoint:
    try:
        metadata = path.lstat()
        content = path.read_bytes()
    except OSError as exc:
        raise SyncDeploymentCheckpointError("deployment artifact is missing") from exc
    match = _ARTIFACT.fullmatch(path.name)
    if (
        match is None
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o400)
        or hashlib.sha256(content).hexdigest() != match.group("digest")
    ):
        raise SyncDeploymentCheckpointError("deployment artifact is untrusted")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncDeploymentCheckpointError("deployment artifact is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _FIELDS
        or _canonical_json(payload) != content
    ):
        raise SyncDeploymentCheckpointError("deployment artifact schema is invalid")
    try:
        checkpoint = PendingDeploymentCheckpoint(**payload)
    except TypeError as exc:
        raise SyncDeploymentCheckpointError("deployment artifact is invalid") from exc
    _validate(checkpoint)
    return checkpoint


def load_pending_deployment(
    receipt_root: Path, *, repo_slug: str
) -> PendingDeploymentCheckpoint | None:
    root = _root(receipt_root)
    pointer = root / "pending.json"
    try:
        metadata = pointer.lstat()
        content = pointer.read_bytes()
        payload = json.loads(content)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncDeploymentCheckpointError("deployment pointer is unreadable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
        or not isinstance(payload, dict)
        or set(payload) != _POINTER_FIELDS
        or _canonical_json(payload) != content
        or payload["schema_version"] != SCHEMA_VERSION
        or payload["repo_slug"] != repo_slug
        or _DIGEST.fullmatch(str(payload["artifact_sha256"])) is None
    ):
        raise SyncDeploymentCheckpointError("deployment pointer is invalid")
    checkpoint = _load_artifact(
        root / f"pending-deployment-{payload['artifact_sha256']}.json"
    )
    if checkpoint.repo_slug != repo_slug:
        raise SyncDeploymentCheckpointError("deployment repository does not match")
    return checkpoint


def clear_pending_deployment(receipt_root: Path, *, sha256: str) -> None:
    if _DIGEST.fullmatch(sha256) is None:
        raise SyncDeploymentCheckpointError("deployment digest is invalid")
    root = _root(receipt_root)
    pointer = root / "pending.json"
    try:
        content = pointer.read_bytes()
        payload = json.loads(content)
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncDeploymentCheckpointError("deployment pointer is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _POINTER_FIELDS
        or _canonical_json(payload) != content
    ):
        raise SyncDeploymentCheckpointError("deployment pointer is invalid")
    if payload["artifact_sha256"] == sha256:
        pointer.unlink()
        _fsync_directory(root)
