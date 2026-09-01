"""Pure validation for canonical product-workflow terminal outcomes.

The database layer owns task/run transitions.  This module only interprets the
structured envelope that an ordinary Test or Review worker is allowed to use.
Serialized prompt-parameter markup is deliberately observed as a leak, never
parsed as lifecycle authority.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast


TerminalVerdict = Literal[
    "passed",
    "approved",
    "changes_requested",
    "architecture_invalid",
]
OutcomeObservation = Literal["serialized_parameter_leak"]

_REWORK_ROUTES: dict[tuple[str, str], str] = {
    ("test", "changes_requested"): "development",
    ("review", "changes_requested"): "development",
    ("review", "architecture_invalid"): "architecture",
}
_POSITIVE_VERDICTS: dict[str, str] = {
    "test": "passed",
    "review": "approved",
}
_KNOWN_VERDICTS = frozenset(
    {"passed", "approved", "changes_requested", "architecture_invalid"}
)
_SERIALIZED_PARAMETER_RE = re.compile(
    r"<parameter\s+name=['\"]workflow_outcome['\"]\s*>"
)
_MISSING = object()


@dataclass(frozen=True)
class TerminalOutcome:
    """The only outcome shape ordinary Test/Review completion may consume."""

    verdict: TerminalVerdict
    target_step: str | None
    findings: tuple[str, ...]
    observations: tuple[OutcomeObservation, ...]


class OutcomeValidationError(ValueError):
    """A safe, bounded reason why a terminal envelope is not authoritative."""

    def __init__(self, code: str, *, qualifier: str | None = None):
        self.code = code
        self.qualifier = qualifier
        super().__init__(code)


class ProductOutcomeError(ValueError):
    """Typed ordinary-completion rejection with no worker-authored prose."""

    def __init__(
        self,
        task_id: str,
        run_id: int,
        phase: str,
        code: str,
        qualifier: str | None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.phase = phase
        self.code = code
        self.qualifier = qualifier
        super().__init__(code)


@dataclass(frozen=True)
class TerminalRunRecord:
    """Immutable, dispatcher-pinned facts from one ended product run."""

    run_id: int
    phase: str
    outcome: TerminalOutcome | None
    test_branch: str | None = None
    test_head_sha: str | None = None
    review_branch: str | None = None
    review_base_sha: str | None = None
    review_head_sha: str | None = None
    writer_provider: str | None = None
    tester_provider: str | None = None
    reviewer_provider: str | None = None


@dataclass(frozen=True)
class ApprovedCandidate:
    run_id: int
    branch: str
    base_sha: str
    source_sha: str
    reviewer_provider: str
    writer_provider: str

    def __iter__(self):
        """Keep the old private DB tuple seam readable during migration."""

        yield self.branch
        yield self.source_sha

    def __getitem__(self, index: int) -> str:
        return (self.branch, self.source_sha)[index]


@dataclass(frozen=True)
class PassedTest:
    run_id: int
    branch: str
    source_sha: str
    tester_provider: str
    writer_provider: str


@dataclass(frozen=True)
class CandidateEligibility:
    source_sha: str
    non_empty: bool


class CandidateEligibilityError(ValueError):
    """A candidate cannot be used as a new integration contribution."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _full_sha(value: object) -> str | None:
    text = str(value or "").strip()
    return text if _FULL_SHA_RE.fullmatch(text) else None


def _provider_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _latest_phase_run(
    runs: Sequence[TerminalRunRecord], phase: str
) -> TerminalRunRecord | None:
    return next((run for run in reversed(runs) if run.phase == phase), None)


def _approved_candidate_from_pinned_run(
    run: TerminalRunRecord,
) -> ApprovedCandidate | None:
    if run.outcome is None or run.outcome.verdict != "approved":
        return None
    branch = str(run.review_branch or "").strip()
    base_sha = _full_sha(run.review_base_sha)
    source_sha = _full_sha(run.review_head_sha)
    reviewer = str(run.reviewer_provider or "").strip()
    writer = str(run.writer_provider or "").strip()
    if (
        not branch
        or base_sha is None
        or source_sha is None
        or not reviewer
        or not writer
        or _provider_key(reviewer) == _provider_key(writer)
    ):
        return None
    return ApprovedCandidate(
        run_id=run.run_id,
        branch=branch,
        base_sha=base_sha,
        source_sha=source_sha,
        reviewer_provider=reviewer,
        writer_provider=writer,
    )


def latest_review_authority(
    runs: Sequence[TerminalRunRecord],
) -> ApprovedCandidate | None:
    """Return authority from exactly the latest ended Review run.

    A later rejection, malformed run, or run with incomplete dispatcher pins
    invalidates older approvals.  The caller must supply ended runs in their
    stored chronological order; this function intentionally never searches
    backward past the latest Review attempt.
    """

    latest = _latest_phase_run(runs, "review")
    return _approved_candidate_from_pinned_run(latest) if latest is not None else None


def _passed_test_from_pinned_run(
    run: TerminalRunRecord, source_sha: str
) -> PassedTest | None:
    if run.outcome is None or run.outcome.verdict != "passed":
        return None
    requested_sha = _full_sha(source_sha)
    test_sha = _full_sha(run.test_head_sha)
    branch = str(run.test_branch or "").strip()
    tester = str(run.tester_provider or "").strip()
    writer = str(run.writer_provider or "").strip()
    if (
        requested_sha is None
        or test_sha is None
        or test_sha != requested_sha
        or not branch
        or not tester
        or not writer
    ):
        return None
    return PassedTest(
        run_id=run.run_id,
        branch=branch,
        source_sha=test_sha,
        tester_provider=tester,
        writer_provider=writer,
    )


def latest_test_authority(
    runs: Sequence[TerminalRunRecord], source_sha: str
) -> PassedTest | None:
    """Return authority from exactly the latest ended Test run."""

    latest = _latest_phase_run(runs, "test")
    return (
        _passed_test_from_pinned_run(latest, source_sha)
        if latest is not None
        else None
    )


def candidate_eligibility(
    repo: Path,
    approved: ApprovedCandidate,
    passed: PassedTest,
) -> CandidateEligibility:
    """Require matching authority and a non-empty reviewed contribution."""

    if (
        approved.branch != passed.branch
        or approved.source_sha != passed.source_sha
    ):
        raise CandidateEligibilityError("stale_review")
    if _full_sha(approved.base_sha) is None or _full_sha(approved.source_sha) is None:
        raise CandidateEligibilityError("stale_review")
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--quiet",
                approved.base_sha,
                approved.source_sha,
            ],
            check=False,
        )
    except OSError as exc:
        raise CandidateEligibilityError("io_error") from exc
    if result.returncode == 0:
        raise CandidateEligibilityError("empty_contribution")
    if result.returncode != 1:
        raise CandidateEligibilityError("io_error")
    return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)


def _has_serialized_parameter_marker(summary: str | None, result: str | None) -> bool:
    return any(
        isinstance(value, str) and _SERIALIZED_PARAMETER_RE.search(value)
        for value in (summary, result)
    )


def _invalid_shape() -> OutcomeValidationError:
    return OutcomeValidationError("invalid_shape")


def _validate_redundant_fields(
    phase: str,
    outcome: TerminalOutcome,
    metadata: Mapping[str, object],
) -> None:
    """Reject contradictory aliases without treating aliases as authority.

    Older workers sometimes repeated a verdict at the metadata root or under a
    role-specific provenance object.  Those values are advisory: a recognized
    value may agree with the canonical envelope, but it can never repair a
    missing envelope or override it.
    """

    aliases: list[Any] = []
    for key in (
        "verdict",
        "outcome",
        "run_outcome",
        "completion_outcome",
        "outcome_verdict",
        "reviewer_verdict",
        "tester_verdict",
        "reviewer_result",
        "tester_result",
    ):
        if key in metadata:
            aliases.append((key, metadata[key]))

    for source in (metadata, metadata.get("ai_provenance")):
        if not isinstance(source, Mapping):
            continue
        for role in ("reviewer", "tester", "verifier"):
            details = source.get(role)
            if isinstance(details, Mapping):
                for key in ("verdict", "result", "outcome"):
                    if key in details:
                        aliases.append((f"{role}.{key}", details[key]))

    canonical_verdict = outcome.verdict
    for name, value in aliases:
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if normalized not in _KNOWN_VERDICTS:
            continue
        if name.startswith("reviewer_"):
            role = "reviewer"
        elif name.startswith("tester_"):
            role = "tester"
        else:
            role = name.split(".", 1)[0]
        role_is_current = (
            role == "reviewer" and phase == "review"
        ) or (role in {"tester", "verifier"} and phase == "test")
        generic_alias = "." not in name and not name.startswith(
            ("reviewer_", "tester_")
        )
        if (generic_alias or role_is_current) and normalized != canonical_verdict:
            raise OutcomeValidationError("contradictory")


def _validate_exact_shape(phase: str, canonical: object) -> TerminalOutcome:
    if not isinstance(canonical, Mapping):
        raise _invalid_shape()

    keys = set(canonical)
    verdict = canonical.get("verdict")
    if not isinstance(verdict, str) or verdict not in _KNOWN_VERDICTS:
        raise OutcomeValidationError("invalid_verdict")
    typed_verdict = cast(TerminalVerdict, verdict)

    positive = _POSITIVE_VERDICTS.get(phase)
    if typed_verdict in {"passed", "approved"}:
        if positive != typed_verdict or keys != {"verdict"}:
            raise OutcomeValidationError(
                "phase_mismatch" if positive != typed_verdict else "invalid_shape"
            )
        return TerminalOutcome(
            verdict=typed_verdict, target_step=None, findings=(), observations=()
        )

    if keys != {"verdict", "target_step", "findings"}:
        raise _invalid_shape()
    target_step = canonical.get("target_step")
    expected_target = _REWORK_ROUTES.get((phase, typed_verdict))
    if expected_target is None or target_step != expected_target:
        raise OutcomeValidationError("phase_mismatch")

    raw_findings = canonical.get("findings")
    if (
        not isinstance(raw_findings, list)
        or not raw_findings
        or not all(isinstance(item, str) and item.strip() for item in raw_findings)
    ):
        raise OutcomeValidationError("invalid_findings")
    findings = tuple(item.strip() for item in raw_findings)
    return TerminalOutcome(
        verdict=typed_verdict,
        target_step=expected_target,
        findings=findings,
        observations=(),
    )


def validate_terminal_outcome(
    *,
    task_id: str,
    run_id: int,
    phase: str,
    summary: str | None,
    result: str | None,
    metadata: Mapping[str, object] | None,
) -> TerminalOutcome:
    """Validate an ordinary Test/Review terminal envelope.

    ``task_id`` and ``run_id`` are part of the public seam so callers can bind
    the result to the active task/run before mutating anything.  The pure
    validator does not use worker-authored identifiers as authority.
    """

    del task_id, run_id
    marker = _has_serialized_parameter_marker(summary, result)
    canonical: object = (
        metadata.get("workflow_outcome", _MISSING)
        if isinstance(metadata, Mapping)
        else _MISSING
    )
    if canonical is _MISSING or canonical is None:
        raise OutcomeValidationError(
            "missing", qualifier="serialized_parameter" if marker else None
        )

    outcome = _validate_exact_shape(phase, canonical)
    if not isinstance(metadata, Mapping):
        # The canonical value could only have been found in a Mapping, but keep
        # this guard explicit for unusual Mapping implementations.
        raise _invalid_shape()
    _validate_redundant_fields(phase, outcome, metadata)
    if marker:
        return TerminalOutcome(
            verdict=outcome.verdict,
            target_step=outcome.target_step,
            findings=outcome.findings,
            observations=("serialized_parameter_leak",),
        )
    return outcome


__all__ = [
    "ApprovedCandidate",
    "CandidateEligibility",
    "CandidateEligibilityError",
    "OutcomeValidationError",
    "PassedTest",
    "ProductOutcomeError",
    "TerminalOutcome",
    "TerminalRunRecord",
    "candidate_eligibility",
    "latest_review_authority",
    "latest_test_authority",
    "validate_terminal_outcome",
]
