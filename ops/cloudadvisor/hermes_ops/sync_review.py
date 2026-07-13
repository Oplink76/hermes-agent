"""Independent exact-head review contract for resolved sync conflicts."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from .command import CommandRunner
from .sync import SyncClassification, is_canonical_backend_executable
from .sync_resolution import ResolutionRecordArtifact, ResolutionRecordError
from .sync_review_evidence import (
    ConflictReviewEvidenceError,
    write_conflict_review_attempt,
)


class ConflictReviewError(ValueError):
    """The conflict review does not prove minor-resolution eligibility."""

    def __init__(self, message: str, *, details_artifact: str | None = None):
        super().__init__(message)
        self.details_artifact = details_artifact


class ConfirmedMajorReviewError(ConflictReviewError):
    """Two exact Claude reviews confirmed actionable major findings."""


@dataclass(frozen=True)
class ConflictReviewReceipt:
    candidate_sha: str
    resolver_backend: str
    reviewer_backend: str
    verdict: Literal["green", "major"]
    findings: tuple[str, ...]
    reviewed_at: str
    resolution_record_sha256: str
    evidence_artifact: str | None = None


class IndependentConflictReviewer(Protocol):
    def review(
        self,
        *,
        candidate_sha: str,
        worktree: Path,
        resolution_record: Path,
    ) -> ConflictReviewReceipt: ...


_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["green", "major"]},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "findings"],
}


@dataclass(frozen=True)
class ClaudeConflictReviewer:
    """Run an independent, non-mutating exact-head Claude review."""

    executable: Path
    runner: CommandRunner
    resolver_backend: str
    evidence_dir: Path
    reviewer_backend: str = "claude"

    def __post_init__(self) -> None:
        if not is_canonical_backend_executable(self.executable, "claude"):
            raise ValueError("conflict reviewer must use the Claude executable")
        if (
            not self.resolver_backend.strip()
            or self.resolver_backend != self.resolver_backend.strip()
            or not self.reviewer_backend.strip()
            or self.reviewer_backend != self.reviewer_backend.strip()
            or self.resolver_backend != "codex"
            or self.reviewer_backend != "claude"
        ):
            raise ValueError("conflict reviewer backends must be codex and claude")

    def _head(self, worktree: Path) -> str:
        completed = self.runner.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, timeout=300
        )
        head = (completed.stdout or "").strip()
        if completed.returncode != 0 or _FULL_SHA.fullmatch(head) is None:
            raise ConflictReviewError("conflict review candidate SHA is unavailable")
        return head

    def _status(self, worktree: Path) -> str:
        completed = self.runner.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            timeout=300,
        )
        if completed.returncode != 0:
            raise ConflictReviewError("conflict review worktree status is unavailable")
        return completed.stdout or ""

    def _review_once(
        self,
        prompt: str,
        *,
        worktree: Path,
        candidate_sha: str,
        resolution: ResolutionRecordArtifact,
        status_before: str,
    ) -> ConflictReviewReceipt:
        command = [
            str(self.executable),
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_REVIEW_SCHEMA, sort_keys=True, separators=(",", ":")),
            "--permission-mode",
            "plan",
            "--no-session-persistence",
            "--safe-mode",
            "--add-dir",
            str(resolution.path.parent),
            "--",
            prompt,
        ]
        completed = self.runner.run(command, cwd=worktree, timeout=1800)
        if completed.returncode != 0:
            raise ConflictReviewError("independent Claude review failed")
        try:
            envelope = json.loads(completed.stdout or "")
            payload = envelope["structured_output"]
            if not isinstance(payload, dict) or set(payload) != {
                "verdict",
                "findings",
            }:
                raise TypeError
            verdict = payload["verdict"]
            findings = payload["findings"]
            if verdict not in {"green", "major"} or not isinstance(findings, list):
                raise TypeError
            if not all(
                isinstance(finding, str)
                and finding
                and finding == finding.strip()
                for finding in findings
            ):
                raise TypeError
            if (verdict == "green" and findings) or (
                verdict == "major" and not findings
            ):
                raise TypeError
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ConflictReviewError(
                "independent Claude review returned invalid structured output"
            ) from exc
        if self._head(worktree) != candidate_sha:
            raise ConflictReviewError("conflict review candidate SHA changed")
        if self._status(worktree) != status_before:
            raise ConflictReviewError("independent review modified the worktree")
        return ConflictReviewReceipt(
            candidate_sha=candidate_sha,
            resolver_backend=self.resolver_backend,
            reviewer_backend=self.reviewer_backend,
            verdict=verdict,
            findings=tuple(findings),
            reviewed_at=datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            resolution_record_sha256=resolution.sha256,
        )

    def review(
        self,
        *,
        candidate_sha: str,
        worktree: Path,
        resolution_record: Path,
    ) -> ConflictReviewReceipt:
        unresolved_record = Path(os.path.abspath(resolution_record))
        expected_dir = Path(os.path.abspath(self.evidence_dir))
        if unresolved_record.parent != expected_dir:
            raise ConflictReviewError(
                "resolution record is not in the expected evidence directory"
            )
        try:
            record_meta = unresolved_record.lstat()
            directory_meta = expected_dir.lstat()
        except OSError as exc:
            raise ConflictReviewError("resolution evidence path is unavailable") from exc
        if stat.S_ISLNK(record_meta.st_mode) or not stat.S_ISREG(record_meta.st_mode):
            raise ConflictReviewError(
                "resolution record must be a direct regular file"
            )
        if stat.S_ISLNK(directory_meta.st_mode) or not stat.S_ISDIR(
            directory_meta.st_mode
        ):
            raise ConflictReviewError("resolution evidence directory is invalid")
        worktree = Path(worktree).resolve(strict=True)
        resolution_record = unresolved_record
        try:
            resolution = ResolutionRecordArtifact.load(resolution_record)
        except ResolutionRecordError as exc:
            raise ConflictReviewError("resolution record artifact is invalid") from exc
        if resolution.candidate_sha != candidate_sha:
            raise ConflictReviewError("resolution record candidate SHA is not exact")
        if self._head(worktree) != candidate_sha:
            raise ConflictReviewError("conflict review candidate SHA is not exact")
        status_before = self._status(worktree)
        prompt = (
            "Independently review this resolved upstream-sync merge at exact HEAD "
            f"{candidate_sha}. Read {resolution_record}, inspect the Git diff and "
            "verify every recorded decision preserves fork behavior, branch protection, "
            "exact-SHA deployment/rollback, credentials/auth, Kanban governance, database "
            "preservation, Trading research-only dormancy, profile isolation, and Second "
            "Brain raw-source immutability. Do not modify files, commit, or push. Return "
            "green only when the resolution is mechanically safe with zero findings; "
            "otherwise return major and concise findings."
        )
        initial = self._review_once(
            prompt,
            worktree=worktree,
            candidate_sha=candidate_sha,
            resolution=resolution,
            status_before=status_before,
        )
        try:
            initial_artifact = write_conflict_review_attempt(
                self.evidence_dir.parent,
                candidate_sha=candidate_sha,
                resolution_record_sha256=resolution.sha256,
                resolver_backend=self.resolver_backend,
                reviewer_backend=self.reviewer_backend,
                attempt=1,
                review_kind="initial",
                verdict=initial.verdict,
                findings=initial.findings,
                reviewed_at=initial.reviewed_at,
            )
        except ConflictReviewEvidenceError as exc:
            raise ConflictReviewError(
                "independent Claude review evidence could not be published"
            ) from exc
        if initial.verdict == "green":
            return replace(
                initial, evidence_artifact=initial_artifact.relative_path
            )

        confirmation_prompt = (
            "Confirm or disprove only these findings from an independent review of "
            f"exact HEAD {candidate_sha}: {json.dumps(list(initial.findings))}. "
            f"Read immutable resolution record {resolution_record} with SHA-256 "
            f"{resolution.sha256}. Return major only for findings directly supported "
            "by the exact code and recorded conflict decisions; otherwise return green "
            "with zero findings. Do not modify files, commit, push, or change remotes."
        )
        try:
            confirmation = self._review_once(
                confirmation_prompt,
                worktree=worktree,
                candidate_sha=candidate_sha,
                resolution=resolution,
                status_before=status_before,
            )
            confirmation_artifact = write_conflict_review_attempt(
                self.evidence_dir.parent,
                candidate_sha=candidate_sha,
                resolution_record_sha256=resolution.sha256,
                resolver_backend=self.resolver_backend,
                reviewer_backend=self.reviewer_backend,
                attempt=2,
                review_kind="major_confirmation",
                verdict=confirmation.verdict,
                findings=confirmation.findings,
                reviewed_at=confirmation.reviewed_at,
                prior_artifact_sha256=initial_artifact.sha256,
            )
        except (ConflictReviewError, ConflictReviewEvidenceError) as exc:
            raise ConflictReviewError(
                str(exc), details_artifact=initial_artifact.relative_path
            ) from exc
        return replace(
            confirmation, evidence_artifact=confirmation_artifact.relative_path
        )


def _resolution_record_paths(path: Path) -> tuple[str, ...]:
    try:
        artifact = ResolutionRecordArtifact.load(Path(path))
    except ResolutionRecordError as exc:
        raise ConflictReviewError("resolution record is missing or invalid") from exc
    return tuple(conflict["path"] for conflict in artifact.conflicts)


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
    try:
        artifact = ResolutionRecordArtifact.load(resolution_record)
    except ResolutionRecordError as exc:
        raise ConflictReviewError("resolution record is missing or invalid") from exc
    if (
        artifact.candidate_sha != candidate_sha
        or receipt.resolution_record_sha256 != artifact.sha256
    ):
        raise ConflictReviewError("resolution record digest is not exact")
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
