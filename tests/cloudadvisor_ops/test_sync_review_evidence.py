from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync_review_evidence import (
    ConflictReviewAttemptArtifact,
    ConflictReviewEvidenceError,
    write_conflict_review_attempt,
)


CANDIDATE_SHA = "a" * 40
RESOLUTION_SHA = "b" * 64
PRIOR_SHA = "c" * 64


def write_attempt(root: Path, **overrides: object) -> ConflictReviewAttemptArtifact:
    values: dict[str, object] = {
        "candidate_sha": CANDIDATE_SHA,
        "resolution_record_sha256": RESOLUTION_SHA,
        "resolver_backend": "codex",
        "reviewer_backend": "claude",
        "attempt": 1,
        "review_kind": "initial",
        "verdict": "major",
        "findings": ("kanban invariant changed",),
        "reviewed_at": "2026-07-13T10:00:00Z",
        "prior_artifact_sha256": None,
    }
    values.update(overrides)
    return write_conflict_review_attempt(root, **values)


def test_review_attempt_round_trip_is_candidate_and_resolution_bound(tmp_path: Path):
    artifact = write_attempt(tmp_path)

    loaded = ConflictReviewAttemptArtifact.load(artifact.path)

    assert loaded == artifact
    assert artifact.relative_path == f"conflict-reviews/review-{artifact.sha256}.json"
    assert artifact.candidate_sha == CANDIDATE_SHA
    assert artifact.resolution_record_sha256 == RESOLUTION_SHA
    if os.name != "nt":
        assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o400


def test_confirmation_attempt_is_chained_to_prior_artifact(tmp_path: Path):
    artifact = write_attempt(
        tmp_path,
        attempt=2,
        review_kind="major_confirmation",
        verdict="green",
        findings=(),
        prior_artifact_sha256=PRIOR_SHA,
    )

    assert artifact.attempt == 2
    assert artifact.prior_artifact_sha256 == PRIOR_SHA


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"candidate_sha": "A" * 40}, "candidate SHA"),
        ({"candidate_sha": "a" * 39}, "candidate SHA"),
        ({"resolution_record_sha256": "B" * 64}, "resolution digest"),
        ({"attempt": 0}, "attempt"),
        ({"attempt": 2}, "attempt and kind"),
        ({"review_kind": "major_confirmation"}, "attempt and kind"),
        ({"verdict": "green"}, "green review"),
        ({"findings": ()}, "major review"),
        ({"findings": ("",)}, "findings"),
        ({"prior_artifact_sha256": PRIOR_SHA}, "initial review"),
        (
            {
                "attempt": 2,
                "review_kind": "major_confirmation",
                "prior_artifact_sha256": None,
            },
            "prior artifact",
        ),
        ({"resolver_backend": " codex"}, "backend"),
        ({"reviewer_backend": "codex"}, "independent"),
        ({"reviewed_at": "2026-07-13 10:00:00"}, "timestamp"),
    ],
)
def test_writer_rejects_invalid_review_attempts(
    tmp_path: Path, overrides: dict[str, object], message: str
):
    with pytest.raises(ConflictReviewEvidenceError, match=message):
        write_attempt(tmp_path, **overrides)


def test_review_attempt_bytes_are_canonical(tmp_path: Path):
    artifact = write_attempt(tmp_path)
    payload = json.loads(artifact.path.read_text(encoding="utf-8"))
    expected = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")

    assert artifact.path.read_bytes() == expected
    assert artifact.sha256 == hashlib.sha256(expected).hexdigest()


def test_load_rejects_noncanonical_bytes(tmp_path: Path):
    artifact = write_attempt(tmp_path)
    payload = json.loads(artifact.path.read_text(encoding="utf-8"))
    content = json.dumps(payload, indent=2).encode("utf-8") + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    path = artifact.path.parent / f"review-{digest}.json"
    path.write_bytes(content)
    path.chmod(0o400)

    with pytest.raises(ConflictReviewEvidenceError, match="canonical"):
        ConflictReviewAttemptArtifact.load(path)


def test_load_rejects_filename_digest_mismatch(tmp_path: Path):
    artifact = write_attempt(tmp_path)
    artifact.path.chmod(0o600)
    artifact.path.write_bytes(artifact.path.read_bytes() + b" ")
    artifact.path.chmod(0o400)

    with pytest.raises(ConflictReviewEvidenceError, match="digest"):
        ConflictReviewAttemptArtifact.load(artifact.path)


def test_load_rejects_artifact_outside_direct_review_directory(tmp_path: Path):
    artifact = write_attempt(tmp_path)
    outside = tmp_path / artifact.path.name
    outside.write_bytes(artifact.path.read_bytes())
    outside.chmod(0o400)

    with pytest.raises(ConflictReviewEvidenceError, match="conflict-reviews"):
        ConflictReviewAttemptArtifact.load(outside)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_load_rejects_symlink_artifact(tmp_path: Path):
    artifact = write_attempt(tmp_path)
    link = artifact.path.parent / f"review-{'d' * 64}.json"
    link.symlink_to(artifact.path)

    with pytest.raises(ConflictReviewEvidenceError, match="regular file"):
        ConflictReviewAttemptArtifact.load(link)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_writer_rejects_symlink_review_directory(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "conflict-reviews").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConflictReviewEvidenceError, match="directory"):
        write_attempt(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_writer_rejects_symlink_receipt_root(tmp_path: Path):
    actual = tmp_path / "actual-receipts"
    actual.mkdir()
    linked = tmp_path / "linked-receipts"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ConflictReviewEvidenceError, match="receipt root"):
        write_attempt(linked)


def test_loader_rejects_artifact_outside_configured_receipt_root(tmp_path: Path):
    artifact = write_attempt(tmp_path / "actual")

    with pytest.raises(ConflictReviewEvidenceError, match="receipt root"):
        ConflictReviewAttemptArtifact.load(
            artifact.path,
            receipt_root=tmp_path / "different",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are not authoritative")
def test_writer_rejects_broad_review_directory_mode(tmp_path: Path):
    directory = tmp_path / "conflict-reviews"
    directory.mkdir(mode=0o755)

    with pytest.raises(ConflictReviewEvidenceError, match="0700"):
        write_attempt(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are not authoritative")
def test_existing_artifact_must_remain_valid(tmp_path: Path):
    artifact = write_attempt(tmp_path)
    artifact.path.chmod(0o600)

    with pytest.raises(ConflictReviewEvidenceError, match="0400"):
        write_attempt(tmp_path)
