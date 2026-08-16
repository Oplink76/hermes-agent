"""Read-only Epic readiness, frozen immutable-release row types, and typed
release invalidation results.

Lifecycle transitions, candidate preparation, and CI observation are owned by
``hermes_cli.kanban_db``.  This module is intentionally limited to strict
persistence parsing, derivation from current durable facts, and the typed
invalidation error/result shapes the release seam consumes.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Literal, Mapping, TypeAlias, cast


EpicReleaseStatus: TypeAlias = Literal[
    "awaiting_push",
    "ci_pending",
    "ci_failed",
    "released",
    "invalidated",
]

_EPIC_RELEASE_STATUSES = frozenset(
    {"awaiting_push", "ci_pending", "ci_failed", "released", "invalidated"}
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
Row: TypeAlias = Mapping[str, object] | sqlite3.Row


class EpicReleasePreparationError(RuntimeError):
    """Typed refusal while preparing an immutable Epic release snapshot."""

    def __init__(self, code: str, evidence: Mapping[str, object] | None = None):
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


class EpicReleaseInvalidationError(RuntimeError):
    """Typed refusal while invalidating an Epic release snapshot."""

    def __init__(self, code: str, evidence: Mapping[str, object] | None = None):
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


class EpicReleaseHandoffError(RuntimeError):
    """Typed refusal while building a human release handoff.

    The handoff is refused — never partially assembled — whenever the
    snapshot's evidence cannot be rechecked truthfully (repository or
    remote unavailable) or when the recheck proves the snapshot is no
    longer the exact release authority (target moved locally or on the
    remote, or any snapshot input drifted).
    """

    def __init__(self, code: str, evidence: Mapping[str, object] | None = None):
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


class EpicReleaseCIObservationError(RuntimeError):
    """Typed refusal while observing CI for an Epic release snapshot."""

    def __init__(self, code: str, evidence: Mapping[str, object] | None = None):
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


@dataclass(frozen=True)
class EpicReleaseSnapshot:
    id: int
    epic_id: str
    epic_tip_sha: str
    target_branch: str
    target_pre_sha: str
    release_candidate_sha: str
    candidate_ref: str
    aggregate_verification_event_id: int
    repository_contract_digest: str
    status: EpicReleaseStatus
    pushed_sha: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class EpicReleaseInvalidation:
    """Typed outcome of one bounded Epic release invalidation attempt.

    ``kind`` is one of:

    * ``invalidated`` — proven authority/input drift: the snapshot row was
      atomically marked ``invalidated`` with typed audit evidence, then the
      exact release-candidate ref was deleted only when it still pinned the
      recorded SHA (``candidate_ref_deleted`` records the ref outcome).
    * ``exact`` — the active snapshot still matches every current input and
      is left untouched.
    * ``missing`` — no active snapshot exists for the epic; nothing to do.
    * ``unverifiable`` — drift cannot be proven right now (repository or
      board authority unavailable); the snapshot is preserved untouched.
    """

    kind: Literal["invalidated", "exact", "missing", "unverifiable"]
    snapshot: EpicReleaseSnapshot | None
    evidence: Mapping[str, object]
    candidate_ref_deleted: bool


@dataclass(frozen=True)
class EpicReleaseHandoff:
    """Truthful immutable release evidence for a human release operator.

    Built only after the snapshot's authority has been rechecked against
    the immediate local and remote target heads.  Every field is plain
    data — IDs, full SHAs, member keys, the repository contract digest
    (via ``snapshot.repository_contract_digest``), the aggregate
    verification event, the required CI workflows, the candidate ref, the
    observed target heads, and one plain-language external action.  There
    is deliberately no merge or push capability here: ``action`` is prose
    for a human and nothing in this shape is executable by the board.
    """

    epic_id: str
    snapshot: EpicReleaseSnapshot
    members: tuple[EpicReleaseMember, ...]
    workflows: tuple[str, ...]
    aggregate_event_kind: str
    aggregate_event_receipt: Mapping[str, object]
    local_target_head: str
    remote_target_head: str
    remote_name: str
    action: str
    checked_at: int


@dataclass(frozen=True)
class EpicReleaseCIObservation:
    """Typed outcome of one bounded, read-only exact-SHA CI observation.

    ``kind`` is one of:

    * ``released`` — the exact ``release_candidate_sha`` is confirmed on
      the remote target and every required workflow passed: the snapshot
      row was atomically flipped to ``released`` with typed audit
      evidence, then the exact release-candidate ref was deleted only
      when it still pinned the recorded SHA.
    * ``ci_pending`` — the candidate is not yet pushed (``pushed_sha`` is
      absent) or its workflows are still queued/running; the snapshot is
      preserved untouched.
    * ``ci_failed`` — the exact candidate is pushed but a required
      workflow failed, was cancelled, or timed out; the snapshot is
      preserved and manual recovery stays available.  A later same-SHA
      observation where every workflow passes releases.
    * ``invalidated`` — proven authority drift or a remote head that
      moved away from the recorded candidate SHA: the snapshot was
      atomically marked ``invalidated`` with typed audit evidence and the
      exact release-candidate ref was deleted when it still pinned the
      recorded SHA.
    * ``missing`` — no active snapshot exists for the epic; nothing to do.
    * ``unavailable`` — the remote target or CI provider cannot be
      observed right now; the snapshot is preserved untouched.

    Every path is strictly read-only against the CI provider (HTTP GET
    only) and against Git (``rev-parse``/``ls-remote`` only): no rerun,
    cancel, merge, push, or update-remote primitive is ever issued.
    """

    kind: Literal[
        "released",
        "ci_pending",
        "ci_failed",
        "invalidated",
        "missing",
        "unavailable",
    ]
    snapshot: EpicReleaseSnapshot | None
    evidence: Mapping[str, object]
    candidate_ref_deleted: bool
    pushed_sha: str | None


@dataclass(frozen=True)
class EpicReleaseMember:
    snapshot_id: int
    epic_id: str
    story_id: str
    source_sha: str
    candidate_sha: str
    integrated_at: int


@dataclass(frozen=True)
class EpicReadinessMember:
    story_id: str
    source_sha: str
    candidate_sha: str
    integrated_at: int


@dataclass(frozen=True)
class EpicTerminalSource:
    source_sha: str
    governed_non_empty: bool


@dataclass(frozen=True)
class EpicReadiness:
    epic_id: str
    epic_tip_sha: str | None
    members: tuple[EpicReadinessMember, ...]
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers and bool(self.members)


def _value(row: Row, field: str) -> object:
    try:
        return row[field]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Epic release row is missing {field}") from exc


def _text(row: Row, field: str) -> str:
    value = _value(row, field)
    if not isinstance(value, str):
        raise ValueError(f"Epic release {field} must be text")
    return value


def _integer(row: Row, field: str) -> int:
    value = _value(row, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Epic release {field} must be an integer")
    return value


def _full_sha(row: Row, field: str, *, nullable: bool = False) -> str | None:
    value = _value(row, field)
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"Epic release {field} must be a full lowercase SHA")
    return value


def epic_release_snapshot_from_row(row: Row) -> EpicReleaseSnapshot:
    """Parse one snapshot without normalizing malformed release authority."""

    status = _text(row, "status")
    if status not in _EPIC_RELEASE_STATUSES:
        raise ValueError(f"invalid Epic release status: {status!r}")

    epic_tip_sha = _full_sha(row, "epic_tip_sha")
    target_pre_sha = _full_sha(row, "target_pre_sha")
    release_candidate_sha = _full_sha(row, "release_candidate_sha")
    assert (
        epic_tip_sha is not None
        and target_pre_sha is not None
        and release_candidate_sha is not None
    )

    return EpicReleaseSnapshot(
        id=_integer(row, "id"),
        epic_id=_text(row, "epic_id"),
        epic_tip_sha=epic_tip_sha,
        target_branch=_text(row, "target_branch"),
        target_pre_sha=target_pre_sha,
        release_candidate_sha=release_candidate_sha,
        candidate_ref=_text(row, "candidate_ref"),
        aggregate_verification_event_id=_integer(
            row, "aggregate_verification_event_id"
        ),
        repository_contract_digest=_text(row, "repository_contract_digest"),
        status=cast(EpicReleaseStatus, status),
        pushed_sha=_full_sha(row, "pushed_sha", nullable=True),
        created_at=_integer(row, "created_at"),
        updated_at=_integer(row, "updated_at"),
    )


def epic_release_member_from_row(row: Row) -> EpicReleaseMember:
    """Parse one immutable member pin from a release snapshot."""

    source_sha = _full_sha(row, "source_sha")
    candidate_sha = _full_sha(row, "candidate_sha")
    assert source_sha is not None and candidate_sha is not None

    return EpicReleaseMember(
        snapshot_id=_integer(row, "snapshot_id"),
        epic_id=_text(row, "epic_id"),
        story_id=_text(row, "story_id"),
        source_sha=source_sha,
        candidate_sha=candidate_sha,
        integrated_at=_integer(row, "integrated_at"),
    )


def derive_epic_readiness(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    epic_tip_sha: str,
    current_terminal_source: Callable[[str], EpicTerminalSource | None],
    commit_contains: Callable[[str, str], bool],
) -> EpicReadiness:
    """Derive release readiness solely from current rows and commit ancestry.

    Story verification events are deliberately absent from this derivation.
    They remain audit evidence, while the terminal Review authority, durable
    integration intent/fact, and repository graph are current authority.
    """

    members = tuple(
        str(row["task_id"])
        for row in conn.execute(
            "SELECT task_id FROM epic_memberships WHERE epic_id=? ORDER BY task_id",
            (epic_id,),
        ).fetchall()
    )
    if not members:
        return EpicReadiness(epic_id, epic_tip_sha, (), ("no_members",))

    blockers: list[str] = []
    ready_members: list[EpicReadinessMember] = []
    active_intent_statuses = (
        "pending",
        "running",
        "prepared",
        "rework_required",
        "attention_required",
    )
    active_placeholders = ",".join("?" for _ in active_intent_statuses)

    for story_id in members:
        prefix = f"{story_id}:"
        task = conn.execute(
            "SELECT status, current_step_key, running, blocked, current_run_id "
            "FROM tasks WHERE id=?",
            (story_id,),
        ).fetchone()
        if (
            task is None
            or task["status"] != "done"
            or task["current_step_key"] != "done"
            or bool(task["running"])
            or bool(task["blocked"])
            or task["current_run_id"] is not None
        ):
            blockers.append(prefix + "nonterminal_member")
        if conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND step_key='review' "
            "AND (status='running' OR ended_at IS NULL) LIMIT 1",
            (story_id,),
        ).fetchone() is not None:
            blockers.append(prefix + "active_review")
        if conn.execute(
            "SELECT 1 FROM product_rework_directives "
            "WHERE task_id=? AND status='active' LIMIT 1",
            (story_id,),
        ).fetchone() is not None:
            blockers.append(prefix + "active_directive")
        if conn.execute(
            f"SELECT 1 FROM story_integration_intents "  # noqa: S608 -- placeholders only
            f"WHERE epic_id=? AND story_id=? AND status IN ({active_placeholders}) LIMIT 1",
            (epic_id, story_id, *active_intent_statuses),
        ).fetchone() is not None:
            blockers.append(prefix + "active_intent")

        terminal_source = current_terminal_source(story_id)
        source_sha = terminal_source.source_sha if terminal_source is not None else None
        if not isinstance(source_sha, str) or _FULL_SHA_RE.fullmatch(source_sha) is None:
            blockers.append(prefix + "missing_terminal_source")
            continue
        if not terminal_source.governed_non_empty:
            blockers.append(prefix + "ungoverned_contribution")
            continue
        fact = conn.execute(
            "SELECT candidate_sha, integrated_at FROM epic_story_integrations "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (epic_id, story_id, source_sha),
        ).fetchone()
        if fact is None:
            blockers.append(prefix + "missing_integration_fact")
            continue
        candidate_sha = fact["candidate_sha"]
        if (
            not isinstance(candidate_sha, str)
            or _FULL_SHA_RE.fullmatch(candidate_sha) is None
        ):
            blockers.append(prefix + "invalid_candidate")
            continue
        intent = conn.execute(
            "SELECT candidate_sha FROM story_integration_intents "
            "WHERE epic_id=? AND story_id=? AND source_sha=? AND status='integrated'",
            (epic_id, story_id, source_sha),
        ).fetchone()
        if intent is None:
            blockers.append(prefix + "missing_integrated_intent")
            continue
        if intent["candidate_sha"] != candidate_sha:
            blockers.append(prefix + "candidate_mismatch")
            continue
        try:
            candidate_has_source = bool(commit_contains(candidate_sha, source_sha))
            tip_has_candidate = bool(commit_contains(epic_tip_sha, candidate_sha))
        except Exception:
            blockers.append(prefix + "ancestry_unavailable")
            continue
        if not candidate_has_source:
            blockers.append(prefix + "candidate_missing_source")
        if not tip_has_candidate:
            blockers.append(prefix + "epic_tip_missing_candidate")
        ready_members.append(
            EpicReadinessMember(
                story_id=story_id,
                source_sha=source_sha,
                candidate_sha=candidate_sha,
                integrated_at=int(fact["integrated_at"]),
            )
        )

    return EpicReadiness(
        epic_id=epic_id,
        epic_tip_sha=epic_tip_sha,
        members=tuple(ready_members),
        blockers=tuple(blockers),
    )
