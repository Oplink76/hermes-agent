from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync import SyncClassification
from ops.cloudadvisor.hermes_ops.sync import SyncResult, SyncState
from ops.cloudadvisor.hermes_ops.sync_resolution import (
    ResolutionRecordError,
    freeze_resolution_record,
)
from ops.cloudadvisor.hermes_ops.sync_review import (
    ClaudeConflictReviewer,
    ConflictReviewError,
    ConflictReviewReceipt,
    validate_conflict_review,
)
from ops.cloudadvisor.hermes_ops.sync_review_evidence import (
    ConflictReviewAttemptArtifact,
    ConflictReviewEvidenceError,
    write_conflict_review_attempt,
)


CANDIDATE_SHA = "a" * 40


def resolution_record(
    tmp_path: Path,
    *,
    complete: bool = True,
    paths: tuple[str, ...] = ("gateway/run.py",),
) -> Path:
    path = tmp_path / "resolution.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    decision = "preserve fork behavior" if complete else ""
    conflicts = [
        {"path": conflict_path, "decision": decision} for conflict_path in paths
    ]
    path.write_text(
        json.dumps(
            {
                "conflicts": conflicts,
                "strategy": "preserve_fork_behavior",
            }
        ),
        encoding="utf-8",
    )
    return path


def receipt(**overrides):
    values = {
        "candidate_sha": CANDIDATE_SHA,
        "resolver_backend": "codex",
        "reviewer_backend": "claude",
        "verdict": "green",
        "findings": (),
        "reviewed_at": "2026-07-12T16:00:00Z",
        "resolution_record_sha256": "d" * 64,
        "evidence_artifacts": (),
    }
    values.update(overrides)
    return ConflictReviewReceipt(**values)


def frozen_record(
    tmp_path: Path,
    *,
    complete: bool = True,
    paths: tuple[str, ...] = ("gateway/run.py",),
) -> Path:
    evidence = tmp_path / ".git" / "hermes-sync-evidence"
    raw = resolution_record(evidence, complete=complete, paths=paths)
    candidate = SyncResult(
        state=SyncState.PR_UPDATED,
        candidate_sha=CANDIDATE_SHA,
        conflicted_files=paths,
        resolution_record=raw,
        resolution_evidence_dir=evidence,
        resolution_strategy="preserve_fork_behavior",
    )
    return freeze_resolution_record(tmp_path / "receipts", candidate).path


def validate(tmp_path: Path, review):
    record = frozen_record(tmp_path)
    review = receipt(
        **{
            **review.__dict__,
            "resolution_record_sha256": record.stem.removeprefix("resolution-"),
        }
    )
    return validate_conflict_review(
        review,
        candidate_sha=CANDIDATE_SHA,
        resolver_backend="codex",
        resolution_record=record,
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
    with pytest.raises(ResolutionRecordError, match="incomplete"):
        frozen_record(tmp_path, complete=False)


def test_resolution_record_rejects_missing_conflicted_file(tmp_path: Path):
    record = frozen_record(tmp_path)
    review = receipt(
        resolution_record_sha256=record.stem.removeprefix("resolution-")
    )

    with pytest.raises(ConflictReviewError, match="conflicted files"):
        validate_conflict_review(
            review,
            candidate_sha=CANDIDATE_SHA,
            resolver_backend="codex",
            resolution_record=record,
            conflicted_files=("gateway/run.py", "hermes_cli/kanban.py"),
        )


def test_resolution_record_rejects_extra_conflicted_file(tmp_path: Path):
    record = frozen_record(
        tmp_path,
        paths=("gateway/run.py", "hermes_cli/kanban.py"),
    )
    review = receipt(
        resolution_record_sha256=record.stem.removeprefix("resolution-")
    )

    with pytest.raises(ConflictReviewError, match="conflicted files"):
        validate_conflict_review(
            review,
            candidate_sha=CANDIDATE_SHA,
            resolver_backend="codex",
            resolution_record=record,
            conflicted_files=("gateway/run.py",),
        )


def test_resolution_record_rejects_duplicate_conflicted_file(tmp_path: Path):
    with pytest.raises(ResolutionRecordError, match="duplicate"):
        frozen_record(
            tmp_path,
            paths=("gateway/run.py", "gateway/run.py"),
        )


def test_green_review_requires_zero_findings(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="findings"):
        validate(tmp_path, receipt(findings=("guard behavior changed",)))


def test_major_review_fails_closed(tmp_path: Path):
    record = frozen_record(tmp_path)
    resolution_digest = record.stem.removeprefix("resolution-")
    root = record.parent.parent
    initial = write_conflict_review_attempt(
        root,
        candidate_sha=CANDIDATE_SHA,
        resolution_record_sha256=resolution_digest,
        resolver_backend="codex",
        reviewer_backend="claude",
        attempt=1,
        review_kind="initial",
        verdict="major",
        findings=("product judgment required",),
        reviewed_at="2026-07-12T15:59:00Z",
    )
    confirmation = write_conflict_review_attempt(
        root,
        candidate_sha=CANDIDATE_SHA,
        resolution_record_sha256=resolution_digest,
        resolver_backend="codex",
        reviewer_backend="claude",
        attempt=2,
        review_kind="major_confirmation",
        verdict="major",
        findings=("product judgment required",),
        reviewed_at="2026-07-12T16:00:00Z",
        prior_artifact_sha256=initial.sha256,
    )
    review = receipt(
        verdict="major",
        findings=("product judgment required",),
        resolution_record_sha256=resolution_digest,
        evidence_artifacts=(initial.relative_path, confirmation.relative_path),
    )

    result = validate_conflict_review(
        review,
        candidate_sha=CANDIDATE_SHA,
        resolver_backend="codex",
        resolution_record=record,
        conflicted_files=("gateway/run.py",),
    )

    assert result is SyncClassification.MAJOR


def test_major_review_requires_findings_at_protocol_boundary(tmp_path: Path):
    with pytest.raises(ConflictReviewError, match="findings"):
        validate(tmp_path, receipt(verdict="major", findings=()))


@pytest.mark.parametrize(
    "evidence_artifacts",
    [(), ("../review.json", "conflict-reviews/review-" + "e" * 64 + ".json")],
)
def test_major_review_requires_two_safe_evidence_artifacts(
    tmp_path: Path,
    evidence_artifacts: tuple[str, ...],
):
    with pytest.raises(ConflictReviewError, match="evidence"):
        validate(
            tmp_path,
            receipt(
                verdict="major",
                findings=("product judgment required",),
                evidence_artifacts=evidence_artifacts,
            ),
        )


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


class SequenceReviewerRunner(ReviewerRunner):
    def __init__(self, responses: list[object]):
        super().__init__({})
        self.responses = iter(responses)

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        if Path(argv[0]).name.startswith("claude"):
            self.calls.append((tuple(argv), Path(cwd), timeout))
            response = next(self.responses)
            if isinstance(response, subprocess.CompletedProcess):
                return response
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"structured_output": response}),
                "",
            )
        return super().run(argv, cwd, timeout)


def reviewer_fixture(tmp_path: Path, runner: ReviewerRunner):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    record = frozen_record(tmp_path)
    reviewer = ClaudeConflictReviewer(
        executable=tmp_path / "bin" / ("claude.exe" if os.name == "nt" else "claude"),
        runner=runner,
        resolver_backend="codex",
        evidence_dir=record.parent,
    )
    return reviewer, worktree, record


def claude_calls(runner: ReviewerRunner):
    return [
        call for call in runner.calls if Path(call[0][0]).name.startswith("claude")
    ]


def test_claude_reviewer_returns_exact_structured_receipt(tmp_path: Path):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    record = frozen_record(tmp_path)
    runner = ReviewerRunner({"verdict": "green", "findings": []})
    reviewer = ClaudeConflictReviewer(
        executable=tmp_path / "bin" / ("claude.exe" if os.name == "nt" else "claude"),
        runner=runner,
        resolver_backend="codex",
        evidence_dir=record.parent,
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
    assert len(review.evidence_artifacts) == 1
    assert review.evidence_artifact == review.evidence_artifacts[-1]
    assert len(claude_calls(runner)) == 1
    artifact = ConflictReviewAttemptArtifact.load(
        record.parent.parent / review.evidence_artifact
    )
    assert artifact.attempt == 1
    assert artifact.verdict == "green"
    claude_command = next(
        call[0] for call in runner.calls if Path(call[0][0]).name.startswith("claude")
    )
    assert claude_command[0] == str(reviewer.executable)
    assert "--print" in claude_command
    assert "--json-schema" in claude_command
    assert "--permission-mode" in claude_command
    assert "plan" in claude_command
    assert claude_command[-2] == "--"
    assert CANDIDATE_SHA in claude_command[-1]


def test_initial_major_confirmation_green_continues(tmp_path: Path):
    runner = SequenceReviewerRunner(
        [
            {"verdict": "major", "findings": ["possible kanban regression"]},
            {"verdict": "green", "findings": []},
        ]
    )
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    review = reviewer.review(
        candidate_sha=CANDIDATE_SHA,
        worktree=worktree,
        resolution_record=record,
    )

    assert review.verdict == "green"
    assert review.findings == ()
    assert len(review.evidence_artifacts) == 2
    assert review.evidence_artifact == review.evidence_artifacts[-1]
    assert len(claude_calls(runner)) == 2
    final_artifact = ConflictReviewAttemptArtifact.load(
        record.parent.parent / review.evidence_artifact
    )
    assert final_artifact.attempt == 2
    assert final_artifact.verdict == "green"
    initial_artifact = ConflictReviewAttemptArtifact.load(
        record.parent.parent / review.evidence_artifacts[0]
    )
    assert initial_artifact.sha256 == final_artifact.prior_artifact_sha256
    assert initial_artifact.verdict == "major"
    assert initial_artifact.findings == ("possible kanban regression",)


def test_initial_major_confirmation_major_stays_major(tmp_path: Path):
    runner = SequenceReviewerRunner(
        [
            {"verdict": "major", "findings": ["kanban gate removed"]},
            {
                "verdict": "major",
                "findings": ["kanban gate is absent at exact HEAD"],
            },
        ]
    )
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    review = reviewer.review(
        candidate_sha=CANDIDATE_SHA,
        worktree=worktree,
        resolution_record=record,
    )

    assert review.verdict == "major"
    assert review.findings == ("kanban gate is absent at exact HEAD",)
    assert len(review.evidence_artifacts) == 2
    assert review.evidence_artifact == review.evidence_artifacts[-1]
    assert len(claude_calls(runner)) == 2
    artifact = ConflictReviewAttemptArtifact.load(
        record.parent.parent / review.evidence_artifact
    )
    assert artifact.attempt == 2
    assert artifact.verdict == "major"


def test_initial_major_requires_findings(tmp_path: Path):
    runner = SequenceReviewerRunner([{"verdict": "major", "findings": []}])
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    with pytest.raises(ConflictReviewError, match="structured output"):
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )

    assert len(claude_calls(runner)) == 1


def test_confirmation_major_requires_findings_and_links_initial_attempt(tmp_path: Path):
    runner = SequenceReviewerRunner(
        [
            {"verdict": "major", "findings": ["possible kanban regression"]},
            {"verdict": "major", "findings": []},
        ]
    )
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    with pytest.raises(ConflictReviewError, match="structured output") as failure:
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )

    assert failure.value.details_artifact is not None
    initial = ConflictReviewAttemptArtifact.load(
        record.parent.parent / failure.value.details_artifact
    )
    assert initial.verdict == "major"


@pytest.mark.parametrize(
    "response",
    [
        subprocess.CompletedProcess(["claude"], 1, "", "failed"),
        subprocess.CompletedProcess(["claude"], 0, "not-json", ""),
    ],
)
def test_confirmation_execution_failure_links_initial_attempt(
    tmp_path: Path, response: subprocess.CompletedProcess
):
    runner = SequenceReviewerRunner(
        [
            {"verdict": "major", "findings": ["possible kanban regression"]},
            response,
        ]
    )
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    with pytest.raises(ConflictReviewError) as failure:
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )

    assert failure.value.details_artifact is not None
    assert (record.parent.parent / failure.value.details_artifact).is_file()


def test_head_change_after_confirmation_fails_closed_with_initial_attempt(tmp_path: Path):
    class ConfirmationHeadChangeRunner(SequenceReviewerRunner):
        def __init__(self):
            super().__init__(
                [
                    {"verdict": "major", "findings": ["possible regression"]},
                    {"verdict": "green", "findings": []},
                ]
            )
            self.head_calls = 0

        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                self.head_calls += 1
                self.calls.append((tuple(argv), Path(cwd), timeout))
                head = CANDIDATE_SHA if self.head_calls < 3 else "b" * 40
                return subprocess.CompletedProcess(argv, 0, head + "\n", "")
            return super().run(argv, cwd, timeout)

    runner = ConfirmationHeadChangeRunner()
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    with pytest.raises(ConflictReviewError, match="candidate SHA changed") as failure:
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )

    assert failure.value.details_artifact is not None


def test_worktree_mutation_after_confirmation_fails_closed_with_initial_attempt(
    tmp_path: Path,
):
    class ConfirmationMutationRunner(SequenceReviewerRunner):
        def __init__(self):
            super().__init__(
                [
                    {"verdict": "major", "findings": ["possible regression"]},
                    {"verdict": "green", "findings": []},
                ]
            )
            self.status_calls = 0

        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            if argv[:2] == ["git", "status"]:
                self.status_calls += 1
                self.calls.append((tuple(argv), Path(cwd), timeout))
                status = "" if self.status_calls < 3 else " M gateway/run.py\n"
                return subprocess.CompletedProcess(argv, 0, status, "")
            return super().run(argv, cwd, timeout)

    runner = ConfirmationMutationRunner()
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    with pytest.raises(ConflictReviewError, match="modified the worktree") as failure:
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )

    assert failure.value.details_artifact is not None


def test_evidence_publication_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner = SequenceReviewerRunner([{"verdict": "green", "findings": []}])
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)
    monkeypatch.setattr(
        "ops.cloudadvisor.hermes_ops.sync_review.write_conflict_review_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConflictReviewEvidenceError("disk unavailable")
        ),
    )

    with pytest.raises(ConflictReviewError, match="evidence"):
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )


def test_claude_reviewer_rejects_changed_head(tmp_path: Path):
    class ChangedHeadRunner(ReviewerRunner):
        def run(self, argv: list[str], cwd: Path, timeout: int = 300):
            completed = super().run(argv, cwd, timeout)
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\n", "")
            return completed

    worktree = tmp_path / "candidate"
    worktree.mkdir()
    record = frozen_record(tmp_path)
    reviewer = ClaudeConflictReviewer(
        executable=tmp_path / "bin" / ("claude.exe" if os.name == "nt" else "claude"),
        runner=ChangedHeadRunner({"verdict": "green", "findings": []}),
        resolver_backend="codex",
        evidence_dir=record.parent,
    )

    with pytest.raises(ConflictReviewError, match="candidate SHA"):
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )


@pytest.mark.parametrize("name", ["claude", "claude.exe", "claude.cmd"])
def test_claude_reviewer_preserves_platform_paths_with_spaces(
    tmp_path: Path, name: str
):
    worktree = tmp_path / "candidate with spaces"
    worktree.mkdir()
    record = frozen_record(tmp_path / "evidence with spaces")
    runner = ReviewerRunner({"verdict": "green", "findings": []})
    executable = tmp_path / "Program Files" / "Claude" / name
    reviewer = ClaudeConflictReviewer(
        executable=executable,
        runner=runner,
        resolver_backend="codex",
        evidence_dir=record.parent,
    )
    reviewer.review(
        candidate_sha=CANDIDATE_SHA,
        worktree=worktree,
        resolution_record=record,
    )
    command = next(
        call[0] for call in runner.calls if Path(call[0][0]).name == name
    )
    assert Path(command[0]) == executable
    add_dir = command.index("--add-dir")
    assert Path(command[add_dir + 1]) == record.parent


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_claude_reviewer_rejects_original_artifact_symlink_before_resolve(
    tmp_path: Path,
):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    record = frozen_record(tmp_path)
    link = record.parent / "linked-resolution.json"
    link.symlink_to(record)
    runner = ReviewerRunner({"verdict": "green", "findings": []})
    reviewer = ClaudeConflictReviewer(
        executable=tmp_path / "bin" / "claude",
        runner=runner,
        resolver_backend="codex",
        evidence_dir=record.parent,
    )

    with pytest.raises(ConflictReviewError, match="direct regular file"):
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=link,
        )

    assert runner.calls == []


def test_claude_reviewer_requires_artifact_direct_child_of_expected_dir(
    tmp_path: Path,
):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    record = frozen_record(tmp_path)
    reviewer = ClaudeConflictReviewer(
        executable=tmp_path / "bin" / ("claude.exe" if os.name == "nt" else "claude"),
        runner=ReviewerRunner({"verdict": "green", "findings": []}),
        resolver_backend="codex",
        evidence_dir=tmp_path / "different-evidence-dir",
    )

    with pytest.raises(ConflictReviewError, match="expected evidence directory"):
        reviewer.review(
            candidate_sha=CANDIDATE_SHA,
            worktree=worktree,
            resolution_record=record,
        )
