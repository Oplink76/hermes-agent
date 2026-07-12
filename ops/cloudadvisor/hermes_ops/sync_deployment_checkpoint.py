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
    "candidate_sha",
    "candidate_tree_sha",
    "pr_number",
    "pr_head_sha",
    "base_sha",
    "upstream_sha",
    "merge_sha",
    "final_receipt_path",
    "final_receipt_sha256",
    "install_root",
    "previous_installed_sha",
}
_POINTER_FIELDS = {"schema_version", "repo_slug", "artifact_sha256"}


class SyncDeploymentCheckpointError(ValueError):
    """Pending deployment evidence is missing, stale, or untrusted."""


@dataclass(frozen=True)
class PendingDeploymentCheckpoint:
    schema_version: int
    repo_slug: str
    candidate_sha: str
    candidate_tree_sha: str
    pr_number: int
    pr_head_sha: str
    base_sha: str
    upstream_sha: str
    merge_sha: str
    final_receipt_path: str
    final_receipt_sha256: str
    install_root: str
    previous_installed_sha: str

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


def _validate(checkpoint: PendingDeploymentCheckpoint) -> None:
    if checkpoint.schema_version != SCHEMA_VERSION:
        raise SyncDeploymentCheckpointError("deployment checkpoint schema is unsupported")
    if (
        not isinstance(checkpoint.repo_slug, str)
        or checkpoint.repo_slug != checkpoint.repo_slug.strip()
        or checkpoint.repo_slug.count("/") != 1
        or not all(checkpoint.repo_slug.split("/"))
    ):
        raise SyncDeploymentCheckpointError("deployment repository is invalid")
    shas = (
        checkpoint.candidate_sha,
        checkpoint.candidate_tree_sha,
        checkpoint.pr_head_sha,
        checkpoint.base_sha,
        checkpoint.upstream_sha,
        checkpoint.merge_sha,
        checkpoint.previous_installed_sha,
    )
    if not all(isinstance(value, str) and _FULL_SHA.fullmatch(value) for value in shas):
        raise SyncDeploymentCheckpointError("deployment identity is invalid")
    if checkpoint.candidate_sha != checkpoint.pr_head_sha:
        raise SyncDeploymentCheckpointError("deployment candidate identity is crossed")
    if type(checkpoint.pr_number) is not int or checkpoint.pr_number < 1:
        raise SyncDeploymentCheckpointError("deployment PR identity is invalid")
    if _DIGEST.fullmatch(checkpoint.final_receipt_sha256) is None:
        raise SyncDeploymentCheckpointError("deployment receipt digest is invalid")
    for name, value in (
        ("receipt", checkpoint.final_receipt_path),
        ("install", checkpoint.install_root),
    ):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise SyncDeploymentCheckpointError(
                f"deployment {name} path is invalid"
            )


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
