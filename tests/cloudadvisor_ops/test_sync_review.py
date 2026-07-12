from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync import SyncClassification
from ops.cloudadvisor.hermes_ops.sync_review import (
    ConflictReviewError,
    ConflictReviewReceipt,
    validate_conflict_review,
)


CANDIDATE_SHA = "a" * 40


def resolution_record(
    tmp_path: Path,
    *,
    complete: bool = True,
    paths: tuple[str, ...] = ("gateway/run.py",),
) -> Path:
    path = tmp_path / "resolution.json"
    decision = "preserve fork behavior" if complete else ""
    conflicts = [
        {"path": conflict_path, "decision": decision} for conflict_path in paths
    ]
    path.write_text(json.dumps({"conflicts": conflicts}), encoding="utf-8")
    return path


def receipt(**overrides):
    values = {
        "candidate_sha": CANDIDATE_SHA,
        "resolver_backend": "codex",
        "reviewer_backend": "claude",
        "verdict": "green",
        "findings": (),
        "reviewed_at": "2026-07-12T16:00:00Z",
    }
    values.update(overrides)
    return ConflictReviewReceipt(**values)


def validate(tmp_path: Path, review):
    return validate_conflict_review(
        review,
        candidate_sha=CANDIDATE_SHA,
        resolver_backend="codex",
        resolution_record=resolution_record(tmp_path),
        conflicted_files=("gateway/run.py",),
    )


def test_exact_independent_green_review_classifies_minor_resolved(tmp_path: Path):
    result = validate(tmp_path, receipt())

    assert result is SyncClassification.MINOR_RESOLVED


def test_review_requires_exact_candidate_sha(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="candidate SHA"):
        validate(tmp_path, receipt(candidate_sha="b" * 40))


def test_review_requires_configured_resolver_backend(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="resolver backend"):
        validate(tmp_path, receipt(resolver_backend="other"))


def test_review_requires_different_backend_ids(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="independent"):
        validate(tmp_path, receipt(reviewer_backend="codex"))


def test_backend_id_whitespace_cannot_bypass_independence(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="independent"):
        validate(tmp_path, receipt(reviewer_backend=" codex "))


def test_backend_id_case_cannot_bypass_independence(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="independent"):
        validate(tmp_path, receipt(reviewer_backend="Codex"))


def test_review_requires_complete_resolution_record(tmp_path: Path):
    record = resolution_record(tmp_path, complete=False)

    with pytest.raises(ConflictReviewError, match="resolution record"):
        validate_conflict_review(
            receipt(),
            candidate_sha=CANDIDATE_SHA,
            resolver_backend="codex",
            resolution_record=record,
            conflicted_files=("gateway/run.py",),
        )


def test_resolution_record_rejects_missing_conflicted_file(tmp_path: Path):
    record = resolution_record(tmp_path)

    with pytest.raises(ConflictReviewError, match="conflicted files"):
        validate_conflict_review(
            receipt(),
            candidate_sha=CANDIDATE_SHA,
            resolver_backend="codex",
            resolution_record=record,
            conflicted_files=("gateway/run.py", "hermes_cli/kanban.py"),
        )


def test_resolution_record_rejects_extra_conflicted_file(tmp_path: Path):
    record = resolution_record(
        tmp_path,
        paths=("gateway/run.py", "hermes_cli/kanban.py"),
    )

    with pytest.raises(ConflictReviewError, match="conflicted files"):
        validate_conflict_review(
            receipt(),
            candidate_sha=CANDIDATE_SHA,
            resolver_backend="codex",
            resolution_record=record,
            conflicted_files=("gateway/run.py",),
        )


def test_resolution_record_rejects_duplicate_conflicted_file(tmp_path: Path):
    record = resolution_record(
        tmp_path,
        paths=("gateway/run.py", "gateway/run.py"),
    )

    with pytest.raises(ConflictReviewError, match="conflicted files"):
        validate_conflict_review(
            receipt(),
            candidate_sha=CANDIDATE_SHA,
            resolver_backend="codex",
            resolution_record=record,
            conflicted_files=("gateway/run.py",),
        )


def test_green_review_requires_zero_findings(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="findings"):
        validate(tmp_path, receipt(findings=("guard behavior changed",)))


def test_major_review_fails_closed(tmp_path: Path):
    result = validate(
        tmp_path,
        receipt(verdict="major", findings=("product judgment required",)),
    )

    assert result is SyncClassification.MAJOR
