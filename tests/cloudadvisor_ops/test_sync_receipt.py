from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync import (
    CheckResult,
    SyncClassification,
    SyncResult,
    SyncState,
)
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_receipt import (
    SyncEligibilityReceipt,
    SyncReceiptError,
    build_sync_receipt,
    finalize_sync_receipt,
    write_sync_receipt,
)
from ops.cloudadvisor.hermes_ops.sync_review import ConflictReviewReceipt


REPO = "Oplink76/hermes-agent"
BASE_SHA = "a" * 40
UPSTREAM_SHA = "b" * 40
CANDIDATE_SHA = "c" * 40
MERGE_SHA = "d" * 40
REQUIRED_CHECK = "All required checks pass"
LOCAL_CHECKS = tuple(
    CheckResult(name, "passed")
    for name in (
        "diff_check",
        "unmerged_index",
        "conflict_markers",
        "compileall",
        "tests",
    )
)


def clean_candidate(**overrides: object) -> SyncResult:
    values: dict[str, object] = {
        "state": SyncState.PR_UPDATED,
        "base_sha": BASE_SHA,
        "upstream_sha": UPSTREAM_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "pr_number": 7,
        "checks": LOCAL_CHECKS,
        "classification": SyncClassification.CLEAN,
    }
    values.update(overrides)
    return SyncResult(**values)


def green_pr(**overrides: object) -> SyncPullRequestEvidence:
    values: dict[str, object] = {
        "number": 7,
        "state": "open",
        "base_sha": BASE_SHA,
        "head_sha": CANDIDATE_SHA,
        "required_check": REQUIRED_CHECK,
        "required_check_conclusion": "success",
    }
    values.update(overrides)
    return SyncPullRequestEvidence(**values)


def green_review(**overrides: object) -> ConflictReviewReceipt:
    values: dict[str, object] = {
        "candidate_sha": CANDIDATE_SHA,
        "resolver_backend": "codex",
        "reviewer_backend": "claude",
        "verdict": "green",
        "findings": (),
        "reviewed_at": "2026-07-12T16:00:00Z",
    }
    values.update(overrides)
    return ConflictReviewReceipt(**values)


def _write_raw_artifact(root: Path, payload: dict[str, object]) -> Path:
    content = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = root / f"sync-premerge-{digest}.json"
    path.write_bytes(content)
    path.chmod(0o400)
    return path


def test_clean_green_candidate_is_eligible(tmp_path: Path):
    artifact = write_sync_receipt(
        tmp_path,
        clean_candidate(),
        green_pr(),
        repo_slug=REPO,
    )

    loaded = SyncEligibilityReceipt.load(artifact.path)

    assert loaded.eligible is True
    assert loaded.candidate_sha == CANDIDATE_SHA
    assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o400
    assert artifact.sha256 in artifact.path.name


def test_minor_candidate_without_review_is_rejected():
    candidate = clean_candidate(classification=SyncClassification.MINOR_RESOLVED)

    with pytest.raises(SyncReceiptError, match="independent review"):
        build_sync_receipt(
            candidate,
            green_pr(),
            repo_slug=REPO,
            conflict_review=None,
        )


def test_minor_candidate_requires_exact_green_independent_review():
    candidate = clean_candidate(classification=SyncClassification.MINOR_RESOLVED)

    receipt = build_sync_receipt(
        candidate,
        green_pr(),
        repo_slug=REPO,
        conflict_review=green_review(),
    )

    assert receipt.eligible is True
    assert receipt.review == green_review()


@pytest.mark.parametrize(
    ("candidate", "evidence", "message"),
    [
        (clean_candidate(candidate_sha="e" * 40), green_pr(), "head SHA"),
        (clean_candidate(pr_number=8), green_pr(), "PR number"),
        (clean_candidate(base_sha="e" * 40), green_pr(), "base SHA"),
    ],
)
def test_candidate_must_match_exact_pr_identity(candidate, evidence, message: str):
    with pytest.raises(SyncReceiptError, match=message):
        build_sync_receipt(candidate, evidence, repo_slug=REPO)


@pytest.mark.parametrize(
    "checks",
    [
        (),
        tuple(check for check in LOCAL_CHECKS if check.name != "compileall"),
        tuple(
            replace(check, status="failed") if check.name == "tests" else check
            for check in LOCAL_CHECKS
        ),
    ],
)
def test_all_prescribed_local_checks_are_required(checks):
    with pytest.raises(SyncReceiptError, match="local checks"):
        build_sync_receipt(
            clean_candidate(checks=checks),
            green_pr(),
            repo_slug=REPO,
        )


@pytest.mark.parametrize("conclusion", ["failure", "pending", "missing"])
def test_required_aggregate_check_must_be_green(conclusion: str):
    with pytest.raises(SyncReceiptError, match="required check"):
        build_sync_receipt(
            clean_candidate(),
            green_pr(required_check_conclusion=conclusion),
            repo_slug=REPO,
        )


@pytest.mark.parametrize(
    "classification",
    [SyncClassification.MAJOR, SyncClassification.MINOR_REVIEW_REQUIRED],
)
def test_unreviewed_or_major_classification_is_ineligible(classification):
    with pytest.raises(SyncReceiptError, match="classification"):
        build_sync_receipt(
            clean_candidate(classification=classification),
            green_pr(),
            repo_slug=REPO,
        )


def test_load_rejects_writable_receipt(tmp_path: Path):
    artifact = write_sync_receipt(
        tmp_path, clean_candidate(), green_pr(), repo_slug=REPO
    )
    artifact.path.chmod(0o600)

    with pytest.raises(SyncReceiptError, match="read-only"):
        SyncEligibilityReceipt.load(artifact.path)


def test_load_rejects_content_tampering_even_after_mode_is_restored(tmp_path: Path):
    artifact = write_sync_receipt(
        tmp_path, clean_candidate(), green_pr(), repo_slug=REPO
    )
    artifact.path.chmod(0o600)
    artifact.path.write_bytes(
        artifact.path.read_bytes().replace(b'"eligible":true', b'"eligible":false')
    )
    artifact.path.chmod(0o400)

    with pytest.raises(SyncReceiptError, match="digest"):
        SyncEligibilityReceipt.load(artifact.path)


def test_load_rejects_unknown_fields(tmp_path: Path):
    receipt = build_sync_receipt(clean_candidate(), green_pr(), repo_slug=REPO)
    payload = receipt.to_dict()
    payload["unexpected_authority"] = True
    path = _write_raw_artifact(tmp_path, payload)

    with pytest.raises(SyncReceiptError, match="unknown fields"):
        SyncEligibilityReceipt.load(path)


def test_load_recomputes_eligibility_instead_of_trusting_stored_boolean(
    tmp_path: Path,
):
    receipt = build_sync_receipt(clean_candidate(), green_pr(), repo_slug=REPO)
    payload = receipt.to_dict()
    payload["eligible"] = False
    path = _write_raw_artifact(tmp_path, payload)

    assert SyncEligibilityReceipt.load(path).eligible is True


def test_finalize_creates_new_immutable_artifact_without_rewriting_premerge(
    tmp_path: Path,
):
    premerge = write_sync_receipt(
        tmp_path, clean_candidate(), green_pr(), repo_slug=REPO
    )
    before = premerge.path.read_bytes()

    merged = finalize_sync_receipt(premerge.path, merge_sha=MERGE_SHA)

    assert merged.path != premerge.path
    assert premerge.path.read_bytes() == before
    assert SyncEligibilityReceipt.load(premerge.path).merge_sha is None
    assert SyncEligibilityReceipt.load(merged.path).merge_sha == MERGE_SHA
    assert stat.S_IMODE(merged.path.stat().st_mode) == 0o400


def test_receipt_bytes_are_canonical_json(tmp_path: Path):
    artifact = write_sync_receipt(
        tmp_path, clean_candidate(), green_pr(), repo_slug=REPO
    )
    payload = json.loads(artifact.path.read_text(encoding="utf-8"))
    expected = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )

    assert artifact.path.read_text(encoding="utf-8") == expected
    assert artifact.sha256 == hashlib.sha256(expected.encode("utf-8")).hexdigest()
