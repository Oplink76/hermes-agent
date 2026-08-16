"""Typed persistence tests for Epic-member integration intents."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
import hermes_cli.kanban_story_integration as integration_module
from hermes_cli.kanban_story_integration import (
    IntegrationFact,
    IntegrationIntent,
    IntegrationKey,
    RecoveryCounts,
    advance_prepared_intent,
    claim_next_intent,
    enqueue_approved_story,
    finish_intent,
    integration_intent_from_row,
    prepare_claimed_intent,
    recover_expired_intents,
)
from hermes_cli.kanban_product_outcomes import (
    ApprovedCandidate,
    CandidateEligibility,
    PassedTest,
)
from hermes_cli.kanban_repository import (
    PreparedRefCASResult,
    PreparedRefRecoveryResult,
    VerificationResult,
    build_verification_receipt_key,
)


SOURCE_SHA = "1" * 40
BASE_SHA = "2" * 40
TARGET_SHA = "3" * 40
CANDIDATE_SHA = "4" * 40


def _claim_board_metadata(repo_root) -> dict[str, object]:
    return {
        "preset": "product",
        "default_workdir": str(repo_root),
        "product_workflow": {"handoff_v2": True},
        "repository": {
            "base_ref": "refs/remotes/origin/main",
            "target_branch": "main",
            "verification_profiles": {
                "story_integration": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
                "epic_release": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
            },
            "ci_observation": {
                "provider": "github_actions",
                "required_workflows": ["CI"],
            },
            "boundary_evidence": {
                "test_globs": ["tests/**"],
                "fixture_globs": ["tests/fixtures/**"],
                "generated_paths": [],
            },
        },
    }


def _insert_claimable_intent(
    conn: sqlite3.Connection,
    *,
    source_sha: str = SOURCE_SHA,
    branch: str = "story/one",
) -> IntegrationKey:
    now = int(time.time())
    epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
    story_id = kb.create_task(
        conn,
        title="Story",
        workflow_template_id="product",
        current_step_key="review",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "tester": {"agent": "tester", "result": "passed"},
        },
        "test_branch": branch,
        "test_head_sha": source_sha,
    }
    review_metadata = {
        "workflow_outcome": {"verdict": "approved"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "reviewer": {"agent": "reviewer"},
        },
        "review_branch": branch,
        "review_base_sha": BASE_SHA,
        "review_head_sha": source_sha,
    }
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
        "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
        (story_id, json.dumps(test_metadata), now - 4, now - 3),
    )
    review_run_id = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
        "VALUES (?, 'review', 'completed', 'advanced', ?, ?, ?)",
        (story_id, json.dumps(review_metadata), now - 2, now - 1),
    ).lastrowid
    with kb.authorized_governance_write(), kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product_epic', "
            "current_step_key='collecting_members', status='todo', assignee=NULL, "
            "running=0, blocked=0, current_run_id=NULL WHERE id=?",
            (epic_id,),
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product', "
            "current_step_key='integration_pending', status='review', assignee=NULL, "
            "running=0, blocked=0, current_run_id=NULL, branch_name=? WHERE id=?",
            (branch, story_id),
        )
        conn.execute(
            "INSERT INTO story_integration_intents "
            "(epic_id, story_id, source_sha, source_branch, review_run_id, "
            "review_base_sha, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                epic_id,
                story_id,
                source_sha,
                branch,
                review_run_id,
                BASE_SHA,
                now,
                now,
            ),
        )
    return IntegrationKey(epic_id, story_id, source_sha)


def _intent_values(*, status: str = "prepared") -> tuple[object, ...]:
    return (
        "epic-1",
        "story-1",
        SOURCE_SHA,
        "feature/story-1",
        17,
        BASE_SHA,
        status,
        "owner-1",
        200,
        2,
        TARGET_SHA,
        CANDIDATE_SHA,
        "refs/hermes/candidates/story-1",
        91,
        None,
        100,
        110,
    )


def _insert_intent(conn: sqlite3.Connection, *, status: str = "prepared") -> None:
    conn.execute(
        """
        INSERT INTO story_integration_intents (
            epic_id, story_id, source_sha, source_branch,
            review_run_id, review_base_sha, status, claim_lock,
            claim_expires, attempt_count, target_pre_sha, candidate_sha,
            candidate_ref, verification_event_id, last_failure_code,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _intent_values(status=status),
    )


def test_story_integration_schema_has_exact_columns_primary_key_and_claim_index(
    tmp_path,
):
    with kb.connect(tmp_path / "fresh.db") as conn:
        info = conn.execute(
            "PRAGMA table_info(story_integration_intents)"
        ).fetchall()
        index_columns = conn.execute(
            "PRAGMA index_info(idx_story_integration_intents_claim)"
        ).fetchall()

    assert tuple(row["name"] for row in info) == (
        "epic_id",
        "story_id",
        "source_sha",
        "source_branch",
        "review_run_id",
        "review_base_sha",
        "status",
        "claim_lock",
        "claim_expires",
        "attempt_count",
        "target_pre_sha",
        "candidate_sha",
        "candidate_ref",
        "verification_event_id",
        "last_failure_code",
        "created_at",
        "updated_at",
    )
    assert {row["name"]: row["pk"] for row in info if row["pk"]} == {
        "epic_id": 1,
        "story_id": 2,
        "source_sha": 3,
    }
    assert tuple(row["name"] for row in index_columns) == (
        "status",
        "claim_expires",
        "created_at",
    )


def test_story_integration_schema_round_trips_frozen_intent(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        _insert_intent(conn)
        row = conn.execute("SELECT * FROM story_integration_intents").fetchone()

    intent = integration_intent_from_row(row)

    assert intent == IntegrationIntent(
        key=IntegrationKey("epic-1", "story-1", SOURCE_SHA),
        source_branch="feature/story-1",
        review_run_id=17,
        review_base_sha=BASE_SHA,
        status="prepared",
        claim_lock="owner-1",
        claim_expires=200,
        attempt_count=2,
        target_pre_sha=TARGET_SHA,
        candidate_sha=CANDIDATE_SHA,
        candidate_ref="refs/hermes/candidates/story-1",
        verification_event_id=91,
        last_failure_code=None,
        created_at=100,
        updated_at=110,
    )
    with pytest.raises(FrozenInstanceError):
        intent.status = "integrated"  # type: ignore[misc]


def test_story_integration_schema_enforces_composite_uniqueness(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        _insert_intent(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_intent(conn)


@pytest.mark.parametrize("status", ["queued", "done", ""])
def test_story_integration_schema_refuses_illegal_status(tmp_path, status):
    with kb.connect(tmp_path / "fresh.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_intent(conn, status=status)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha", "abc"),
        ("review_base_sha", "A" * 40),
        ("target_pre_sha", "3" * 39),
        ("candidate_sha", "not-a-sha"),
        ("status", "queued"),
    ],
)
def test_story_integration_parser_refuses_malformed_sha_or_status(field, value):
    row = {
        "epic_id": "epic-1",
        "story_id": "story-1",
        "source_sha": SOURCE_SHA,
        "source_branch": "feature/story-1",
        "review_run_id": 17,
        "review_base_sha": BASE_SHA,
        "status": "prepared",
        "claim_lock": None,
        "claim_expires": None,
        "attempt_count": 0,
        "target_pre_sha": TARGET_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_ref": None,
        "verification_event_id": None,
        "last_failure_code": None,
        "created_at": 100,
        "updated_at": 100,
    }
    row[field] = value

    with pytest.raises(ValueError):
        integration_intent_from_row(row)


def test_story_integration_parser_keeps_verification_event_audit_only_nullable():
    row = {
        "epic_id": "epic-1",
        "story_id": "story-1",
        "source_sha": SOURCE_SHA,
        "source_branch": "feature/story-1",
        "review_run_id": 17,
        "review_base_sha": BASE_SHA,
        "status": "integrated",
        "claim_lock": None,
        "claim_expires": None,
        "attempt_count": 1,
        "target_pre_sha": TARGET_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_ref": None,
        "verification_event_id": None,
        "last_failure_code": None,
        "created_at": 100,
        "updated_at": 120,
    }

    assert integration_intent_from_row(row).verification_event_id is None


def test_integration_enqueued_transaction_is_idempotent_and_uses_zero_git(
    tmp_path, monkeypatch
):
    branch = "story/one"
    now = int(time.time())
    approved = ApprovedCandidate(
        run_id=0,
        branch=branch,
        base_sha=BASE_SHA,
        source_sha=SOURCE_SHA,
        reviewer_provider="reviewer",
        writer_provider="developer",
    )
    passed = PassedTest(
        run_id=0,
        branch=branch,
        source_sha=SOURCE_SHA,
        tester_provider="tester",
        writer_provider="developer",
    )
    eligibility = CandidateEligibility(source_sha=SOURCE_SHA, non_empty=True)
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "tester": {"agent": "tester", "result": "passed"},
        },
        "test_branch": branch,
        "test_head_sha": SOURCE_SHA,
    }
    review_metadata = {
        "workflow_outcome": {"verdict": "approved"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "reviewer": {"agent": "reviewer"},
        },
        "review_branch": branch,
        "review_base_sha": BASE_SHA,
        "review_head_sha": SOURCE_SHA,
    }

    monkeypatch.setattr(
        kb,
        "_integration_git",
        lambda *_args, **_kwargs: pytest.fail("enqueue must not call Git"),
    )
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("enqueue must not spawn Git"),
    )
    with kb.connect(tmp_path / "enqueue.db") as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="review",
        )
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        test_run_id = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(test_metadata), now - 2, now - 1),
        ).lastrowid
        review_run_id = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, metadata, started_at) "
            "VALUES (?, 'review', 'running', ?, ?)",
            (story_id, json.dumps(review_metadata), now),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status='running', running=1, assignee='reviewer', "
            "current_run_id=? WHERE id=?",
            (review_run_id, story_id),
        )
        approved = ApprovedCandidate(
            run_id=review_run_id,
            branch=branch,
            base_sha=BASE_SHA,
            source_sha=SOURCE_SHA,
            reviewer_provider="reviewer",
            writer_provider="developer",
        )
        passed = PassedTest(
            run_id=test_run_id,
            branch=branch,
            source_sha=SOURCE_SHA,
            tester_provider="tester",
            writer_provider="developer",
        )

        first = enqueue_approved_story(
            conn,
            epic_id=epic_id,
            story_id=story_id,
            approved=approved,
            passed=passed,
            eligibility=eligibility,
            expected_run_id=review_run_id,
            summary="approved",
            metadata=review_metadata,
        )
        replay = enqueue_approved_story(
            conn,
            epic_id=epic_id,
            story_id=story_id,
            approved=approved,
            passed=passed,
            eligibility=eligibility,
            expected_run_id=review_run_id,
        )
        stale_test_metadata = dict(test_metadata)
        stale_test_metadata["test_head_sha"] = "9" * 40
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(stale_test_metadata), test_run_id),
        )
        with pytest.raises(ValueError, match="stale"):
            enqueue_approved_story(
                conn,
                epic_id=epic_id,
                story_id=story_id,
                approved=approved,
                passed=passed,
                eligibility=eligibility,
                expected_run_id=review_run_id,
            )
        story = conn.execute(
            "SELECT workflow_template_id, current_step_key, status, assignee, "
            "current_run_id FROM tasks WHERE id=?",
            (story_id,),
        ).fetchone()
        epic = conn.execute(
            "SELECT workflow_template_id, current_step_key, status, assignee "
            "FROM tasks WHERE id=?",
            (epic_id,),
        ).fetchone()
        events = [event for event in kb.list_events(conn, story_id)
                  if event.kind == "story_integration_enqueued"]

    assert replay == first
    assert first.status == "pending"
    assert tuple(story) == ("product", "integration_pending", "review", None, None)
    assert tuple(epic) == ("product_epic", "collecting_members", "todo", None)
    assert len(events) == 1
    assert events[0].payload == {
        "epic_id": epic_id,
        "story_id": story_id,
        "source_sha": SOURCE_SHA,
        "review_run_id": review_run_id,
    }


def test_claim_next_intent_has_one_winner_across_two_connections(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "claim.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        expected_key = _insert_claimable_intent(conn)

    repository_calls = []
    barrier = threading.Barrier(2)

    def repository_check(contract, approved, passed):
        repository_calls.append((contract, approved, passed))
        return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)

    def claim(owner: str):
        with kb.connect(db_path) as conn:
            barrier.wait(timeout=5)
            return claim_next_intent(
                conn,
                owner,
                60,
                repository_check=repository_check,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("owner-a", "owner-b")))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].key == expected_key
    assert winners[0].status == "running"
    assert winners[0].attempt_count == 1
    assert len(repository_calls) == 1
    with kb.connect(db_path) as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM story_integration_intents WHERE status='running'"
        ).fetchone()[0]
    assert running == 1


def test_claim_next_intent_reclaims_expired_intent_before_new_work(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "reclaim.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        _insert_claimable_intent(conn, branch="story/first")
        _insert_claimable_intent(conn, source_sha="5" * 40, branch="story/second")

    repository_calls = []

    def repository_check(contract, approved, passed):
        repository_calls.append(approved.source_sha)
        return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)

    monkeypatch.setattr(time, "time", lambda: 100)
    with kb.connect(db_path) as conn:
        first = claim_next_intent(
            conn,
            "owner-a",
            60,
            repository_check=repository_check,
        )
        assert claim_next_intent(
            conn,
            "owner-b",
            60,
            repository_check=repository_check,
        ) is None

    monkeypatch.setattr(time, "time", lambda: 161)
    with kb.connect(db_path) as conn:
        reclaimed = claim_next_intent(
            conn,
            "owner-b",
            60,
            repository_check=repository_check,
        )

    assert first is not None and reclaimed is not None
    assert first.key == reclaimed.key
    assert first.claim_lock != reclaimed.claim_lock
    assert reclaimed.attempt_count == 2
    assert repository_calls == [first.key.source_sha, first.key.source_sha]


def test_claim_next_intent_fails_closed_on_running_lease_without_expiry(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "missing-expiry.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        first_key = _insert_claimable_intent(conn, branch="story/first")
        _insert_claimable_intent(conn, source_sha="5" * 40, branch="story/second")
        conn.execute(
            "UPDATE story_integration_intents SET status='running', "
            "claim_lock='owner:existing', claim_expires=NULL "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (first_key.epic_id, first_key.story_id, first_key.source_sha),
        )

    repository_calls = []
    with kb.connect(db_path) as conn:
        claimed = claim_next_intent(
            conn,
            "owner-new",
            60,
            repository_check=lambda *_args: repository_calls.append(True),
        )
        running = conn.execute(
            "SELECT COUNT(*) FROM story_integration_intents WHERE status='running'"
        ).fetchone()[0]

    assert claimed is None
    assert repository_calls == []
    assert running == 1


@pytest.mark.parametrize(
    "stale_case",
    [
        "membership",
        "test",
        "review",
        "provider",
        "test_provider",
        "sha",
        "source_branch",
        "directive",
        "phase",
        "contract",
    ],
)
def test_claim_next_intent_refuses_stale_authority_before_repository_access(
    tmp_path, monkeypatch, stale_case
):
    db_path = tmp_path / f"stale-{stale_case}.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        key = _insert_claimable_intent(conn)
        if stale_case == "membership":
            conn.execute(
                "DELETE FROM epic_memberships WHERE epic_id=? AND task_id=?",
                (key.epic_id, key.story_id),
            )
        elif stale_case in {"test", "review", "provider", "test_provider", "sha"}:
            phase = "test" if stale_case in {"test", "test_provider"} else "review"
            row = conn.execute(
                "SELECT id, metadata FROM task_runs WHERE task_id=? AND step_key=? "
                "ORDER BY id DESC LIMIT 1",
                (key.story_id, phase),
            ).fetchone()
            metadata = json.loads(row["metadata"])
            if stale_case in {"test", "review"}:
                metadata["workflow_outcome"] = {
                    "verdict": "changes_requested",
                    "target_step": "development",
                    "findings": ["later evidence rejected the candidate"],
                }
            elif stale_case == "provider":
                metadata["ai_provenance"]["reviewer"]["agent"] = "developer"
            elif stale_case == "test_provider":
                metadata["ai_provenance"]["tester"]["agent"] = "developer"
            else:
                metadata["review_head_sha"] = "9" * 40
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), row["id"]),
            )
        elif stale_case == "source_branch":
            conn.execute(
                "UPDATE tasks SET branch_name='story/moved' WHERE id=?",
                (key.story_id,),
            )
        elif stale_case == "directive":
            kb.create_rework_directive(
                conn,
                key.story_id,
                origin_kind="review",
                origin_phase="review",
                target_phase="development",
                rejected_branch="story/one",
                rejected_sha=SOURCE_SHA,
                findings=["rework is active"],
            )
        elif stale_case == "phase":
            conn.execute(
                "UPDATE tasks SET current_step_key='review' WHERE id=?",
                (key.story_id,),
            )
        else:
            repository = board_metadata["repository"]
            assert isinstance(repository, dict)
            repository["verification_profiles"].pop("story_integration")

    repository_calls = []

    def repository_check(contract, approved, passed):
        repository_calls.append((contract, approved, passed))
        return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)

    with kb.connect(db_path) as conn:
        claimed = claim_next_intent(
            conn,
            "owner",
            60,
            repository_check=repository_check,
        )

    assert claimed is None
    assert repository_calls == []


def _passed_candidate(tmp_path, contract, key):
    profile = contract.verification["story_integration"]
    receipt_key = build_verification_receipt_key(
        profile,
        tmp_path,
        candidate_sha=CANDIDATE_SHA,
        contract_digest=contract.digest,
        generated_policy_digest=contract.generated_policy_digest,
        gate_kind="story_integration",
        profile_name="story_integration",
    )
    verification = VerificationResult(
        status="passed",
        source_sha=SOURCE_SHA,
        candidate_sha=CANDIDATE_SHA,
        contract_digest=contract.digest,
        profile="story_integration",
        steps=(),
        key=receipt_key,
    )
    return kb.IntegrationCandidate(
        pre_sha=TARGET_SHA,
        candidate_sha=CANDIDATE_SHA,
        source_branch="story/one",
        source_sha=SOURCE_SHA,
        target_branch=kb.epic_branch_for(key.epic_id),
        target_worktree=None,
        scratch_worktree=tmp_path / "removed-scratch",
        repo_root=tmp_path,
        candidate_ref="refs/hermes/integration-candidates/exact",
        verification_result=verification,
    )


def test_prepare_claimed_candidate_persists_exact_receipt_atomically_without_apply(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "prepared.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    monkeypatch.setattr(
        kb,
        "_fast_forward_target",
        lambda *_args, **_kwargs: pytest.fail("preparation must not move the target ref"),
    )
    with kb.connect(db_path) as conn:
        key = _insert_claimable_intent(conn)
        claimed = claim_next_intent(
            conn,
            "owner",
            60,
            repository_check=lambda _contract, approved, _passed: CandidateEligibility(
                source_sha=approved.source_sha, non_empty=True
            ),
        )
        assert claimed is not None
        contract = kb.repository_contract_for_metadata(board_metadata)
        assert contract is not None
        candidate = _passed_candidate(tmp_path, contract, key)

        def candidate_builder(
            repo_root,
            target_branch,
            source_branch,
            message,
            **kwargs,
        ):
            assert conn.in_transaction is False
            assert repo_root == tmp_path.resolve()
            assert target_branch == kb.epic_branch_for(key.epic_id)
            assert source_branch == "story/one"
            assert message == f"integrate story {key.story_id}"
            assert kwargs["expected_source_sha"] == SOURCE_SHA
            return candidate

        prepared = prepare_claimed_intent(
            conn, claimed, candidate_builder=candidate_builder
        )
        replay = prepare_claimed_intent(
            conn,
            prepared,
            candidate_builder=lambda *_args, **_kwargs: pytest.fail(
                "prepared replay must reuse the exact receipt"
            ),
        )
        events = [
            event
            for event in kb.list_events(conn, key.story_id)
            if event.kind == "repository_verification"
        ]

    assert replay == prepared
    assert prepared.status == "prepared"
    assert prepared.claim_lock is None
    assert prepared.claim_expires is None
    assert prepared.target_pre_sha == TARGET_SHA
    assert prepared.candidate_sha == CANDIDATE_SHA
    assert prepared.candidate_ref == "refs/hermes/integration-candidates/exact"
    assert len(events) == 1
    assert prepared.verification_event_id == events[0].id
    assert events[0].payload["receipt"]["key"]["candidate_sha"] == CANDIDATE_SHA


@pytest.mark.parametrize("kind", ["advanced", "reflected", "target_moved"])
def test_advance_prepared_intent_forwards_exact_cas_without_db_fact_completion(
    tmp_path, monkeypatch, kind
):
    db_path = tmp_path / f"advance-{kind}.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        key = _insert_claimable_intent(conn)
        claimed = claim_next_intent(
            conn,
            "owner",
            60,
            repository_check=lambda _contract, approved, _passed: CandidateEligibility(
                source_sha=approved.source_sha, non_empty=True
            ),
        )
        assert claimed is not None
        contract = kb.repository_contract_for_metadata(board_metadata)
        assert contract is not None
        prepared = prepare_claimed_intent(
            conn,
            claimed,
            candidate_builder=lambda *_args, **_kwargs: _passed_candidate(
                tmp_path, contract, key
            ),
        )
        calls = []

        def exact_cas(repo_root, **kwargs):
            calls.append((repo_root, kwargs))
            current = CANDIDATE_SHA if kind in {"advanced", "reflected"} else TARGET_SHA
            return PreparedRefCASResult(kind, current)

        monkeypatch.setattr(
            integration_module, "advance_prepared_candidate_ref", exact_cas
        )
        before_changes = conn.total_changes
        before_intent = conn.execute(
            "SELECT * FROM story_integration_intents WHERE epic_id=? AND story_id=? "
            "AND source_sha=?",
            (key.epic_id, key.story_id, key.source_sha),
        ).fetchone()
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (key.story_id,)
        ).fetchone()[0]

        result = advance_prepared_intent(conn, prepared)

        after_intent = conn.execute(
            "SELECT * FROM story_integration_intents WHERE epic_id=? AND story_id=? "
            "AND source_sha=?",
            (key.epic_id, key.story_id, key.source_sha),
        ).fetchone()
        after_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=?", (key.story_id,)
        ).fetchone()[0]
        after_changes = conn.total_changes

    assert result == PreparedRefCASResult(
        kind, CANDIDATE_SHA if kind in {"advanced", "reflected"} else TARGET_SHA
    )
    assert calls == [
        (
            tmp_path.resolve(),
            {
                "target_ref": f"refs/heads/{kb.epic_branch_for(key.epic_id)}",
                "candidate_ref": "refs/hermes/integration-candidates/exact",
                "pre_sha": TARGET_SHA,
                "candidate_sha": CANDIDATE_SHA,
            },
        )
    ]
    assert dict(after_intent) == dict(before_intent)
    assert after_events == before_events
    assert after_changes == before_changes


def test_prepare_claimed_candidate_crash_replay_leaves_one_durable_prepared_record(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "prepared-replay.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        key = _insert_claimable_intent(conn)
        claimed = claim_next_intent(
            conn,
            "owner",
            60,
            repository_check=lambda _contract, approved, _passed: CandidateEligibility(
                source_sha=approved.source_sha, non_empty=True
            ),
        )
        assert claimed is not None
        contract = kb.repository_contract_for_metadata(board_metadata)
        assert contract is not None
        candidate = _passed_candidate(tmp_path, contract, key)
        real_append = kb._append_event
        attempts = 0

        def interrupt_after_event(*args, **kwargs):
            nonlocal attempts
            event_id = real_append(*args, **kwargs)
            attempts += 1
            if attempts == 1:
                raise RuntimeError("simulated crash before prepared state commit")
            return event_id

        monkeypatch.setattr(kb, "_append_event", interrupt_after_event)
        builder_calls = []

        def candidate_builder(*_args, **_kwargs):
            assert conn.in_transaction is False
            builder_calls.append(True)
            return candidate

        with pytest.raises(RuntimeError, match="simulated crash"):
            prepare_claimed_intent(
                conn, claimed, candidate_builder=candidate_builder
            )
        interrupted = conn.execute(
            "SELECT * FROM story_integration_intents WHERE epic_id=? AND story_id=? "
            "AND source_sha=?",
            (key.epic_id, key.story_id, key.source_sha),
        ).fetchone()
        assert interrupted["status"] == "running"
        assert interrupted["verification_event_id"] is None

        prepared = prepare_claimed_intent(
            conn, claimed, candidate_builder=candidate_builder
        )
        prepared_count = conn.execute(
            "SELECT COUNT(*) FROM story_integration_intents WHERE status='prepared'"
        ).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? "
            "AND kind='repository_verification'",
            (key.story_id,),
        ).fetchone()[0]

    assert prepared.status == "prepared"
    assert builder_calls == [True, True]
    assert prepared_count == 1
    assert event_count == 1


def test_prepare_claimed_candidate_refuses_active_db_transaction(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "prepared-transaction.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        _insert_claimable_intent(conn)
        claimed = claim_next_intent(
            conn,
            "owner",
            60,
            repository_check=lambda _contract, approved, _passed: CandidateEligibility(
                source_sha=approved.source_sha, non_empty=True
            ),
        )
        assert claimed is not None
        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(ValueError, match="no active DB transaction"):
                prepare_claimed_intent(
                    conn,
                    claimed,
                    candidate_builder=lambda *_args, **_kwargs: pytest.fail(
                        "candidate must not build inside a DB transaction"
                    ),
                )
        finally:
            conn.execute("ROLLBACK")


def test_prepared_candidate_replay_rejects_mismatched_receipt(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "prepared-mismatch.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        _insert_intent(conn)
        row = conn.execute("SELECT * FROM story_integration_intents").fetchone()
        prepared = integration_intent_from_row(row)
        conn.execute(
            "INSERT INTO task_events "
            "(task_id, kind, payload, created_at) VALUES (?, 'repository_verification', ?, ?)",
            (prepared.key.story_id, json.dumps({"status": "passed"}), 100),
        )

        with pytest.raises(ValueError, match="receipt"):
            prepare_claimed_intent(
                conn,
                prepared,
                candidate_builder=lambda *_args, **_kwargs: pytest.fail(
                    "mismatched prepared receipt must not rebuild"
                ),
            )


def _prepared_intent(tmp_path, monkeypatch, conn):
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    key = _insert_claimable_intent(conn)
    claimed = claim_next_intent(
        conn,
        "owner",
        60,
        repository_check=lambda _contract, approved, _passed: CandidateEligibility(
            source_sha=approved.source_sha, non_empty=True
        ),
    )
    assert claimed is not None
    contract = kb.repository_contract_for_metadata(board_metadata)
    assert contract is not None
    prepared = prepare_claimed_intent(
        conn,
        claimed,
        candidate_builder=lambda *_args, **_kwargs: _passed_candidate(
            tmp_path, contract, key
        ),
    )
    return key, prepared


def _insert_active_release_snapshot(conn, epic_id: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO epic_release_snapshots (
                epic_id, epic_tip_sha, target_branch, target_pre_sha,
                release_candidate_sha, candidate_ref,
                aggregate_verification_event_id, repository_contract_digest,
                status, created_at, updated_at
            ) VALUES (?, ?, 'main', ?, ?, ?, 1, ?, 'awaiting_push', 1, 1)
            """,
            (
                epic_id,
                TARGET_SHA,
                TARGET_SHA,
                CANDIDATE_SHA,
                "refs/hermes/integration-candidates/release",
                "d" * 64,
            ),
        ).lastrowid
    )


def test_finish_intent_atomically_persists_fact_task_event_and_snapshot_invalidation(
    tmp_path, monkeypatch
):
    with kb.connect(tmp_path / "finish.db") as conn:
        key, prepared = _prepared_intent(tmp_path, monkeypatch, conn)
        snapshot_id = _insert_active_release_snapshot(conn, key.epic_id)
        cleanup_observations = []

        def cleanup(_repo_root, *, candidate_ref, candidate_sha):
            fact = conn.execute(
                "SELECT candidate_sha FROM epic_story_integrations "
                "WHERE epic_id=? AND story_id=? AND source_sha=?",
                (key.epic_id, key.story_id, key.source_sha),
            ).fetchone()
            story = conn.execute(
                "SELECT status, current_step_key FROM tasks WHERE id=?",
                (key.story_id,),
            ).fetchone()
            cleanup_observations.append(
                (fact["candidate_sha"], tuple(story), candidate_ref, candidate_sha)
            )
            return True

        monkeypatch.setattr(
            integration_module, "delete_prepared_candidate_ref", cleanup
        )
        fact = finish_intent(
            conn,
            prepared,
            PreparedRefCASResult("advanced", CANDIDATE_SHA),
        )
        replay = finish_intent(
            conn,
            prepared,
            PreparedRefCASResult("reflected", CANDIDATE_SHA),
        )
        intent = conn.execute(
            "SELECT status, candidate_ref FROM story_integration_intents "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (key.epic_id, key.story_id, key.source_sha),
        ).fetchone()
        snapshot = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? "
            "AND kind='story_integrated'",
            (key.story_id,),
        ).fetchall()

    assert fact == replay == IntegrationFact(
        key.epic_id, key.story_id, SOURCE_SHA, CANDIDATE_SHA, fact.integrated_at
    )
    assert tuple(intent) == ("integrated", None)
    assert snapshot["status"] == "invalidated"
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["candidate_sha"] == CANDIDATE_SHA
    assert cleanup_observations == [
        (
            CANDIDATE_SHA,
            ("done", "done"),
            "refs/hermes/integration-candidates/exact",
            CANDIDATE_SHA,
        )
    ]


def test_finish_intent_rolls_back_every_state_when_event_write_interrupts(
    tmp_path, monkeypatch
):
    with kb.connect(tmp_path / "finish-rollback.db") as conn:
        key, prepared = _prepared_intent(tmp_path, monkeypatch, conn)
        snapshot_id = _insert_active_release_snapshot(conn, key.epic_id)
        real_append = kb._append_event

        def interrupt(conn, task_id, kind, *args, **kwargs):
            if kind == "story_integrated":
                raise RuntimeError("simulated crash before fact commit")
            return real_append(conn, task_id, kind, *args, **kwargs)

        monkeypatch.setattr(kb, "_append_event", interrupt)
        monkeypatch.setattr(
            integration_module,
            "delete_prepared_candidate_ref",
            lambda *_args, **_kwargs: pytest.fail(
                "candidate cleanup must happen only after a durable fact"
            ),
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            finish_intent(
                conn,
                prepared,
                PreparedRefCASResult("advanced", CANDIDATE_SHA),
            )
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM epic_story_integrations WHERE story_id=?",
            (key.story_id,),
        ).fetchone()[0]
        intent = conn.execute(
            "SELECT status, candidate_ref FROM story_integration_intents "
            "WHERE story_id=?",
            (key.story_id,),
        ).fetchone()
        story = conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id=?",
            (key.story_id,),
        ).fetchone()
        snapshot = conn.execute(
            "SELECT status FROM epic_release_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()

    assert fact_count == 0
    assert tuple(intent) == (
        "prepared",
        "refs/hermes/integration-candidates/exact",
    )
    assert tuple(story) == ("review", "integration_pending")
    assert snapshot["status"] == "awaiting_push"


@pytest.mark.parametrize(
    ("boundary", "current_sha", "expected"),
    [
        ("preimage", TARGET_SHA, RecoveryCounts(1, 1, 0, 0, 1)),
        ("candidate", CANDIDATE_SHA, RecoveryCounts(0, 1, 0, 0, 1)),
        ("descendant", "5" * 40, RecoveryCounts(0, 1, 0, 0, 1)),
        ("diverged", "6" * 40, RecoveryCounts(0, 0, 1, 0, 0)),
    ],
)
def test_recover_prepared_intent_handles_each_target_boundary(
    tmp_path, monkeypatch, boundary, current_sha, expected
):
    with kb.connect(tmp_path / f"recover-{boundary}.db") as conn:
        key, prepared = _prepared_intent(tmp_path, monkeypatch, conn)
        monkeypatch.setattr(
            integration_module,
            "inspect_prepared_candidate_ref",
            lambda *_args, **_kwargs: PreparedRefRecoveryResult(
                boundary, current_sha
            ),
        )
        advance_calls = []

        def advance(*_args, **_kwargs):
            advance_calls.append(True)
            return PreparedRefCASResult("advanced", CANDIDATE_SHA)

        monkeypatch.setattr(integration_module, "advance_prepared_intent", advance)
        monkeypatch.setattr(
            integration_module, "delete_prepared_candidate_ref", lambda *_a, **_k: True
        )

        result = recover_expired_intents(conn)
        row = conn.execute(
            "SELECT status, target_pre_sha, candidate_sha, candidate_ref, "
            "verification_event_id, last_failure_code "
            "FROM story_integration_intents WHERE story_id=?",
            (key.story_id,),
        ).fetchone()
        fact = conn.execute(
            "SELECT candidate_sha FROM epic_story_integrations WHERE story_id=?",
            (key.story_id,),
        ).fetchone()
        story = conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id=?",
            (key.story_id,),
        ).fetchone()
        pin = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1",
            (key.epic_id, kb.EPIC_BASE_PINNED_EVENT),
        ).fetchone()

    assert result == expected
    assert advance_calls == ([True] if boundary == "preimage" else [])
    if boundary == "diverged":
        assert tuple(row) == (
            "attention_required",
            TARGET_SHA,
            CANDIDATE_SHA,
            prepared.candidate_ref,
            prepared.verification_event_id,
            "target_moved",
        )
        assert fact is None
        assert tuple(story) == ("review", "integration_pending")
    else:
        assert row["status"] == "integrated"
        assert row["candidate_ref"] is None
        assert fact["candidate_sha"] == CANDIDATE_SHA
        assert tuple(story) == ("done", "done")
        expected_tip = current_sha if boundary == "descendant" else CANDIDATE_SHA
        assert json.loads(pin["payload"])["base_sha"] == expected_tip


def test_integrated_fact_recovery_and_epic_readiness_survive_verification_event_pruning(
    tmp_path, monkeypatch
):
    with kb.connect(tmp_path / "pruning.db") as conn:
        key, prepared = _prepared_intent(tmp_path, monkeypatch, conn)
        monkeypatch.setattr(
            integration_module, "delete_prepared_candidate_ref", lambda *_a, **_k: False
        )
        finish_intent(
            conn,
            prepared,
            PreparedRefCASResult("reflected", CANDIDATE_SHA),
        )
        conn.execute(
            "DELETE FROM task_events WHERE id=?", (prepared.verification_event_id,)
        )
        cleanup_observed_fact = []

        def cleanup(_root, **_kwargs):
            cleanup_observed_fact.append(
                conn.execute(
                    "SELECT candidate_sha FROM epic_story_integrations "
                    "WHERE story_id=?",
                    (key.story_id,),
                ).fetchone()["candidate_sha"]
            )
            return True

        monkeypatch.setattr(
            integration_module, "delete_prepared_candidate_ref", cleanup
        )
        recovered = recover_expired_intents(conn)
        # epic_ready on a repository-policy board derives readiness through
        # epic_readiness, which resolves the epic branch and commit ancestry
        # from a live git repository.  This fixture has none, so the durable
        # integration assertions above are exercised here while the readiness
        # gate under test is the handoff-v2 all-members-done path (still live
        # production behaviour for boards without repository policy).  The
        # repository-governed derivation's independence from the pruned
        # verification event is guarded separately by
        # test_kanban_epics.py::test_public_epic_readiness_uses_current_durable_fact_not_story_events.
        legacy_meta = dict(kb.product_board_metadata() or {})
        legacy_meta.pop("repository", None)
        monkeypatch.setattr(
            kb, "product_board_metadata", lambda _board=None: legacy_meta
        )
        ready = kb.epic_ready(conn, key.epic_id, verify_fn=lambda _branch: True)
        row = conn.execute(
            "SELECT status, candidate_ref FROM story_integration_intents "
            "WHERE story_id=?",
            (key.story_id,),
        ).fetchone()

    assert recovered == RecoveryCounts(0, 0, 0, 0, 1)
    assert cleanup_observed_fact == [CANDIDATE_SHA]
    assert tuple(row) == ("integrated", None)
    assert ready is True


@pytest.mark.parametrize("failure_code", ["merge_conflict", "verification_failed"])
def test_product_owned_integration_failure_uses_existing_development_rework_path(
    tmp_path, monkeypatch, failure_code
):
    metadata = _claim_board_metadata(tmp_path)
    metadata["product_workflow"]["max_rework_cycles"] = 3
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: metadata)

    with kb.connect(tmp_path / f"product-{failure_code}.db") as conn:
        key = _insert_claimable_intent(conn)
        claimed = claim_next_intent(
            conn,
            "owner",
            60,
            repository_check=lambda *_args: CandidateEligibility(SOURCE_SHA, True),
        )
        assert claimed is not None

        routed = integration_module.route_intent_failure(
            conn,
            claimed,
            kb.IntegrationCandidateError(
                "safe product failure",
                code=failure_code,
            ),
        )
        task = kb.get_task(conn, key.story_id)
        directive = kb.active_rework_directive(conn, key.story_id)
        intent = conn.execute(
            "SELECT status, claim_lock, claim_expires, last_failure_code "
            "FROM story_integration_intents WHERE story_id=?",
            (key.story_id,),
        ).fetchone()
        approval_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND step_key='review'",
            (key.story_id,),
        ).fetchone()[0]

    assert routed.status == "rework_required"
    assert tuple(intent) == ("rework_required", None, None, failure_code)
    assert task is not None
    assert task.current_step_key == "development"
    assert task.rework_count == 1
    assert task.current_step_key != "release_measure"
    assert directive is not None
    assert directive.origin_kind == "integration"
    assert directive.origin_intent_key == (
        f"{key.epic_id}:{key.story_id}:{key.source_sha}"
    )
    assert directive.target_phase == "development"
    assert directive.rejected_branch == "story/one"
    assert directive.rejected_sha == SOURCE_SHA
    assert directive.findings == (f"story integration {failure_code}",)
    assert approval_runs == 1


@pytest.mark.parametrize(
    "failure_code",
    [
        "command_missing",
        "ref_missing",
        "profile_missing",
        "timeout",
        "provisioning_failed",
        "io_error",
        "ownership_changed",
        "checked_out",
        "source_moved",
        "target_moved",
    ],
)
def test_infrastructure_integration_failure_keeps_same_lineage_without_rework(
    tmp_path, monkeypatch, failure_code
):
    metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: metadata)

    with kb.connect(tmp_path / f"attention-{failure_code}.db") as conn:
        key = _insert_claimable_intent(conn)
        claimed = claim_next_intent(
            conn,
            "owner-one",
            60,
            repository_check=lambda *_args: CandidateEligibility(SOURCE_SHA, True),
        )
        assert claimed is not None
        routed = integration_module.route_intent_failure(
            conn,
            claimed,
            kb.IntegrationCandidateError(
                "safe infrastructure failure",
                code=failure_code,
            ),
        )
        retried = claim_next_intent(
            conn,
            "owner-two",
            60,
            repository_check=lambda *_args: CandidateEligibility(SOURCE_SHA, True),
        )
        task = kb.get_task(conn, key.story_id)
        directive = kb.active_rework_directive(conn, key.story_id)
        intent = conn.execute(
            "SELECT status, attempt_count, last_failure_code "
            "FROM story_integration_intents WHERE story_id=?",
            (key.story_id,),
        ).fetchone()

    assert routed.status == "attention_required"
    assert retried is not None and retried.key == key
    assert retried.attempt_count == 2
    assert tuple(intent) == ("running", 2, failure_code)
    assert task is not None
    assert task.current_step_key == "integration_pending"
    assert task.rework_count == 0
    assert task.current_step_key != "release_measure"
    assert directive is None
