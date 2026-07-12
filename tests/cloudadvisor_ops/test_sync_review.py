from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync import SyncClassification
from ops.cloudadvisor.hermes_ops.sync_review import (
    ClaudeConflictReviewer,
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


class ReviewerRunner:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        self.calls.append((tuple(argv), Path(cwd), timeout))
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, CANDIDATE_SHA + "\n", "")
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"structured_output": self.payload}),
            "",
        )


def test_claude_reviewer_returns_exact_structured_receipt(tmp_path: Path):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    record = resolution_record(worktree)
    runner = ReviewerRunner({"verdict": "green", "findings": []})
    reviewer = ClaudeConflictReviewer(
        executable=Path("/Users/cloudadvisor/.local/bin/claude"),
        runner=runner,
        resolver_backend="codex",
    )

    review = reviewer.review(
        candidate_sha=CANDIDATE_SHA,
        worktree=worktree,
        resolution_record=record,
    )

    assert review.candidate_sha == CANDIDATE_SHA
    assert review.resolver_backend == "codex"
    assert review.reviewer_backend == "claude"
    assert review.verdict == "green"
    assert review.findings == ()
    claude_command = next(
        call[0] for call in runner.calls if call[0][0].endswith("/claude")
    )
    assert claude_command[0] == "/Users/cloudadvisor/.local/bin/claude"
    assert "--print" in claude_command
    assert "--json-schema" in claude_command
    assert "--permission-mode" in claude_command
    assert "plan" in claude_command
    assert CANDIDATE_SHA in claude_command[-1]


def test_claude_reviewer_rejects_changed_head(tmp_path: Path):
    class ChangedHeadRunner(ReviewerRunner):
        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            completed = super().run(argv, cwd, timeout)
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\n", "")
            return completed

    worktree = tmp_path / "candidate"
    worktree.mkdir()
    reviewer = ClaudeConflictReviewer(
        executable=Path("/Users/cloudadvisor/.local/bin/claude"),
        runner=ChangedHeadRunner({"verdict": "green", "findings": []}),
        resolver_backend="codex",
    )

    with pytest.raises(ConflictReviewError, match="candidate SHA"):
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=resolution_record(worktree),
        )
