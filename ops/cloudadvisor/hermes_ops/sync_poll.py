"""Shared bounded exact-head polling for protected sync pull requests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .sync_github import SyncGitHubPort, SyncPullRequestEvidence


class ExactHeadPollError(RuntimeError):
    """Exact protected-PR evidence did not become green within its budget."""


class RequiredCheckRedError(ExactHeadPollError):
    """The exact required check completed unsuccessfully."""


@dataclass(frozen=True)
class ExactHeadExpectation:
    pr_number: int
    base_sha: str
    head_sha: str
    required_check: str


def poll_exact_head(
    github: SyncGitHubPort,
    expectation: ExactHeadExpectation,
    *,
    timeout_seconds: int,
    poll_interval_seconds: int,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> SyncPullRequestEvidence:
    """Return exact green evidence without sleeping or accepting after deadline."""
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ExactHeadPollError("polling budget is invalid")
    deadline = clock() + timeout_seconds
    max_attempts = math.ceil(timeout_seconds / poll_interval_seconds) + 2
    transient_failures = 0
    for _attempt in range(max_attempts):
        if clock() >= deadline:
            raise ExactHeadPollError("required check timed out")
        try:
            evidence = github.evidence(expectation.pr_number)
        except Exception as exc:
            transient_failures += 1
            if transient_failures > 1:
                raise ExactHeadPollError("GitHub evidence failed repeatedly") from exc
            remaining = deadline - clock()
            if remaining <= 0:
                raise ExactHeadPollError("required check timed out") from exc
            sleeper(min(poll_interval_seconds, remaining))
            continue
        if clock() >= deadline:
            raise ExactHeadPollError("required check timed out")
        if evidence.number != expectation.pr_number:
            raise ExactHeadPollError("pull request number changed")
        if evidence.state != "open":
            raise ExactHeadPollError("pull request is not open")
        if evidence.base_sha != expectation.base_sha:
            raise ExactHeadPollError("pull request base changed")
        if evidence.head_sha != expectation.head_sha:
            raise ExactHeadPollError("pull request head changed")
        if evidence.required_check != expectation.required_check:
            raise ExactHeadPollError("required check identity changed")
        conclusion = evidence.required_check_conclusion.lower()
        if conclusion == "success":
            return evidence
        if conclusion not in {"pending", "queued", "in_progress"}:
            raise RequiredCheckRedError("required check is not green")
        remaining = deadline - clock()
        if remaining <= 0:
            raise ExactHeadPollError("required check timed out")
        sleeper(min(poll_interval_seconds, remaining))
    raise ExactHeadPollError("required check timed out")
