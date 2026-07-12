"""Immutable eligibility evidence for automatic upstream synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .sync import CheckResult, SyncClassification, SyncResult
from .sync_github import SyncPullRequestEvidence
from .sync_review import ConflictReviewReceipt


SCHEMA_VERSION = 1
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ARTIFACT_NAME = re.compile(
    r"sync-(?:premerge|merged)-(?P<digest>[0-9a-f]{64})\.json\Z"
)
_REQUIRED_LOCAL_CHECKS = frozenset(
    {"diff_check", "unmerged_index", "conflict_markers", "compileall", "tests"}
)


class SyncReceiptError(ValueError):
    """The supplied evidence cannot authorize an automatic sync action."""


@dataclass(frozen=True)
class SyncEligibilityReceipt:
    schema_version: int
    repo_slug: str
    base_sha: str
    upstream_sha: str
    candidate_sha: str
    classification: str
    local_checks: tuple[CheckResult, ...]
    review: ConflictReviewReceipt | None
    pr_number: int
    pr_head_sha: str
    required_check: str
    required_check_conclusion: str
    merge_sha: str | None
    eligible: bool
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "repo_slug": self.repo_slug,
            "base_sha": self.base_sha,
            "upstream_sha": self.upstream_sha,
            "candidate_sha": self.candidate_sha,
            "classification": self.classification,
            "local_checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.local_checks
            ],
            "review": (
                None
                if self.review is None
                else {
                    "candidate_sha": self.review.candidate_sha,
                    "resolver_backend": self.review.resolver_backend,
                    "reviewer_backend": self.review.reviewer_backend,
                    "verdict": self.review.verdict,
                    "findings": list(self.review.findings),
                    "reviewed_at": self.review.reviewed_at,
                }
            ),
            "pr_number": self.pr_number,
            "pr_head_sha": self.pr_head_sha,
            "required_check": self.required_check,
            "required_check_conclusion": self.required_check_conclusion,
            "merge_sha": self.merge_sha,
            "eligible": self.eligible,
            "created_at": self.created_at,
        }

    @classmethod
    def load(cls, path: Path) -> "SyncEligibilityReceipt":
        path = Path(path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SyncReceiptError("sync receipt is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SyncReceiptError("sync receipt must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o400:
            raise SyncReceiptError("sync receipt must be read-only mode 0400")
        name_match = _ARTIFACT_NAME.fullmatch(path.name)
        if name_match is None:
            raise SyncReceiptError("sync receipt filename has no trusted digest")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SyncReceiptError("sync receipt could not be read") from exc
        digest = hashlib.sha256(content).hexdigest()
        if digest != name_match.group("digest"):
            raise SyncReceiptError("sync receipt digest does not match its content")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncReceiptError("sync receipt JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise SyncReceiptError("sync receipt must contain a JSON object")
        expected_fields = {
            "schema_version",
            "repo_slug",
            "base_sha",
            "upstream_sha",
            "candidate_sha",
            "classification",
            "local_checks",
            "review",
            "pr_number",
            "pr_head_sha",
            "required_check",
            "required_check_conclusion",
            "merge_sha",
            "eligible",
            "created_at",
        }
        unknown = set(payload) - expected_fields
        missing = expected_fields - set(payload)
        if unknown:
            raise SyncReceiptError(f"sync receipt has unknown fields: {sorted(unknown)}")
        if missing:
            raise SyncReceiptError(f"sync receipt is missing fields: {sorted(missing)}")
        if _canonical_json(payload) != content:
            raise SyncReceiptError("sync receipt JSON is not canonical")

        local_checks = _load_checks(payload["local_checks"])
        review = _load_review(payload["review"])
        if type(payload["eligible"]) is not bool:
            raise SyncReceiptError("sync receipt eligible field must be boolean")
        receipt = cls(
            schema_version=payload["schema_version"],
            repo_slug=payload["repo_slug"],
            base_sha=payload["base_sha"],
            upstream_sha=payload["upstream_sha"],
            candidate_sha=payload["candidate_sha"],
            classification=payload["classification"],
            local_checks=local_checks,
            review=review,
            pr_number=payload["pr_number"],
            pr_head_sha=payload["pr_head_sha"],
            required_check=payload["required_check"],
            required_check_conclusion=payload["required_check_conclusion"],
            merge_sha=payload["merge_sha"],
            eligible=False,
            created_at=payload["created_at"],
        )
        _validate_receipt(receipt)
        return replace(receipt, eligible=True)


@dataclass(frozen=True)
class SyncReceiptArtifact:
    path: Path
    sha256: str


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _is_full_sha(value: object) -> bool:
    return isinstance(value, str) and _FULL_SHA.fullmatch(value) is not None


def _load_checks(value: object) -> tuple[CheckResult, ...]:
    if not isinstance(value, list):
        raise SyncReceiptError("sync receipt local checks are invalid")
    checks: list[CheckResult] = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "status", "detail"}:
            raise SyncReceiptError("sync receipt local checks are invalid")
        if not all(isinstance(row[field], str) for field in row):
            raise SyncReceiptError("sync receipt local checks are invalid")
        checks.append(
            CheckResult(
                name=row["name"], status=row["status"], detail=row["detail"]
            )
        )
    return tuple(checks)


def _load_review(value: object) -> ConflictReviewReceipt | None:
    if value is None:
        return None
    fields = {
        "candidate_sha",
        "resolver_backend",
        "reviewer_backend",
        "verdict",
        "findings",
        "reviewed_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SyncReceiptError("sync receipt conflict review is invalid")
    findings = value["findings"]
    if not isinstance(findings, list) or not all(
        isinstance(finding, str) for finding in findings
    ):
        raise SyncReceiptError("sync receipt conflict review is invalid")
    string_fields = fields - {"findings"}
    if not all(isinstance(value[field], str) for field in string_fields):
        raise SyncReceiptError("sync receipt conflict review is invalid")
    return ConflictReviewReceipt(
        candidate_sha=value["candidate_sha"],
        resolver_backend=value["resolver_backend"],
        reviewer_backend=value["reviewer_backend"],
        verdict=value["verdict"],
        findings=tuple(findings),
        reviewed_at=value["reviewed_at"],
    )


def _validate_review(
    review: ConflictReviewReceipt | None,
    *,
    candidate_sha: str,
) -> None:
    if review is None:
        raise SyncReceiptError("minor candidate requires independent review")
    if review.candidate_sha != candidate_sha:
        raise SyncReceiptError("independent review candidate SHA does not match")
    if (
        not review.resolver_backend
        or review.resolver_backend != review.resolver_backend.strip()
        or not review.reviewer_backend
        or review.reviewer_backend != review.reviewer_backend.strip()
        or review.resolver_backend.casefold() == review.reviewer_backend.casefold()
    ):
        raise SyncReceiptError("conflict review is not independent")
    if review.verdict != "green" or review.findings:
        raise SyncReceiptError("independent review is not green")
    if not review.reviewed_at:
        raise SyncReceiptError("independent review timestamp is missing")


def _validate_receipt(receipt: SyncEligibilityReceipt) -> None:
    if type(receipt.schema_version) is not int or receipt.schema_version != SCHEMA_VERSION:
        raise SyncReceiptError("sync receipt schema version is unsupported")
    if (
        not isinstance(receipt.repo_slug, str)
        or receipt.repo_slug.strip() != receipt.repo_slug
        or receipt.repo_slug.count("/") != 1
        or not all(receipt.repo_slug.split("/"))
    ):
        raise SyncReceiptError("sync receipt repository slug is invalid")
    if not all(
        _is_full_sha(value)
        for value in (receipt.base_sha, receipt.upstream_sha, receipt.candidate_sha)
    ):
        raise SyncReceiptError("sync receipt contains an invalid commit SHA")
    if receipt.pr_head_sha != receipt.candidate_sha:
        raise SyncReceiptError("sync receipt PR head SHA is not exact")
    if type(receipt.pr_number) is not int or receipt.pr_number < 1:
        raise SyncReceiptError("sync receipt PR number is invalid")
    if receipt.classification == SyncClassification.CLEAN.value:
        if receipt.review is not None:
            raise SyncReceiptError("clean sync receipt must not contain conflict review")
    elif receipt.classification == SyncClassification.MINOR_RESOLVED.value:
        _validate_review(receipt.review, candidate_sha=receipt.candidate_sha)
    else:
        raise SyncReceiptError("sync receipt classification is not eligible")
    names = [check.name for check in receipt.local_checks]
    if (
        len(names) != len(set(names))
        or set(names) != _REQUIRED_LOCAL_CHECKS
        or any(check.status != "passed" for check in receipt.local_checks)
        or any(
            not isinstance(check.name, str)
            or not isinstance(check.status, str)
            or not isinstance(check.detail, str)
            for check in receipt.local_checks
        )
    ):
        raise SyncReceiptError("sync receipt local checks are incomplete or not green")
    if not isinstance(receipt.required_check, str) or not receipt.required_check.strip():
        raise SyncReceiptError("sync receipt required check name is invalid")
    if receipt.required_check_conclusion != "success":
        raise SyncReceiptError("sync receipt required check is not green")
    if receipt.merge_sha is not None and not _is_full_sha(receipt.merge_sha):
        raise SyncReceiptError("sync receipt merge SHA is invalid")
    if not isinstance(receipt.created_at, str) or not receipt.created_at.endswith("Z"):
        raise SyncReceiptError("sync receipt creation timestamp is invalid")


def build_sync_receipt(
    candidate: SyncResult,
    evidence: SyncPullRequestEvidence,
    *,
    repo_slug: str,
    conflict_review: ConflictReviewReceipt | None = None,
    created_at: str | None = None,
) -> SyncEligibilityReceipt:
    """Validate exact pre-merge evidence and build an eligible receipt value."""
    if candidate.pr_number != evidence.number:
        raise SyncReceiptError("candidate PR number does not match evidence")
    if candidate.candidate_sha != evidence.head_sha:
        raise SyncReceiptError("candidate head SHA does not match PR head SHA")
    if candidate.base_sha != evidence.base_sha:
        raise SyncReceiptError("candidate base SHA does not match PR base SHA")
    if evidence.state != "open":
        raise SyncReceiptError("candidate pull request is not open")
    if evidence.merge_sha is not None:
        raise SyncReceiptError("pre-merge evidence unexpectedly has a merge SHA")
    classification = (
        candidate.classification.value
        if isinstance(candidate.classification, SyncClassification)
        else candidate.classification
    )
    receipt = SyncEligibilityReceipt(
        schema_version=SCHEMA_VERSION,
        repo_slug=repo_slug,
        base_sha=candidate.base_sha,
        upstream_sha=candidate.upstream_sha,
        candidate_sha=candidate.candidate_sha,
        classification=classification,
        local_checks=candidate.checks,
        review=conflict_review,
        pr_number=evidence.number,
        pr_head_sha=evidence.head_sha,
        required_check=evidence.required_check,
        required_check_conclusion=evidence.required_check_conclusion,
        merge_sha=None,
        eligible=False,
        created_at=created_at or _now(),
    )
    _validate_receipt(receipt)
    return replace(receipt, eligible=True)


def _write_receipt(
    receipt_root: Path,
    receipt: SyncEligibilityReceipt,
    *,
    phase: str,
) -> SyncReceiptArtifact:
    root = Path(receipt_root)
    root.mkdir(parents=True, exist_ok=True)
    content = _canonical_json(receipt.to_dict())
    digest = hashlib.sha256(content).hexdigest()
    target = root / f"sync-{phase}-{digest}.json"
    if target.exists():
        if target.read_bytes() != content:
            raise SyncReceiptError("existing sync receipt digest collision")
        SyncEligibilityReceipt.load(target)
        return SyncReceiptArtifact(target, digest)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sync-receipt-", suffix=".tmp", dir=root
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
            if target.read_bytes() != content:
                raise SyncReceiptError("existing sync receipt digest collision")
        SyncEligibilityReceipt.load(target)
    finally:
        temporary.unlink(missing_ok=True)
    return SyncReceiptArtifact(target, digest)


def write_sync_receipt(
    receipt_root: Path,
    candidate: SyncResult,
    evidence: SyncPullRequestEvidence,
    *,
    repo_slug: str,
    conflict_review: ConflictReviewReceipt | None = None,
    created_at: str | None = None,
) -> SyncReceiptArtifact:
    receipt = build_sync_receipt(
        candidate,
        evidence,
        repo_slug=repo_slug,
        conflict_review=conflict_review,
        created_at=created_at,
    )
    return _write_receipt(receipt_root, receipt, phase="premerge")


def finalize_sync_receipt(
    premerge_path: Path,
    *,
    merge_sha: str,
) -> SyncReceiptArtifact:
    """Create a separate merged receipt without modifying pre-merge evidence."""
    receipt = SyncEligibilityReceipt.load(premerge_path)
    if receipt.merge_sha is not None:
        raise SyncReceiptError("sync receipt is already finalized")
    finalized = replace(receipt, merge_sha=merge_sha, eligible=False, created_at=_now())
    _validate_receipt(finalized)
    return _write_receipt(
        Path(premerge_path).parent,
        replace(finalized, eligible=True),
        phase="merged",
    )
