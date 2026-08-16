"""Typed persistence tests for immutable Epic release snapshots."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace
import json
import sqlite3
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_epic_release import (
    EpicReadiness,
    EpicReadinessMember,
    EpicReleaseCIObservation,
    EpicReleaseCIObservationError,
    EpicReleaseHandoff,
    EpicReleaseHandoffError,
    EpicReleaseInvalidation,
    EpicReleaseMember,
    EpicReleaseSnapshot,
    EpicTerminalSource,
    derive_epic_readiness,
    epic_release_member_from_row,
    epic_release_snapshot_from_row,
)
from hermes_cli.kanban_repository import TargetHeadsObservation


EPIC_SHA = "1" * 40
TARGET_SHA = "2" * 40
RELEASE_SHA = "3" * 40
SOURCE_SHA = "4" * 40
MEMBER_CANDIDATE_SHA = "5" * 40
PUSHED_SHA = "6" * 40
CONTRACT_DIGEST = "7" * 64
GENERATED_POLICY_DIGEST = "8" * 64
AGGREGATE_CANDIDATE_SHA = "9" * 40
RELEASE_CANDIDATE_REF = "refs/hermes/release-candidates/exact"


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    epic_id: str = "epic-1",
    status: str = "awaiting_push",
    pushed_sha: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO epic_release_snapshots (
            epic_id, epic_tip_sha, target_branch, target_pre_sha,
            release_candidate_sha, candidate_ref,
            aggregate_verification_event_id, repository_contract_digest,
            status, pushed_sha, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            epic_id,
            EPIC_SHA,
            "main",
            TARGET_SHA,
            RELEASE_SHA,
            "refs/hermes/releases/epic-1",
            71,
            CONTRACT_DIGEST,
            status,
            pushed_sha,
            100,
            110,
        ),
    )
    return int(cursor.lastrowid)


def _insert_member(conn: sqlite3.Connection, snapshot_id: int) -> None:
    conn.execute(
        """
        INSERT INTO epic_release_members (
            snapshot_id, epic_id, story_id, source_sha,
            candidate_sha, integrated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            "epic-1",
            "story-1",
            SOURCE_SHA,
            MEMBER_CANDIDATE_SHA,
            90,
        ),
    )


def test_epic_release_schema_has_exact_snapshot_and_member_columns(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        snapshot_info = conn.execute(
            "PRAGMA table_info(epic_release_snapshots)"
        ).fetchall()
        member_info = conn.execute(
            "PRAGMA table_info(epic_release_members)"
        ).fetchall()

    assert tuple(row["name"] for row in snapshot_info) == (
        "id",
        "epic_id",
        "epic_tip_sha",
        "target_branch",
        "target_pre_sha",
        "release_candidate_sha",
        "candidate_ref",
        "aggregate_verification_event_id",
        "repository_contract_digest",
        "status",
        "pushed_sha",
        "created_at",
        "updated_at",
    )
    assert tuple(row["name"] for row in member_info) == (
        "snapshot_id",
        "epic_id",
        "story_id",
        "source_sha",
        "candidate_sha",
        "integrated_at",
    )
    assert {row["name"]: row["pk"] for row in member_info if row["pk"]} == {
        "snapshot_id": 1,
        "story_id": 2,
    }


def test_epic_release_schema_round_trips_frozen_snapshot_and_member(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        snapshot_id = _insert_snapshot(conn, status="ci_pending", pushed_sha=PUSHED_SHA)
        _insert_member(conn, snapshot_id)
        snapshot_row = conn.execute(
            "SELECT * FROM epic_release_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        member_row = conn.execute(
            "SELECT * FROM epic_release_members WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()

    snapshot = epic_release_snapshot_from_row(snapshot_row)
    member = epic_release_member_from_row(member_row)

    assert snapshot == EpicReleaseSnapshot(
        id=snapshot_id,
        epic_id="epic-1",
        epic_tip_sha=EPIC_SHA,
        target_branch="main",
        target_pre_sha=TARGET_SHA,
        release_candidate_sha=RELEASE_SHA,
        candidate_ref="refs/hermes/releases/epic-1",
        aggregate_verification_event_id=71,
        repository_contract_digest=CONTRACT_DIGEST,
        status="ci_pending",
        pushed_sha=PUSHED_SHA,
        created_at=100,
        updated_at=110,
    )
    assert member == EpicReleaseMember(
        snapshot_id=snapshot_id,
        epic_id="epic-1",
        story_id="story-1",
        source_sha=SOURCE_SHA,
        candidate_sha=MEMBER_CANDIDATE_SHA,
        integrated_at=90,
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.status = "released"  # type: ignore[misc]


def test_epic_release_schema_allows_only_one_active_snapshot_per_epic(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        first_id = _insert_snapshot(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn, status="ci_failed")

        conn.execute(
            "UPDATE epic_release_snapshots SET status = 'invalidated' WHERE id = ?",
            (first_id,),
        )
        second_id = _insert_snapshot(conn, status="ci_failed")

    assert second_id != first_id


@pytest.mark.parametrize("status", ["pending", "done", ""])
def test_epic_release_schema_refuses_illegal_status(tmp_path, status):
    with kb.connect(tmp_path / "fresh.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn, status=status)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("epic_tip_sha", "abc"),
        ("target_pre_sha", "B" * 40),
        ("release_candidate_sha", "3" * 39),
        ("pushed_sha", "not-a-sha"),
        ("status", "pending"),
    ],
)
def test_epic_release_snapshot_parser_refuses_malformed_sha_or_status(field, value):
    row = {
        "id": 1,
        "epic_id": "epic-1",
        "epic_tip_sha": EPIC_SHA,
        "target_branch": "main",
        "target_pre_sha": TARGET_SHA,
        "release_candidate_sha": RELEASE_SHA,
        "candidate_ref": "refs/hermes/releases/epic-1",
        "aggregate_verification_event_id": 71,
        "repository_contract_digest": CONTRACT_DIGEST,
        "status": "awaiting_push",
        "pushed_sha": None,
        "created_at": 100,
        "updated_at": 100,
    }
    row[field] = value

    with pytest.raises(ValueError):
        epic_release_snapshot_from_row(row)


@pytest.mark.parametrize("field", ["source_sha", "candidate_sha"])
def test_epic_release_member_parser_refuses_malformed_sha(field):
    row = {
        "snapshot_id": 1,
        "epic_id": "epic-1",
        "story_id": "story-1",
        "source_sha": SOURCE_SHA,
        "candidate_sha": MEMBER_CANDIDATE_SHA,
        "integrated_at": 90,
    }
    row[field] = "short"

    with pytest.raises(ValueError):
        epic_release_member_from_row(row)


def _readiness_member(
    conn: sqlite3.Connection,
    *,
    source_sha: str = SOURCE_SHA,
    candidate_sha: str = MEMBER_CANDIDATE_SHA,
) -> tuple[str, str]:
    epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
    story_id = kb.create_task(
        conn,
        title="Story",
        workflow_template_id="product",
        current_step_key="done",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    conn.execute(
        "UPDATE tasks SET status='done', current_step_key='done', running=0, "
        "blocked=0, current_run_id=NULL WHERE id=?",
        (story_id,),
    )
    conn.execute(
        "INSERT INTO story_integration_intents ("
        "epic_id, story_id, source_sha, source_branch, review_run_id, "
        "review_base_sha, status, candidate_sha, created_at, updated_at"
        ") VALUES (?, ?, ?, 'story/one', 17, ?, 'integrated', ?, 90, 90)",
        (epic_id, story_id, source_sha, TARGET_SHA, candidate_sha),
    )
    conn.execute(
        "INSERT INTO epic_story_integrations "
        "(epic_id, story_id, source_sha, candidate_sha, integrated_at) "
        "VALUES (?, ?, ?, ?, 90)",
        (epic_id, story_id, source_sha, candidate_sha),
    )
    return epic_id, story_id


def _derive_ready(
    conn: sqlite3.Connection,
    epic_id: str,
    story_id: str,
    *,
    terminal_source_sha: str | None = SOURCE_SHA,
    governed_non_empty: bool = True,
    contains=lambda _descendant, _ancestor: True,
) -> EpicReadiness:
    return derive_epic_readiness(
        conn,
        epic_id,
        epic_tip_sha=EPIC_SHA,
        current_terminal_source=lambda requested: (
            EpicTerminalSource(terminal_source_sha, governed_non_empty)
            if requested == story_id and terminal_source_sha is not None
            else None
        ),
        commit_contains=contains,
    )


def test_fact_derived_readiness_accepts_exact_current_member_fact_and_candidate(tmp_path):
    with kb.connect(tmp_path / "ready.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(conn, epic_id, story_id)

    assert result.ready is True
    assert result.blockers == ()
    assert tuple(member.story_id for member in result.members) == (story_id,)
    assert result.members[0].source_sha == SOURCE_SHA
    assert result.members[0].candidate_sha == MEMBER_CANDIDATE_SHA


@pytest.mark.parametrize(
    ("mutate", "terminal_source_sha", "blocker"),
    [
        (
            lambda conn, _epic, story: conn.execute(
                "UPDATE tasks SET status='review', current_step_key='integration_pending' "
                "WHERE id=?",
                (story,),
            ),
            SOURCE_SHA,
            "nonterminal_member",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "INSERT INTO task_runs (task_id, step_key, status, started_at) "
                "VALUES (?, 'review', 'running', 100)",
                (story,),
            ),
            SOURCE_SHA,
            "active_review",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "INSERT INTO product_rework_directives ("
                "task_id, origin_kind, origin_phase, target_phase, findings_json, status, created_at"
                ") VALUES (?, 'integration', 'review', 'development', '[]', 'active', 100)",
                (story,),
            ),
            SOURCE_SHA,
            "active_directive",
        ),
        (
            lambda conn, epic, story: conn.execute(
                "INSERT INTO story_integration_intents ("
                "epic_id, story_id, source_sha, source_branch, review_run_id, review_base_sha, "
                "status, created_at, updated_at"
                ") VALUES (?, ?, ?, 'story/new', 18, ?, 'pending', 100, 100)",
                (epic, story, "8" * 40, TARGET_SHA),
            ),
            SOURCE_SHA,
            "active_intent",
        ),
        (lambda conn, _epic, _story: None, None, "missing_terminal_source"),
        (
            lambda conn, _epic, _story: None,
            "8" * 40,
            "missing_integration_fact",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "DELETE FROM epic_story_integrations WHERE story_id=?", (story,)
            ),
            SOURCE_SHA,
            "missing_integration_fact",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "DELETE FROM story_integration_intents WHERE story_id=?", (story,)
            ),
            SOURCE_SHA,
            "missing_integrated_intent",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "UPDATE epic_story_integrations SET candidate_sha='short' WHERE story_id=?",
                (story,),
            ),
            SOURCE_SHA,
            "invalid_candidate",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "UPDATE story_integration_intents SET candidate_sha=? WHERE story_id=?",
                ("9" * 40, story),
            ),
            SOURCE_SHA,
            "candidate_mismatch",
        ),
    ],
)
def test_fact_derived_readiness_reports_each_member_blocker(
    tmp_path, mutate, terminal_source_sha, blocker
):
    with kb.connect(tmp_path / f"{blocker}.db") as conn:
        epic_id, story_id = _readiness_member(conn)
        mutate(conn, epic_id, story_id)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            terminal_source_sha=terminal_source_sha,
        )

    assert result.ready is False
    assert f"{story_id}:{blocker}" in result.blockers


def test_fact_derived_readiness_requires_governed_non_empty_contribution(tmp_path):
    with kb.connect(tmp_path / "empty.db") as conn:
        epic_id, story_id = _readiness_member(conn)
        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            governed_non_empty=False,
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:ungoverned_contribution",)


def test_fact_derived_readiness_requires_members(tmp_path):
    with kb.connect(tmp_path / "empty.db") as conn:
        epic_id = kb.create_task(conn, title="Empty Epic", work_item_kind="epic")

        result = derive_epic_readiness(
            conn,
            epic_id,
            epic_tip_sha=EPIC_SHA,
            current_terminal_source=lambda _story: None,
            commit_contains=lambda _descendant, _ancestor: True,
        )

    assert result.ready is False
    assert result.blockers == ("no_members",)


def test_fact_derived_readiness_requires_candidate_lineage_and_epic_containment(tmp_path):
    with kb.connect(tmp_path / "ancestry.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            contains=lambda descendant, ancestor: (
                descendant == EPIC_SHA and ancestor == MEMBER_CANDIDATE_SHA
            ),
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:candidate_missing_source",)


def test_fact_derived_readiness_requires_epic_tip_to_contain_candidate(tmp_path):
    with kb.connect(tmp_path / "tip.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            contains=lambda descendant, ancestor: (
                descendant == MEMBER_CANDIDATE_SHA and ancestor == SOURCE_SHA
            ),
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:epic_tip_missing_candidate",)


def test_fact_derived_readiness_blocks_when_ancestry_is_unavailable(tmp_path):
    def unavailable(_descendant, _ancestor):
        raise RuntimeError("repository unavailable")

    with kb.connect(tmp_path / "unavailable.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            contains=unavailable,
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:ancestry_unavailable",)


def test_fact_derived_readiness_ignores_pruned_story_verification_events(tmp_path):
    with kb.connect(tmp_path / "pruned.db") as conn:
        epic_id, story_id = _readiness_member(conn)
        event_id = conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'repository_verification', '{}', 80)",
            (story_id,),
        ).lastrowid
        before = _derive_ready(conn, epic_id, story_id)
        conn.execute("DELETE FROM task_events WHERE id=?", (event_id,))

        after = _derive_ready(conn, epic_id, story_id)

    assert before == after
    assert after.ready is True


def _release_prepare_fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    board_meta = {
        "preset": "product",
        "product_workflow": {"handoff_v2": True},
        "repository": {},
    }
    contract = type(
        "Contract",
        (),
        {
            "repo_root": repo.resolve(),
            "base_ref": "refs/remotes/origin/main",
            "target_branch": "main",
            "verification": {"epic_release": object()},
            "generated_policy_digest": GENERATED_POLICY_DIGEST,
            "ci_workflows": ("CI", "Deploy Test"),
            "digest": CONTRACT_DIGEST,
        },
    )()
    epic_id = "epic-prepare"
    story_id = "story-prepare"
    member = EpicReadinessMember(
        story_id=story_id,
        source_sha=SOURCE_SHA,
        candidate_sha=MEMBER_CANDIDATE_SHA,
        integrated_at=90,
    )
    readiness = EpicReadiness(epic_id, EPIC_SHA, (member,), ())
    receipt_key = kb.build_verification_receipt_key(
        None,
        repo,
        candidate_sha=AGGREGATE_CANDIDATE_SHA,
        contract_digest=CONTRACT_DIGEST,
        generated_policy_digest=GENERATED_POLICY_DIGEST,
        gate_kind="epic_release",
        profile_name="epic_release",
    )
    verification = kb.VerificationResult(
        status="passed",
        source_sha=EPIC_SHA,
        candidate_sha=AGGREGATE_CANDIDATE_SHA,
        contract_digest=CONTRACT_DIGEST,
        profile="epic_release",
        steps=(),
        key=receipt_key,
    )
    candidate = kb.IntegrationCandidate(
        pre_sha=TARGET_SHA,
        candidate_sha=AGGREGATE_CANDIDATE_SHA,
        source_branch="epic/epic-prepare",
        source_sha=EPIC_SHA,
        target_branch="main",
        target_worktree=None,
        scratch_worktree=repo / ".worktrees" / "removed",
        repo_root=repo.resolve(),
        candidate_ref=RELEASE_CANDIDATE_REF,
        verification_result=verification,
    )
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_meta)
    monkeypatch.setattr(
        kb, "repository_contract_for_metadata", lambda _metadata: contract
    )
    monkeypatch.setattr(kb, "epic_readiness", lambda *_args, **_kwargs: readiness)
    monkeypatch.setattr(
        kb,
        "resolve_commit",
        lambda _repo, ref: EPIC_SHA if "epic/" in ref else TARGET_SHA,
    )
    return epic_id, story_id, board_meta, contract, readiness, candidate


def test_prepare_epic_release_snapshot_persists_once_and_replays_without_rebuild(
    tmp_path, monkeypatch
):
    _fixture_epic_id, _fixture_story_id, board_meta, _contract, readiness, candidate = _release_prepare_fixture(
        tmp_path, monkeypatch
    )
    builder_calls = []

    def candidate_builder(*args, **kwargs):
        builder_calls.append((args, kwargs))
        return candidate

    with kb.connect(tmp_path / "prepare.db") as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(conn, title="Story")
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        readiness = replace(
            readiness,
            epic_id=epic_id,
            members=(replace(readiness.members[0], story_id=story_id),),
        )
        candidate = replace(candidate, source_branch=kb.epic_branch_for(epic_id))
        monkeypatch.setattr(kb, "epic_readiness", lambda *_args, **_kwargs: readiness)
        prepared = kb.prepare_epic_release_snapshot(
            conn,
            epic_id,
            board="release-board",
            board_meta=board_meta,
            candidate_builder=candidate_builder,
        )
        replay = kb.prepare_epic_release_snapshot(
            conn,
            epic_id,
            board="release-board",
            board_meta=board_meta,
            candidate_builder=lambda *_args, **_kwargs: pytest.fail(
                "exact release replay must not rebuild"
            ),
        )
        events = conn.execute(
            "SELECT id, kind, payload FROM task_events WHERE task_id=? "
            "AND kind='repository_verification'",
            (epic_id,),
        ).fetchall()
        members = [
            tuple(row)
            for row in conn.execute(
            "SELECT snapshot_id, epic_id, story_id, source_sha, candidate_sha, integrated_at "
            "FROM epic_release_members"
            ).fetchall()
        ]

    assert replay == prepared
    assert prepared.status == "awaiting_push"
    assert prepared.epic_tip_sha == EPIC_SHA
    assert prepared.target_pre_sha == TARGET_SHA
    assert prepared.release_candidate_sha == AGGREGATE_CANDIDATE_SHA
    assert prepared.candidate_ref == RELEASE_CANDIDATE_REF
    assert len(builder_calls) == 1
    assert len(events) == 1
    assert events[0]["kind"] == "repository_verification"
    assert members == [
        (
            prepared.id,
            epic_id,
            story_id,
            SOURCE_SHA,
            MEMBER_CANDIDATE_SHA,
            90,
        )
    ]


def test_prepare_epic_release_snapshot_changed_inputs_cleanup_only_new_candidate(
    tmp_path, monkeypatch
):
    _fixture_epic_id, _fixture_story_id, board_meta, _contract, readiness, candidate = _release_prepare_fixture(
        tmp_path, monkeypatch
    )
    cleanup_calls = []
    monkeypatch.setattr(
        kb,
        "delete_release_candidate_ref",
        lambda repo_root, *, candidate_ref, candidate_sha: cleanup_calls.append(
            (repo_root, candidate_ref, candidate_sha)
        ) or True,
    )

    with kb.connect(tmp_path / "changed.db") as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(conn, title="Story")
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        readiness = replace(
            readiness,
            epic_id=epic_id,
            members=(replace(readiness.members[0], story_id=story_id),),
        )
        candidate = replace(candidate, source_branch=kb.epic_branch_for(epic_id))
        changed = replace(
            readiness,
            members=(replace(readiness.members[0], source_sha="a" * 40),),
        )
        readiness_calls = 0

        def current_readiness(*_args, **_kwargs):
            nonlocal readiness_calls
            readiness_calls += 1
            return readiness if readiness_calls == 1 else changed

        monkeypatch.setattr(kb, "epic_readiness", current_readiness)
        with pytest.raises(kb.EpicReleasePreparationError) as exc_info:
            kb.prepare_epic_release_snapshot(
                conn,
                epic_id,
                board="release-board",
                board_meta=board_meta,
                candidate_builder=lambda *_args, **_kwargs: candidate,
            )
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM epic_release_snapshots"
        ).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? "
            "AND kind='repository_verification'",
            (epic_id,),
        ).fetchone()[0]

    assert exc_info.value.code == "inputs_changed"
    assert cleanup_calls == [
        (candidate.repo_root, RELEASE_CANDIDATE_REF, AGGREGATE_CANDIDATE_SHA)
    ]
    assert snapshot_count == 0
    assert event_count == 0


def test_prepare_epic_release_snapshot_refuses_mismatching_active_snapshot_without_replacement(
    tmp_path, monkeypatch
):
    _fixture_epic_id, _story_id, board_meta, _contract, readiness, candidate = _release_prepare_fixture(
        tmp_path, monkeypatch
    )
    builder = lambda *_args, **_kwargs: pytest.fail(
        "a mismatching active snapshot must not be rebuilt"
    )

    with kb.connect(tmp_path / "active-mismatch.db") as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        readiness = replace(readiness, epic_id=epic_id)
        candidate = replace(candidate, source_branch=kb.epic_branch_for(epic_id))
        monkeypatch.setattr(kb, "epic_readiness", lambda *_args, **_kwargs: readiness)
        conn.execute(
            "INSERT INTO epic_release_snapshots ("
            "epic_id, epic_tip_sha, target_branch, target_pre_sha, "
            "release_candidate_sha, candidate_ref, aggregate_verification_event_id, "
            "repository_contract_digest, status, created_at, updated_at"
            ") VALUES (?, ?, 'main', ?, ?, ?, 71, ?, 'awaiting_push', 100, 100)",
            (
                epic_id,
                "b" * 40,
                TARGET_SHA,
                AGGREGATE_CANDIDATE_SHA,
                RELEASE_CANDIDATE_REF,
                CONTRACT_DIGEST,
            ),
        )
        with pytest.raises(kb.EpicReleasePreparationError) as exc_info:
            kb.prepare_epic_release_snapshot(
                conn,
                epic_id,
                board="release-board",
                board_meta=board_meta,
                candidate_builder=builder,
            )
        snapshot = conn.execute(
            "SELECT epic_tip_sha, status FROM epic_release_snapshots WHERE epic_id=?",
            (epic_id,),
        ).fetchone()

    assert exc_info.value.code == "active_snapshot_mismatch"
    assert tuple(snapshot) == ("b" * 40, "awaiting_push")


# ---------------------------------------------------------------------------
# Invalidation helpers
# ---------------------------------------------------------------------------

@dataclass
class _InvCtx:
    epic_id: str
    story_id: str
    board_meta: dict[str, Any]
    contract: Any
    readiness: EpicReadiness
    candidate: Any
    prepared: EpicReleaseSnapshot


def _prepare_exact_snapshot(conn, monkeypatch, tmp_path, *, epic_label="epic"):
    """Create epic+story tasks, prepare one exact snapshot, and return context."""

    epic_id, story_id, board_meta, contract, readiness, candidate = (
        _release_prepare_fixture(tmp_path, monkeypatch)
    )
    epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
    story_id = kb.create_task(conn, title="Story")
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    readiness = replace(
        readiness,
        epic_id=epic_id,
        members=(replace(readiness.members[0], story_id=story_id),),
    )
    candidate = replace(candidate, source_branch=kb.epic_branch_for(epic_id))
    monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)
    prepared = kb.prepare_epic_release_snapshot(
        conn,
        epic_id,
        board="release-board",
        board_meta=board_meta,
        candidate_builder=lambda *_a, **_k: candidate,
    )
    return _InvCtx(
        epic_id=epic_id,
        story_id=story_id,
        board_meta=board_meta,
        contract=contract,
        readiness=readiness,
        candidate=candidate,
        prepared=prepared,
    )


# ---------------------------------------------------------------------------
# Drift appliers — one function per authority/input drift axis.  Each
# mutates the test environment so that the active snapshot no longer
# matches current authority.
# ---------------------------------------------------------------------------

def _drift_epic_tip(ctx, conn, monkeypatch):
    moved = "e" * 40
    readiness = replace(ctx.readiness, epic_tip_sha=moved)
    ctx.readiness = readiness
    monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)
    monkeypatch.setattr(
        kb, "resolve_commit",
        lambda _repo, ref: moved if "epic/" in ref else TARGET_SHA,
    )


def _drift_target(ctx, conn, monkeypatch):
    moved = "f" * 40
    monkeypatch.setattr(
        kb, "resolve_commit",
        lambda _repo, ref: EPIC_SHA if "epic/" in ref else moved,
    )


def _drift_contract(ctx, conn, monkeypatch):
    moved = type(
        "Contract", (),
        {
            "repo_root": ctx.contract.repo_root,
            "target_branch": "main",
            "verification": {"epic_release": object()},
            "generated_policy_digest": GENERATED_POLICY_DIGEST,
            "digest": "e" * 64,
        },
    )()
    monkeypatch.setattr(kb, "repository_contract_for_metadata", lambda _m: moved)


def _drift_member_pin(ctx, conn, monkeypatch):
    readiness = replace(
        ctx.readiness,
        members=(replace(ctx.readiness.members[0], source_sha="a" * 40),),
    )
    monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)


def _drift_member_set(ctx, conn, monkeypatch):
    extra = EpicReadinessMember(
        story_id=ctx.story_id + "-extra",
        source_sha="b" * 40,
        candidate_sha="c" * 40,
        integrated_at=95,
    )
    readiness = replace(
        ctx.readiness, members=ctx.readiness.members + (extra,)
    )
    monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)


def _drift_fact_intent(ctx, conn, monkeypatch):
    readiness = EpicReadiness(
        ctx.epic_id, EPIC_SHA, (),
        (f"{ctx.story_id}:active_intent",)
    )
    monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)


def _drift_readiness_tip(ctx, conn, monkeypatch):
    moved = "e" * 40
    monkeypatch.setattr(
        kb, "resolve_commit",
        lambda _repo, ref: moved if "epic/" in ref else TARGET_SHA,
    )


def _drift_verification_event(ctx, conn, monkeypatch):
    conn.execute(
        "DELETE FROM task_events WHERE id=?",
        (ctx.prepared.aggregate_verification_event_id,),
    )


def _drift_verification_receipt(ctx, conn, monkeypatch):
    conn.execute(
        "UPDATE task_events SET payload='{}' WHERE id=?",
        (ctx.prepared.aggregate_verification_event_id,),
    )


def _drift_candidate_ref(ctx, conn, monkeypatch):
    drifted_ref = "refs/hermes/releases/exact"
    conn.execute(
        "UPDATE epic_release_snapshots SET candidate_ref=? WHERE id=?",
        (drifted_ref, ctx.prepared.id),
    )
    ctx.prepared = replace(ctx.prepared, candidate_ref=drifted_ref)


# ---------------------------------------------------------------------------
# Parametrized drift invalidation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case_name", "apply_drift", "expected_evidence_key", "expect_ref_deleted"),
    [
        ("epic_tip", _drift_epic_tip, "epic_tip_sha", True),
        ("target", _drift_target, "target_pre_sha", True),
        ("contract", _drift_contract, "repository_contract_digest", True),
        ("member_pin", _drift_member_pin, "members", True),
        ("member_set", _drift_member_set, "members", True),
        ("fact_intent", _drift_fact_intent, "inputs_error", True),
        ("readiness_tip", _drift_readiness_tip, "inputs_error", True),
        ("verification_event", _drift_verification_event, "aggregate_verification_event", True),
        ("verification_receipt", _drift_verification_receipt, "aggregate_verification_event", True),
        ("candidate_ref", _drift_candidate_ref, "candidate_ref", False),
    ],
)
def test_invalidate_epic_release_snapshot_parametrized_drift_invalidates_only_affected_snapshot(
    tmp_path, monkeypatch, case_name, apply_drift, expected_evidence_key, expect_ref_deleted,
):
    delete_calls: list = []

    def wrap_delete(repo_root, *, candidate_ref, candidate_sha):
        delete_calls.append((repo_root, candidate_ref, candidate_sha))
        # Mimic the real delete's namespace gate so an invalid drifted
        # candidate_ref is preserved rather than deleted.
        return candidate_ref.startswith(kb.RELEASE_CANDIDATE_REF_PREFIX)

    monkeypatch.setattr(kb, "delete_release_candidate_ref", wrap_delete)

    with kb.connect(tmp_path / f"{case_name}.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        apply_drift(ctx, conn, monkeypatch)
        result = kb.invalidate_epic_release_snapshot(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert result.kind == "invalidated"
    assert result.snapshot is not None
    assert result.snapshot.status == "invalidated"
    assert expected_evidence_key in result.evidence
    assert result.candidate_ref_deleted is expect_ref_deleted
    assert row["status"] == "invalidated"
    assert delete_calls == [
        (ctx.contract.repo_root, ctx.prepared.candidate_ref, ctx.prepared.release_candidate_sha)
    ]


# ---------------------------------------------------------------------------
# Exact, missing, idempotence, replay, and bulk
# ---------------------------------------------------------------------------


def test_invalidate_epic_release_snapshot_exact_authority_leaves_snapshot_active(
    tmp_path, monkeypatch,
):
    delete_calls: list = []

    def wrap_delete(repo_root, *, candidate_ref, candidate_sha):
        delete_calls.append((repo_root, candidate_ref, candidate_sha))
        return True

    monkeypatch.setattr(kb, "delete_release_candidate_ref", wrap_delete)

    with kb.connect(tmp_path / "exact.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        result = kb.invalidate_epic_release_snapshot(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND kind='epic_release_invalidated'",
            (ctx.epic_id,),
        ).fetchall()

    assert result.kind == "exact"
    assert result.snapshot is not None
    assert result.snapshot.status == "awaiting_push"
    assert result.evidence == {}
    assert result.candidate_ref_deleted is False
    assert row["status"] == "awaiting_push"
    assert delete_calls == []
    assert events == []


def test_invalidate_epic_release_snapshot_without_active_snapshot_returns_missing(
    tmp_path, monkeypatch,
):
    delete_calls: list = []

    def wrap_delete(repo_root, *, candidate_ref, candidate_sha):
        delete_calls.append((repo_root, candidate_ref, candidate_sha))
        return True

    monkeypatch.setattr(kb, "delete_release_candidate_ref", wrap_delete)

    with kb.connect(tmp_path / "missing.db") as conn:
        _, story_id, board_meta, _contract, readiness, _candidate = (
            _release_prepare_fixture(tmp_path, monkeypatch)
        )
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(conn, title="Story")
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        readiness = replace(
            readiness, epic_id=epic_id,
            members=(replace(readiness.members[0], story_id=story_id),),
        )
        monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)
        result = kb.invalidate_epic_release_snapshot(
            conn, epic_id, board="release-board", board_meta=board_meta,
        )

    assert result == EpicReleaseInvalidation("missing", None, {}, False)
    assert delete_calls == []


def test_invalidate_epic_release_snapshot_repeated_invalidation_is_idempotent(
    tmp_path, monkeypatch,
):
    delete_calls: list = []

    def wrap_delete(repo_root, *, candidate_ref, candidate_sha):
        delete_calls.append((repo_root, candidate_ref, candidate_sha))
        return True

    monkeypatch.setattr(kb, "delete_release_candidate_ref", wrap_delete)

    with kb.connect(tmp_path / "idempotent.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        _drift_epic_tip(ctx, conn, monkeypatch)
        first = kb.invalidate_epic_release_snapshot(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        second = kb.invalidate_epic_release_snapshot(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='epic_release_invalidated'",
            (ctx.epic_id,),
        ).fetchone()[0]

    assert first.kind == "invalidated"
    assert second == EpicReleaseInvalidation("missing", None, {}, False)
    assert row["status"] == "invalidated"
    assert len(delete_calls) == 1  # ref cleaned up once
    assert event_count == 2  # drift event + ref-deletion event, no extras


def test_invalidate_epic_release_snapshot_preparation_replay_rebuilds_after_invalidation(
    tmp_path, monkeypatch,
):
    """E05B1 replay creates a replacement only after old authority is durably
    invalidated."""

    new_sha = "a" * 40
    new_ref = RELEASE_CANDIDATE_REF + "-2"
    builder_calls: list = []

    with kb.connect(tmp_path / "replay.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        _drift_epic_tip(ctx, conn, monkeypatch)

        inv = kb.invalidate_epic_release_snapshot(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        assert inv.kind == "invalidated"

        new_key = kb.build_verification_receipt_key(
            None,
            ctx.contract.repo_root,
            candidate_sha=new_sha,
            contract_digest=CONTRACT_DIGEST,
            generated_policy_digest=GENERATED_POLICY_DIGEST,
            gate_kind="epic_release",
            profile_name="epic_release",
        )
        new_verification = kb.VerificationResult(
            status="passed",
            source_sha=ctx.readiness.epic_tip_sha,
            candidate_sha=new_sha,
            contract_digest=CONTRACT_DIGEST,
            profile="epic_release",
            steps=(),
            key=new_key,
        )
        new_candidate = kb.IntegrationCandidate(
            pre_sha=TARGET_SHA,
            candidate_sha=new_sha,
            source_branch=kb.epic_branch_for(ctx.epic_id),
            source_sha=ctx.readiness.epic_tip_sha,
            target_branch="main",
            target_worktree=None,
            scratch_worktree=ctx.contract.repo_root / ".worktrees" / "replay",
            repo_root=ctx.contract.repo_root.resolve(),
            candidate_ref=new_ref,
            verification_result=new_verification,
        )

        def builder(*_a, **_k):
            builder_calls.append("called")
            return new_candidate

        prepared = kb.prepare_epic_release_snapshot(
            conn,
            ctx.epic_id,
            board="release-board",
            board_meta=ctx.board_meta,
            candidate_builder=builder,
        )
        snapshots = conn.execute(
            "SELECT id, status FROM epic_release_snapshots WHERE epic_id=? ORDER BY id",
            (ctx.epic_id,),
        ).fetchall()

    assert inv.snapshot.status == "invalidated"
    assert prepared.epic_id == ctx.epic_id
    assert prepared.status == "awaiting_push"
    assert prepared.release_candidate_sha == new_sha
    assert prepared.candidate_ref == new_ref
    assert len(builder_calls) == 1
    assert len(snapshots) == 2
    assert snapshots[0]["status"] == "invalidated"
    assert snapshots[1]["status"] == "awaiting_push"


def test_invalidate_stale_epic_release_snapshots_only_invalidates_drifted_epic(
    tmp_path, monkeypatch,
):
    delete_calls: list = []

    def wrap_delete(repo_root, *, candidate_ref, candidate_sha):
        delete_calls.append((repo_root, candidate_ref, candidate_sha))
        return True

    monkeypatch.setattr(kb, "delete_release_candidate_ref", wrap_delete)

    with kb.connect(tmp_path / "bulk.db") as conn:
        ctx_a = _prepare_exact_snapshot(conn, monkeypatch, tmp_path, epic_label="A")
        ctx_b = _prepare_exact_snapshot(conn, monkeypatch, tmp_path, epic_label="B")
        # Drift ONLY epic A: its tip moves; epic B keeps its exact authority.
        moved = "e" * 40
        drifted_a = replace(ctx_a.readiness, epic_tip_sha=moved)
        ctx_a.readiness = drifted_a
        monkeypatch.setattr(
            kb,
            "epic_readiness",
            lambda _conn, epic_id, **_kw: (
                drifted_a if epic_id == ctx_a.epic_id else ctx_b.readiness
            ),
        )
        monkeypatch.setattr(
            kb,
            "resolve_commit",
            lambda _repo, ref: (
                moved
                if ref == f"refs/heads/{kb.epic_branch_for(ctx_a.epic_id)}"
                else TARGET_SHA
                if ref == "refs/heads/main"
                else EPIC_SHA
            ),
        )
        results = kb.invalidate_stale_epic_release_snapshots(
            conn, board="release-board", board_meta=ctx_a.board_meta,
        )
        statuses = conn.execute(
            "SELECT epic_id, status FROM epic_release_snapshots ORDER BY epic_id",
        ).fetchall()

    kinds = tuple(r.kind for r in results)
    assert kinds == ("invalidated", "exact") or kinds == ("exact", "invalidated")
    assert results[0].candidate_ref_deleted is (results[0].kind == "invalidated")
    assert len([r for r in results if r.candidate_ref_deleted]) == 1
    assert len([r for r in results if r.kind == "exact"]) == 1
    assert len(delete_calls) == 1
    # Epic B ref never touched — its exact candidate_ref is not in delete_calls.
    # Verify by checking the delete calls only contain ctx_a's candidate info.
    status_by_epic = {row["epic_id"]: row["status"] for row in statuses}
    assert status_by_epic[ctx_a.epic_id] == "invalidated"
    assert status_by_epic[ctx_b.epic_id] == "awaiting_push"


def test_invalidate_stale_epic_release_snapshots_ungoverned_board_returns_empty(
    tmp_path, monkeypatch,
):
    with kb.connect(tmp_path / "ungov.db") as conn:
        # No board metadata — product_board_metadata(None) returns None.
        results = kb.invalidate_stale_epic_release_snapshots(conn)
    assert results == ()


# ---------------------------------------------------------------------------
# E06 — Pinned human release handoff
# ---------------------------------------------------------------------------


def _handoff_observe(
    monkeypatch,
    *,
    local_head: str | None = TARGET_SHA,
    remote_head: str | None = TARGET_SHA,
    remote_available: bool = True,
    remote_name: str = "origin",
):
    def observe(_repo_root, *, target_branch, base_ref):
        return TargetHeadsObservation(
            local_head=local_head,
            remote_head=remote_head,
            remote_name=remote_name,
            remote_available=remote_available,
        )

    monkeypatch.setattr(kb, "observe_target_heads", observe)


def _handoff_delete_calls(monkeypatch):
    calls: list = []

    def wrap_delete(repo_root, *, candidate_ref, candidate_sha):
        calls.append((repo_root, candidate_ref, candidate_sha))
        return True

    monkeypatch.setattr(kb, "delete_release_candidate_ref", wrap_delete)
    return calls


def test_build_release_handoff_returns_truthful_immutable_evidence_with_plain_action(
    tmp_path, monkeypatch,
):
    _handoff_observe(monkeypatch)
    with kb.connect(tmp_path / "handoff.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        handoff = kb.build_epic_release_handoff(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? "
            "AND kind='epic_release_invalidated'",
            (ctx.epic_id,),
        ).fetchall()

    assert isinstance(handoff, EpicReleaseHandoff)
    assert handoff.epic_id == ctx.epic_id
    assert handoff.snapshot == ctx.prepared
    assert handoff.members == (
        EpicReleaseMember(
            snapshot_id=ctx.prepared.id,
            epic_id=ctx.epic_id,
            story_id=ctx.story_id,
            source_sha=SOURCE_SHA,
            candidate_sha=MEMBER_CANDIDATE_SHA,
            integrated_at=90,
        ),
    )
    assert handoff.workflows == ("CI", "Deploy Test")
    assert handoff.aggregate_event_kind == "repository_verification"
    assert (
        handoff.aggregate_event_receipt["candidate_sha"] == AGGREGATE_CANDIDATE_SHA
    )
    assert (
        handoff.aggregate_event_receipt["source_sha"] == EPIC_SHA
    )
    assert handoff.local_target_head == TARGET_SHA
    assert handoff.remote_target_head == TARGET_SHA
    assert handoff.remote_name == "origin"
    assert handoff.checked_at > 0
    # The plain external action carries the pinned ref and full SHAs but is
    # prose for a human — no executable release primitive anywhere.
    assert ctx.prepared.candidate_ref in handoff.action
    assert ctx.prepared.release_candidate_sha in handoff.action
    assert ctx.prepared.target_pre_sha in handoff.action
    assert "git " not in handoff.action
    assert "merge" not in handoff.action
    assert "push" not in handoff.action
    assert events == []


def test_build_release_handoff_refuses_and_invalidates_on_local_target_mismatch(
    tmp_path, monkeypatch,
):
    moved = "f" * 40
    _handoff_observe(monkeypatch, local_head=moved)
    delete_calls = _handoff_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "handoff-local.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        with pytest.raises(EpicReleaseHandoffError) as exc_info:
            kb.build_epic_release_handoff(
                conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
            )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert exc_info.value.code == "local_target_moved"
    assert exc_info.value.evidence["local_head"] == moved
    assert exc_info.value.evidence["snapshot_pre_sha"] == TARGET_SHA
    assert row["status"] == "invalidated"
    assert delete_calls == [
        (
            ctx.contract.repo_root,
            ctx.prepared.candidate_ref,
            ctx.prepared.release_candidate_sha,
        )
    ]


def test_build_release_handoff_refuses_and_invalidates_on_remote_target_mismatch(
    tmp_path, monkeypatch,
):
    moved = "f" * 40
    _handoff_observe(monkeypatch, remote_head=moved)
    delete_calls = _handoff_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "handoff-remote.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        with pytest.raises(EpicReleaseHandoffError) as exc_info:
            kb.build_epic_release_handoff(
                conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
            )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert exc_info.value.code == "remote_target_moved"
    assert exc_info.value.evidence["remote_head"] == moved
    assert exc_info.value.evidence["snapshot_pre_sha"] == TARGET_SHA
    assert row["status"] == "invalidated"
    assert delete_calls == [
        (
            ctx.contract.repo_root,
            ctx.prepared.candidate_ref,
            ctx.prepared.release_candidate_sha,
        )
    ]


def test_build_release_handoff_refuses_without_invalidating_on_remote_unavailability(
    tmp_path, monkeypatch,
):
    _handoff_observe(monkeypatch, remote_head=None, remote_available=False)
    delete_calls = _handoff_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "handoff-unavail.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        with pytest.raises(EpicReleaseHandoffError) as exc_info:
            kb.build_epic_release_handoff(
                conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
            )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()
        invalidations = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? "
            "AND kind='epic_release_invalidated'",
            (ctx.epic_id,),
        ).fetchone()[0]

    assert exc_info.value.code == "remote_unavailable"
    assert exc_info.value.evidence["remote_name"] == "origin"
    # Unavailability cannot prove drift: the snapshot is preserved untouched.
    assert row["status"] == "awaiting_push"
    assert delete_calls == []
    assert invalidations == 0


def test_build_release_handoff_remote_unavailable_then_available_handoff_succeeds(
    tmp_path, monkeypatch,
):
    delete_calls = _handoff_delete_calls(monkeypatch)
    _handoff_observe(monkeypatch, remote_head=None, remote_available=False)

    with kb.connect(tmp_path / "handoff-retry.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        with pytest.raises(EpicReleaseHandoffError) as first:
            kb.build_epic_release_handoff(
                conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
            )
        _handoff_observe(monkeypatch)
        handoff = kb.build_epic_release_handoff(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert first.value.code == "remote_unavailable"
    assert handoff.remote_target_head == TARGET_SHA
    assert row["status"] == "awaiting_push"
    assert delete_calls == []


def test_build_release_handoff_refuses_without_invalidating_on_local_unavailability(
    tmp_path, monkeypatch,
):
    _handoff_observe(monkeypatch, local_head=None)
    delete_calls = _handoff_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "handoff-local-unavail.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        with pytest.raises(EpicReleaseHandoffError) as exc_info:
            kb.build_epic_release_handoff(
                conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
            )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert exc_info.value.code == "local_target_unavailable"
    assert row["status"] == "awaiting_push"
    assert delete_calls == []


def test_build_release_handoff_refuses_and_invalidates_on_snapshot_authority_drift(
    tmp_path, monkeypatch,
):
    _handoff_observe(monkeypatch)
    delete_calls = _handoff_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "handoff-drift.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        _drift_epic_tip(ctx, conn, monkeypatch)
        with pytest.raises(EpicReleaseHandoffError) as exc_info:
            kb.build_epic_release_handoff(
                conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
            )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert exc_info.value.code == "snapshot_drifted"
    evidence = exc_info.value.evidence
    assert isinstance(evidence, dict)
    drift = evidence["drift"]
    assert isinstance(drift, dict)
    assert "epic_tip_sha" in drift
    assert row["status"] == "invalidated"
    assert delete_calls == [
        (
            ctx.contract.repo_root,
            ctx.prepared.candidate_ref,
            ctx.prepared.release_candidate_sha,
        )
    ]


def test_build_release_handoff_refuses_without_active_snapshot(tmp_path, monkeypatch):
    _handoff_observe(monkeypatch)
    with kb.connect(tmp_path / "handoff-none.db") as conn:
        _fixture_epic_id, _story_id, board_meta, _contract, readiness, _candidate = (
            _release_prepare_fixture(tmp_path, monkeypatch)
        )
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(conn, title="Story")
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        readiness = replace(
            readiness, epic_id=epic_id,
            members=(replace(readiness.members[0], story_id=story_id),),
        )
        monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)
        with pytest.raises(EpicReleaseHandoffError) as exc_info:
            kb.build_epic_release_handoff(
                conn, epic_id, board="release-board", board_meta=board_meta,
            )

    assert exc_info.value.code == "no_active_snapshot"


def test_build_release_handoff_refuses_on_ungoverned_board(tmp_path, monkeypatch):
    _handoff_observe(monkeypatch)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: None)
    with kb.connect(tmp_path / "handoff-ungov.db") as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        with pytest.raises(EpicReleaseHandoffError) as exc_info:
            kb.build_epic_release_handoff(conn, epic_id, board="release-board")

    assert exc_info.value.code == "not_governed_epic"


# ---------------------------------------------------------------------------
# E06B — Read-only exact-SHA CI observation
# ---------------------------------------------------------------------------


def _ci_observe(
    monkeypatch,
    *,
    remote_head: str | None = None,
    remote_available: bool = True,
    remote_name: str = "origin",
):
    def observe(_repo_root, *, target_branch, base_ref):
        return TargetHeadsObservation(
            local_head=TARGET_SHA,
            remote_head=remote_head,
            remote_name=remote_name,
            remote_available=remote_available,
        )

    monkeypatch.setattr(kb, "observe_target_heads", observe)


def _ci_workflows(monkeypatch, conclusions):
    calls: list = []

    def observe_runs(_repo_root, *, base_ref, workflows, head_sha):
        calls.append((base_ref, workflows, head_sha))
        return conclusions

    monkeypatch.setattr(kb, "observe_ci_workflow_runs", observe_runs)
    return calls


def _ci_delete_calls(monkeypatch):
    calls: list = []

    def wrap_delete(repo_root, *, candidate_ref, candidate_sha):
        calls.append((repo_root, candidate_ref, candidate_sha))
        return True

    monkeypatch.setattr(kb, "delete_release_candidate_ref", wrap_delete)
    return calls


def test_observe_epic_release_ci_not_yet_pushed_returns_ci_pending_preserved(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch, remote_head=TARGET_SHA)
    run_calls = _ci_workflows(monkeypatch, {})
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-not-pushed.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        result = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status, pushed_sha FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert result.kind == "ci_pending"
    assert result.snapshot == ctx.prepared
    assert result.pushed_sha is None
    assert result.candidate_ref_deleted is False
    assert result.evidence["not_yet_pushed"] is True
    assert row["status"] == "awaiting_push"
    assert row["pushed_sha"] is None
    assert run_calls == []  # CI is never queried until the exact SHA is pushed
    assert delete_calls == []


def test_observe_epic_release_ci_exact_sha_all_workflows_pass_released_and_ref_deleted(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch, remote_head=AGGREGATE_CANDIDATE_SHA)
    run_calls = _ci_workflows(
        monkeypatch, {"CI": "success", "Deploy Test": "success"}
    )
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-released.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        result = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status, pushed_sha FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()
        kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (ctx.epic_id,),
            ).fetchall()
        ]

    assert result.kind == "released"
    assert result.snapshot is not None
    assert result.snapshot.status == "released"
    assert result.pushed_sha == AGGREGATE_CANDIDATE_SHA
    assert result.candidate_ref_deleted is True
    assert row["status"] == "released"
    assert row["pushed_sha"] == AGGREGATE_CANDIDATE_SHA
    # CI is observed for the exact release-candidate SHA only.
    assert run_calls == [
        ("refs/remotes/origin/main", ("CI", "Deploy Test"), AGGREGATE_CANDIDATE_SHA)
    ]
    # Released transition exact-deletes only the snapshot's ref+SHA.
    assert delete_calls == [
        (ctx.contract.repo_root, ctx.prepared.candidate_ref, AGGREGATE_CANDIDATE_SHA)
    ]
    assert "epic_release_released" in kinds
    assert "epic_release_invalidated" not in kinds


def test_observe_epic_release_ci_failure_marks_ci_failed_then_same_sha_later_pass_releases(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch, remote_head=AGGREGATE_CANDIDATE_SHA)
    _ci_workflows(monkeypatch, {"CI": "failure", "Deploy Test": "success"})
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-failed.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        first = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status, pushed_sha FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()
        # Manual recovery is retained: the snapshot stays active and the
        # candidate ref is preserved.
        assert first.kind == "ci_failed"
        assert first.candidate_ref_deleted is False
        assert row["status"] == "ci_failed"
        assert row["pushed_sha"] == AGGREGATE_CANDIDATE_SHA
        assert delete_calls == []

        # Same SHA, later observation: every workflow now passes.
        _ci_workflows(monkeypatch, {"CI": "success", "Deploy Test": "success"})
        second = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row2 = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert second.kind == "released"
    assert second.pushed_sha == AGGREGATE_CANDIDATE_SHA
    assert row2["status"] == "released"
    assert delete_calls == [
        (ctx.contract.repo_root, ctx.prepared.candidate_ref, AGGREGATE_CANDIDATE_SHA)
    ]


def test_observe_epic_release_ci_different_sha_after_pinned_push_invalidates(
    tmp_path, monkeypatch,
):
    moved = "f" * 40
    _ci_observe(monkeypatch, remote_head=AGGREGATE_CANDIDATE_SHA)
    _ci_workflows(monkeypatch, {"CI": None, "Deploy Test": None})
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-moved.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        first = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        assert first.kind == "ci_pending"
        assert delete_calls == []

        # The remote moves to a different SHA after the candidate was
        # pinned pushed: durable invalidation with exact-SHA ref cleanup.
        _ci_observe(monkeypatch, remote_head=moved)
        second = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert second.kind == "invalidated"
    assert second.evidence["remote_head"] == moved
    assert row["status"] == "invalidated"
    assert delete_calls == [
        (ctx.contract.repo_root, ctx.prepared.candidate_ref, AGGREGATE_CANDIDATE_SHA)
    ]


def test_observe_epic_release_ci_running_workflow_stays_ci_pending(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch, remote_head=AGGREGATE_CANDIDATE_SHA)
    _ci_workflows(monkeypatch, {"CI": None, "Deploy Test": None})
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-running.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        result = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status, pushed_sha FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert result.kind == "ci_pending"
    assert result.pushed_sha == AGGREGATE_CANDIDATE_SHA
    assert result.candidate_ref_deleted is False
    assert row["status"] == "ci_pending"
    assert row["pushed_sha"] == AGGREGATE_CANDIDATE_SHA
    assert delete_calls == []


def test_observe_epic_release_ci_remote_unavailable_preserves_snapshot(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch, remote_head=None, remote_available=False)
    run_calls = _ci_workflows(monkeypatch, {})
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-remote-unavail.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        result = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert result.kind == "unavailable"
    assert result.snapshot == ctx.prepared
    assert row["status"] == "awaiting_push"
    assert run_calls == []
    assert delete_calls == []


def test_observe_epic_release_ci_provider_unavailable_preserves_snapshot(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch, remote_head=AGGREGATE_CANDIDATE_SHA)
    _ci_workflows(monkeypatch, None)
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-provider-unavail.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        result = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status, pushed_sha FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert result.kind == "unavailable"
    assert result.pushed_sha == AGGREGATE_CANDIDATE_SHA
    assert row["status"] == "ci_pending"  # push was pinned; CI unknown
    assert delete_calls == []


def test_observe_epic_release_ci_authority_drift_invalidates_exactly(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch, remote_head=AGGREGATE_CANDIDATE_SHA)
    _ci_workflows(monkeypatch, {"CI": "success", "Deploy Test": "success"})
    delete_calls = _ci_delete_calls(monkeypatch)

    with kb.connect(tmp_path / "ci-drift.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        _drift_epic_tip(ctx, conn, monkeypatch)
        result = kb.observe_epic_release_ci(
            conn, ctx.epic_id, board="release-board", board_meta=ctx.board_meta,
        )
        row = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?",
            (ctx.prepared.id,),
        ).fetchone()

    assert result.kind == "invalidated"
    assert result.snapshot is not None
    assert result.snapshot.status == "invalidated"
    assert row["status"] == "invalidated"
    assert delete_calls == [
        (ctx.contract.repo_root, ctx.prepared.candidate_ref, AGGREGATE_CANDIDATE_SHA)
    ]


def test_observe_epic_release_ci_without_active_snapshot_returns_missing(
    tmp_path, monkeypatch,
):
    _ci_observe(monkeypatch)
    _ci_workflows(monkeypatch, {})
    with kb.connect(tmp_path / "ci-missing.db") as conn:
        _fixture_epic_id, _fixture_story_id, board_meta, _contract, readiness, _candidate = (
            _release_prepare_fixture(tmp_path, monkeypatch)
        )
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(conn, title="Story")
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        readiness = replace(
            readiness, epic_id=epic_id,
            members=(replace(readiness.members[0], story_id=story_id),),
        )
        monkeypatch.setattr(kb, "epic_readiness", lambda *_a, **_k: readiness)
        result = kb.observe_epic_release_ci(
            conn, epic_id, board="release-board", board_meta=board_meta,
        )

    assert result.kind == "missing"
    assert result.snapshot is None
    assert result.pushed_sha is None


def test_observe_epic_release_ci_refuses_on_ungoverned_board(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: None)
    with kb.connect(tmp_path / "ci-ungov.db") as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        with pytest.raises(EpicReleaseCIObservationError) as exc_info:
            kb.observe_epic_release_ci(conn, epic_id, board="release-board")

    assert exc_info.value.code == "not_governed_epic"


def test_observe_epic_release_ci_refuses_inside_active_transaction(
    tmp_path, monkeypatch,
):
    with kb.connect(tmp_path / "ci-txn.db") as conn:
        ctx = _prepare_exact_snapshot(conn, monkeypatch, tmp_path)
        conn.execute("BEGIN IMMEDIATE")
        assert conn.in_transaction is True
        try:
            with pytest.raises(EpicReleaseCIObservationError) as exc_info:
                kb.observe_epic_release_ci(
                    conn, ctx.epic_id, board="release-board",
                    board_meta=ctx.board_meta,
                )
        finally:
            conn.execute("ROLLBACK")

    assert exc_info.value.code == "active_transaction"
