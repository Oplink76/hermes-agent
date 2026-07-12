"""Immutable, candidate-bound evidence for resolved sync conflicts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .sync import SyncResult


SCHEMA_VERSION = 1
_ARTIFACT = re.compile(r"resolution-(?P<digest>[0-9a-f]{64})\.json\Z")


class ResolutionRecordError(ValueError):
    """Resolution evidence is mutable, misplaced, or incomplete."""


def _canonical(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _conflicts(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise ResolutionRecordError("resolution record is incomplete")
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "decision"}:
            raise ResolutionRecordError("resolution record is incomplete")
        path = item["path"]
        decision = item["decision"]
        if (
            not isinstance(path, str)
            or not path.strip()
            or not isinstance(decision, str)
            or not decision.strip()
        ):
            raise ResolutionRecordError("resolution record is incomplete")
        rows.append({"path": path, "decision": decision})
    paths = [row["path"] for row in rows]
    if len(paths) != len(set(paths)):
        raise ResolutionRecordError("resolution record contains duplicate paths")
    return tuple(rows)


@dataclass(frozen=True)
class ResolutionRecordArtifact:
    path: Path
    sha256: str
    candidate_sha: str
    conflicts: tuple[dict[str, str], ...]
    strategy: str

    @classmethod
    def load(cls, path: Path) -> "ResolutionRecordArtifact":
        path = Path(path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ResolutionRecordError("resolution artifact is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ResolutionRecordError("resolution artifact must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o400:
            raise ResolutionRecordError("resolution artifact must be read-only mode 0400")
        match = _ARTIFACT.fullmatch(path.name)
        if match is None:
            raise ResolutionRecordError("resolution artifact filename is invalid")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != match.group("digest"):
            raise ResolutionRecordError("resolution artifact digest does not match")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResolutionRecordError("resolution artifact JSON is invalid") from exc
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"schema_version", "candidate_sha", "conflicts", "strategy"}
            or payload["schema_version"] != SCHEMA_VERSION
            or not isinstance(payload["candidate_sha"], str)
            or not payload["candidate_sha"]
            or not isinstance(payload["strategy"], str)
            or not payload["strategy"]
            or _canonical(payload) != content
        ):
            raise ResolutionRecordError("resolution artifact is not canonical")
        conflicts = _conflicts(payload["conflicts"])
        return cls(
            path, digest, payload["candidate_sha"], conflicts, payload["strategy"]
        )


def freeze_resolution_record(
    receipt_root: Path,
    candidate: SyncResult,
) -> ResolutionRecordArtifact:
    """Copy one mutable Resolver output into immutable canonical authority."""
    if (
        not candidate.candidate_sha
        or candidate.resolution_record is None
        or candidate.resolution_evidence_dir is None
        or not candidate.conflicted_files
        or not candidate.resolution_strategy
    ):
        raise ResolutionRecordError("resolution evidence context is incomplete")
    record = Path(candidate.resolution_record)
    evidence_dir = Path(candidate.resolution_evidence_dir)
    try:
        directory_meta = evidence_dir.lstat()
        record_meta = record.lstat()
    except OSError as exc:
        raise ResolutionRecordError("resolution record is missing") from exc
    if stat.S_ISLNK(directory_meta.st_mode) or not stat.S_ISDIR(directory_meta.st_mode):
        raise ResolutionRecordError("resolution evidence directory is invalid")
    if record.parent != evidence_dir:
        raise ResolutionRecordError("resolution record is outside evidence directory")
    if stat.S_ISLNK(record_meta.st_mode) or not stat.S_ISREG(record_meta.st_mode):
        raise ResolutionRecordError("resolution record must be a regular file")
    try:
        raw = json.loads(record.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolutionRecordError("resolution record JSON is invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"conflicts", "strategy"}:
        raise ResolutionRecordError("resolution record is incomplete")
    if raw["strategy"] != candidate.resolution_strategy:
        raise ResolutionRecordError("resolution record strategy does not match")
    conflicts = _conflicts(raw["conflicts"])
    paths = tuple(row["path"] for row in conflicts)
    if set(paths) != set(candidate.conflicted_files) or len(paths) != len(
        candidate.conflicted_files
    ):
        raise ResolutionRecordError("resolution record paths do not match conflicts")
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": candidate.candidate_sha,
        "conflicts": list(conflicts),
        "strategy": candidate.resolution_strategy,
    }
    content = _canonical(payload)
    digest = hashlib.sha256(content).hexdigest()
    root = Path(receipt_root) / "resolutions"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"resolution-{digest}.json"
    if not target.exists():
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".resolution-", suffix=".tmp", dir=root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o400)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    artifact = ResolutionRecordArtifact.load(target)
    if artifact.candidate_sha != candidate.candidate_sha:
        raise ResolutionRecordError("resolution artifact candidate is not exact")
    return artifact
