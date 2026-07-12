"""Durable evidence for resuming complete-tree sync reconstruction."""

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
MAX_RESUME_ATTEMPTS = 2
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT = re.compile(r"pending-reconstruction-(?P<digest>[0-9a-f]{64})\.json\Z")
_FIELDS = {
    "schema_version",
    "repo_slug",
    "failed_base_sha",
    "failed_upstream_sha",
    "failed_candidate_sha",
    "failed_candidate_tree_sha",
    "failed_pr_number",
    "failed_merge_sha",
    "revert_main_sha",
    "previous_healthy_installed_sha",
    "rolling_candidate_sha",
    "pending_upstream_sha",
    "reason",
    "resume_attempts",
}
_POINTER_FIELDS = {"schema_version", "repo_slug", "artifact_sha256"}


class ReconstructionCheckpointError(ValueError):
    """Pending reconstruction evidence is missing, stale, or untrusted."""


@dataclass(frozen=True)
class PendingReconstructionCheckpoint:
    schema_version: int
    repo_slug: str
    failed_base_sha: str
    failed_upstream_sha: str
    failed_candidate_sha: str
    failed_candidate_tree_sha: str
    failed_pr_number: int
    failed_merge_sha: str
    revert_main_sha: str
    previous_healthy_installed_sha: str
    rolling_candidate_sha: str
    pending_upstream_sha: str
    reason: str
    resume_attempts: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ReconstructionCheckpointArtifact:
    path: Path
    sha256: str


def reconstruction_checkpoint_sha256(
    checkpoint: PendingReconstructionCheckpoint,
) -> str:
    _validate(checkpoint)
    return hashlib.sha256(_canonical_json(checkpoint.to_dict())).hexdigest()


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _validate_repo_slug(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and value.count("/") == 1
        and all(value.split("/"))
    )


def _validate(checkpoint: PendingReconstructionCheckpoint) -> None:
    if checkpoint.schema_version != SCHEMA_VERSION:
        raise ReconstructionCheckpointError("reconstruction schema is unsupported")
    if not _validate_repo_slug(checkpoint.repo_slug):
        raise ReconstructionCheckpointError("reconstruction repository is invalid")
    shas = (
        checkpoint.failed_base_sha,
        checkpoint.failed_upstream_sha,
        checkpoint.failed_candidate_sha,
        checkpoint.failed_candidate_tree_sha,
        checkpoint.failed_merge_sha,
        checkpoint.revert_main_sha,
        checkpoint.previous_healthy_installed_sha,
        checkpoint.rolling_candidate_sha,
        checkpoint.pending_upstream_sha,
    )
    if not all(isinstance(value, str) and _FULL_SHA.fullmatch(value) for value in shas):
        raise ReconstructionCheckpointError("reconstruction identity is invalid")
    if checkpoint.pending_upstream_sha == checkpoint.failed_upstream_sha:
        raise ReconstructionCheckpointError("reconstruction upstream did not advance")
    if type(checkpoint.failed_pr_number) is not int or checkpoint.failed_pr_number < 1:
        raise ReconstructionCheckpointError("reconstruction PR identity is invalid")
    if (
        type(checkpoint.resume_attempts) is not int
        or not 0 <= checkpoint.resume_attempts <= MAX_RESUME_ATTEMPTS
    ):
        raise ReconstructionCheckpointError("reconstruction retry count is invalid")
    if not isinstance(checkpoint.reason, str) or not checkpoint.reason.strip():
        raise ReconstructionCheckpointError("reconstruction reason is missing")


def _root(receipt_root: Path, *, create: bool = False) -> Path:
    root = Path(receipt_root).expanduser().resolve(strict=False) / "reconstruction"
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        if not create:
            return root
        root.mkdir(parents=True, mode=0o700)
        metadata = root.lstat()
    except OSError as exc:
        raise ReconstructionCheckpointError(
            "reconstruction checkpoint scope is unreadable"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReconstructionCheckpointError(
            "reconstruction checkpoint scope is untrusted"
        )
    return root


def _fsync_directory(root: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_pointer(root: Path, *, repo_slug: str, digest: str) -> None:
    content = _canonical_json({
        "artifact_sha256": digest,
        "repo_slug": repo_slug,
        "schema_version": SCHEMA_VERSION,
    })
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".pending-reconstruction-", suffix=".tmp", dir=root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, root / "pending.json")
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def write_pending_reconstruction(
    receipt_root: Path,
    checkpoint: PendingReconstructionCheckpoint,
) -> ReconstructionCheckpointArtifact:
    _validate(checkpoint)
    root = _root(receipt_root, create=True)
    content = _canonical_json(checkpoint.to_dict())
    digest = hashlib.sha256(content).hexdigest()
    target = root / f"pending-reconstruction-{digest}.json"
    if target.exists():
        loaded = _load_artifact(target)
        if loaded != checkpoint:
            raise ReconstructionCheckpointError("reconstruction digest collision")
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pending-reconstruction-", suffix=".tmp", dir=root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                temporary.chmod(0o400)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != content:
                    raise ReconstructionCheckpointError(
                        "reconstruction digest collision"
                    )
            _load_artifact(target)
            _fsync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)
    _write_pointer(root, repo_slug=checkpoint.repo_slug, digest=digest)
    return ReconstructionCheckpointArtifact(target, digest)


def _load_artifact(path: Path) -> PendingReconstructionCheckpoint:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReconstructionCheckpointError("reconstruction artifact is missing") from exc
    match = _ARTIFACT.fullmatch(path.name)
    if (
        match is None
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o400)
    ):
        raise ReconstructionCheckpointError("reconstruction artifact is untrusted")
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconstructionCheckpointError("reconstruction artifact is unreadable") from exc
    if hashlib.sha256(content).hexdigest() != match.group("digest"):
        raise ReconstructionCheckpointError("reconstruction artifact digest changed")
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ReconstructionCheckpointError("reconstruction artifact schema is invalid")
    if _canonical_json(payload) != content:
        raise ReconstructionCheckpointError("reconstruction artifact is not canonical")
    try:
        checkpoint = PendingReconstructionCheckpoint(**payload)
    except TypeError as exc:
        raise ReconstructionCheckpointError("reconstruction artifact is invalid") from exc
    _validate(checkpoint)
    return checkpoint


def load_pending_reconstruction(
    receipt_root: Path,
    *,
    repo_slug: str,
) -> PendingReconstructionCheckpoint | None:
    root = _root(receipt_root)
    pointer = root / "pending.json"
    try:
        metadata = pointer.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReconstructionCheckpointError("reconstruction pointer is unreadable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise ReconstructionCheckpointError("reconstruction pointer is untrusted")
    try:
        content = pointer.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconstructionCheckpointError("reconstruction pointer is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _POINTER_FIELDS
        or _canonical_json(payload) != content
        or payload["schema_version"] != SCHEMA_VERSION
        or payload["repo_slug"] != repo_slug
        or not _DIGEST.fullmatch(str(payload["artifact_sha256"]))
    ):
        raise ReconstructionCheckpointError("reconstruction pointer is invalid")
    artifact = root / f"pending-reconstruction-{payload['artifact_sha256']}.json"
    checkpoint = _load_artifact(artifact)
    if checkpoint.repo_slug != repo_slug:
        raise ReconstructionCheckpointError("reconstruction repository does not match")
    return checkpoint


def clear_pending_reconstruction(receipt_root: Path, *, sha256: str) -> None:
    if _DIGEST.fullmatch(sha256) is None:
        raise ReconstructionCheckpointError("reconstruction digest is invalid")
    root = _root(receipt_root)
    pointer = root / "pending.json"
    try:
        content = pointer.read_bytes()
        payload = json.loads(content)
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconstructionCheckpointError("reconstruction pointer is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _POINTER_FIELDS
        or _canonical_json(payload) != content
    ):
        raise ReconstructionCheckpointError("reconstruction pointer is invalid")
    if payload["artifact_sha256"] == sha256:
        pointer.unlink()
        _fsync_directory(root)
