"""Independent exact-head review contract for resolved sync conflicts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .sync import SyncClassification


class ConflictReviewError(ValueError):
    """The conflict review does not prove minor-resolution eligibility."""


@dataclass(frozen=True)
class ConflictReviewReceipt:
    candidate_sha: str
    resolver_backend: str
    reviewer_backend: str
    verdict: Literal["green", "major"]
    findings: tuple[str, ...]
    reviewed_at: str


class IndependentConflictReviewer(Protocol):
    def review(
        self,
        *,
        candidate_sha: str,
        worktree: Path,
        resolution_record: Path,
    ) -> ConflictReviewReceipt: ...


def _resolution_record_paths(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ConflictReviewError("resolution record is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise ConflictReviewError("resolution record is incomplete")
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list) or not conflicts:
        raise ConflictReviewError("resolution record is incomplete")
    paths: list[str] = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            raise ConflictReviewError("resolution record is incomplete")
        conflict_path = conflict.get("path")
        decision = conflict.get("decision")
        if (
            not isinstance(conflict_path, str)
            or not conflict_path.strip()
            or not isinstance(decision, str)
            or not decision.strip()
        ):
            raise ConflictReviewError("resolution record is incomplete")
        paths.append(conflict_path)
    return tuple(paths)


def validate_conflict_review(
    receipt: ConflictReviewReceipt,
    *,
    candidate_sha: str,
    resolver_backend: str,
    resolution_record: Path,
    conflicted_files: tuple[str, ...],
) -> SyncClassification:
    """Return the reviewed classification or reject incomplete/stale evidence."""
    if (
        not isinstance(candidate_sha, str)
        or not candidate_sha
        or receipt.candidate_sha != candidate_sha
    ):
        raise ConflictReviewError("conflict review candidate SHA is not exact")
    if (
        not isinstance(resolver_backend, str)
        or not resolver_backend.strip()
        or resolver_backend != resolver_backend.strip()
        or receipt.resolver_backend != resolver_backend
    ):
        raise ConflictReviewError("conflict review resolver backend does not match")
    if (
        not isinstance(receipt.reviewer_backend, str)
        or not receipt.reviewer_backend.strip()
        or receipt.reviewer_backend != receipt.reviewer_backend.strip()
        or receipt.reviewer_backend.casefold() == resolver_backend.casefold()
    ):
        raise ConflictReviewError("conflict review is not independent")

    record_paths = _resolution_record_paths(resolution_record)
    if (
        not isinstance(conflicted_files, tuple)
        or not conflicted_files
        or not all(isinstance(path, str) and path for path in conflicted_files)
        or len(set(conflicted_files)) != len(conflicted_files)
        or len(set(record_paths)) != len(record_paths)
        or set(record_paths) != set(conflicted_files)
    ):
        raise ConflictReviewError(
            "resolution record paths do not match conflicted files"
        )

    if receipt.verdict == "major":
        return SyncClassification.MAJOR
    if receipt.verdict != "green":
        raise ConflictReviewError("conflict review verdict is invalid")
    if receipt.findings:
        raise ConflictReviewError("green conflict review must have zero findings")
    return SyncClassification.MINOR_RESOLVED
