"""Immutable evidence for exact conflict-review attempts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


SCHEMA_VERSION = 1
ReviewKind = Literal["initial", "major_confirmation"]
ReviewVerdict = Literal["green", "major"]

_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT = re.compile(r"review-(?P<digest>[0-9a-f]{64})\.json\Z")
_FIELDS = {
    "schema_version",
    "candidate_sha",
    "resolution_record_sha256",
    "resolver_backend",
    "reviewer_backend",
    "attempt",
    "review_kind",
    "verdict",
    "findings",
    "reviewed_at",
    "prior_artifact_sha256",
}


class ConflictReviewEvidenceError(ValueError):
    """A conflict-review attempt artifact is unsafe or invalid."""


def _requires_posix_readonly() -> bool:
    return os.name != "nt"


def _canonical(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ConflictReviewEvidenceError("review timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ConflictReviewEvidenceError("review timestamp is invalid") from exc
    if parsed.utcoffset() is None:
        raise ConflictReviewEvidenceError("review timestamp is invalid")
    return value


def _validate_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ConflictReviewEvidenceError("review artifact schema is invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ConflictReviewEvidenceError("review artifact schema is invalid")
    candidate_sha = payload["candidate_sha"]
    if not isinstance(candidate_sha, str) or _FULL_SHA.fullmatch(candidate_sha) is None:
        raise ConflictReviewEvidenceError("review candidate SHA is invalid")
    resolution_digest = payload["resolution_record_sha256"]
    if (
        not isinstance(resolution_digest, str)
        or _DIGEST.fullmatch(resolution_digest) is None
    ):
        raise ConflictReviewEvidenceError("review resolution digest is invalid")

    resolver = payload["resolver_backend"]
    reviewer = payload["reviewer_backend"]
    if (
        not isinstance(resolver, str)
        or not resolver
        or resolver != resolver.strip()
        or not isinstance(reviewer, str)
        or not reviewer
        or reviewer != reviewer.strip()
    ):
        raise ConflictReviewEvidenceError("review backend identity is invalid")
    if resolver.casefold() == reviewer.casefold():
        raise ConflictReviewEvidenceError("review backends are not independent")

    attempt = payload["attempt"]
    kind = payload["review_kind"]
    prior = payload["prior_artifact_sha256"]
    if type(attempt) is not int or attempt not in {1, 2}:
        raise ConflictReviewEvidenceError("review attempt is invalid")
    if attempt == 1:
        if kind != "initial":
            raise ConflictReviewEvidenceError("review attempt and kind do not match")
        if prior is not None:
            raise ConflictReviewEvidenceError(
                "initial review cannot reference a prior artifact"
            )
    else:
        if kind != "major_confirmation":
            raise ConflictReviewEvidenceError("review attempt and kind do not match")
        if not isinstance(prior, str) or _DIGEST.fullmatch(prior) is None:
            raise ConflictReviewEvidenceError(
                "confirmation review prior artifact is invalid"
            )

    verdict = payload["verdict"]
    findings = payload["findings"]
    if verdict not in {"green", "major"} or not isinstance(findings, list):
        raise ConflictReviewEvidenceError("review verdict or findings are invalid")
    if not all(
        isinstance(finding, str) and finding and finding == finding.strip()
        for finding in findings
    ):
        raise ConflictReviewEvidenceError("review findings are invalid")
    if verdict == "green" and findings:
        raise ConflictReviewEvidenceError("green review must not contain findings")
    if verdict == "major" and not findings:
        raise ConflictReviewEvidenceError("major review must contain findings")
    _validate_timestamp(payload["reviewed_at"])
    return payload


@dataclass(frozen=True)
class ConflictReviewAttemptArtifact:
    path: Path
    sha256: str
    candidate_sha: str
    resolution_record_sha256: str
    resolver_backend: str
    reviewer_backend: str
    attempt: int
    review_kind: ReviewKind
    verdict: ReviewVerdict
    findings: tuple[str, ...]
    reviewed_at: str
    prior_artifact_sha256: str | None

    @property
    def relative_path(self) -> str:
        return f"conflict-reviews/{self.path.name}"

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        receipt_root: Path | None = None,
    ) -> "ConflictReviewAttemptArtifact":
        path = Path(os.path.abspath(path))
        try:
            parent_metadata = path.parent.lstat()
            metadata = path.lstat()
        except OSError as exc:
            raise ConflictReviewEvidenceError("review artifact is missing") from exc
        if (
            path.parent.name != "conflict-reviews"
            or stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
        ):
            raise ConflictReviewEvidenceError(
                "review artifact must be in a direct conflict-reviews directory"
            )
        if _requires_posix_readonly() and stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            raise ConflictReviewEvidenceError(
                "review evidence directory must be private mode 0700"
            )
        if receipt_root is not None:
            root = Path(os.path.abspath(receipt_root))
            try:
                root_metadata = root.lstat()
                resolved_root = root.resolve(strict=True)
                resolved_parent = path.parent.resolve(strict=True)
            except OSError as exc:
                raise ConflictReviewEvidenceError(
                    "review artifact receipt root is invalid"
                ) from exc
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or not stat.S_ISDIR(root_metadata.st_mode)
                or resolved_parent != resolved_root / "conflict-reviews"
            ):
                raise ConflictReviewEvidenceError(
                    "review artifact is outside the configured receipt root"
                )
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ConflictReviewEvidenceError(
                "review artifact must be a regular file"
            )
        if _requires_posix_readonly() and stat.S_IMODE(metadata.st_mode) != 0o400:
            raise ConflictReviewEvidenceError(
                "review artifact must be read-only mode 0400"
            )
        match = _ARTIFACT.fullmatch(path.name)
        if match is None:
            raise ConflictReviewEvidenceError("review artifact filename is invalid")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ConflictReviewEvidenceError("review artifact could not be read") from exc
        digest = hashlib.sha256(content).hexdigest()
        if digest != match.group("digest"):
            raise ConflictReviewEvidenceError("review artifact digest does not match")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConflictReviewEvidenceError("review artifact JSON is invalid") from exc
        validated = _validate_payload(payload)
        if _canonical(validated) != content:
            raise ConflictReviewEvidenceError("review artifact is not canonical")
        return cls(
            path=path,
            sha256=digest,
            candidate_sha=validated["candidate_sha"],
            resolution_record_sha256=validated["resolution_record_sha256"],
            resolver_backend=validated["resolver_backend"],
            reviewer_backend=validated["reviewer_backend"],
            attempt=validated["attempt"],
            review_kind=validated["review_kind"],
            verdict=validated["verdict"],
            findings=tuple(validated["findings"]),
            reviewed_at=validated["reviewed_at"],
            prior_artifact_sha256=validated["prior_artifact_sha256"],
        )


def write_conflict_review_attempt(
    receipt_root: Path,
    *,
    candidate_sha: str,
    resolution_record_sha256: str,
    resolver_backend: str,
    reviewer_backend: str,
    attempt: int,
    review_kind: ReviewKind,
    verdict: ReviewVerdict,
    findings: tuple[str, ...],
    reviewed_at: str,
    prior_artifact_sha256: str | None = None,
) -> ConflictReviewAttemptArtifact:
    """Validate and atomically publish one structured review attempt."""
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": candidate_sha,
        "resolution_record_sha256": resolution_record_sha256,
        "resolver_backend": resolver_backend,
        "reviewer_backend": reviewer_backend,
        "attempt": attempt,
        "review_kind": review_kind,
        "verdict": verdict,
        "findings": list(findings),
        "reviewed_at": reviewed_at,
        "prior_artifact_sha256": prior_artifact_sha256,
    }
    _validate_payload(payload)
    content = _canonical(payload)
    digest = hashlib.sha256(content).hexdigest()
    root = Path(os.path.abspath(receipt_root))
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root_metadata = root.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise ConflictReviewEvidenceError("review evidence receipt root is invalid")
        root = root.resolve(strict=True)
        directory = root / "conflict-reviews"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_metadata = directory.lstat()
    except ConflictReviewEvidenceError:
        raise
    except OSError as exc:
        raise ConflictReviewEvidenceError(
            "review evidence receipt root is invalid"
        ) from exc
    if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(
        directory_metadata.st_mode
    ):
        raise ConflictReviewEvidenceError("review evidence directory is invalid")
    if _requires_posix_readonly() and stat.S_IMODE(directory_metadata.st_mode) != 0o700:
        raise ConflictReviewEvidenceError(
            "review evidence directory must be private mode 0700"
        )

    target = directory / f"review-{digest}.json"
    if target.exists() or target.is_symlink():
        return ConflictReviewAttemptArtifact.load(target, receipt_root=root)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".review-", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _requires_posix_readonly():
            temporary.chmod(0o400)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return ConflictReviewAttemptArtifact.load(target, receipt_root=root)
