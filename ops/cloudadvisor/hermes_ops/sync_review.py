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


def _validate_resolution_record(path: Path) -> None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ConflictReviewError("resolution record is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise ConflictReviewError("resolution record is incomplete")
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list) or not conflicts:
        raise ConflictReviewError("resolution record is incomplete")
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


def validate_conflict_review(
    receipt: ConflictReviewReceipt,
    *,
    candidate_sha: str,
    resolver_backend: str,
    resolution_record: Path,
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
        or receipt.reviewer_backend == resolver_backend
    ):
        raise ConflictReviewError("conflict review is not independent")

    _validate_resolution_record(resolution_record)

    if receipt.verdict == "major":
        return SyncClassification.MAJOR
    if receipt.verdict != "green":
        raise ConflictReviewError("conflict review verdict is invalid")
    if receipt.findings:
        raise ConflictReviewError("green conflict review must have zero findings")
    return SyncClassification.MINOR_RESOLVED
