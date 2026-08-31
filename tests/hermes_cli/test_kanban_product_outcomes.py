"""Canonical product-workflow outcome fixtures and validation regressions."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_product_outcomes as outcomes
from hermes_cli.kanban_product_outcomes import (
    ApprovedCandidate,
    CandidateEligibilityError,
    OutcomeValidationError,
    PassedTest,
    ProductOutcomeError,
    TerminalOutcome,
    TerminalRunRecord,
    candidate_eligibility,
    latest_review_authority,
    latest_test_authority,
    validate_terminal_outcome,
)


_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "kanban" / "product_outcomes"


def _production_envelope(run_id: int) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / f"run_{run_id}.json").read_text(encoding="utf-8"))


def _v2_product_board(name: str) -> None:
    kb.create_board(name, name="V2 Board", preset="product")
    meta_path = kb.board_metadata_path(name)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["handoff_v2"] = True
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _seed_product_card(board: str, *, step: str, assignee: str) -> tuple[str, int]:
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: canonical outcome",
            assignee=assignee,
            workflow_template_id="product",
            current_step_key=step,
            board=board,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        return task_id, claimed.current_run_id


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Immutable production envelopes
# ---------------------------------------------------------------------------


def test_production_run_407_has_marker_without_canonical_outcome():
    row = _production_envelope(407)
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert "workflow_outcome" not in row["metadata"]


def test_production_run_304_is_an_independent_missing_canonical_occurrence():
    row = _production_envelope(304)
    assert row["task_id"] != _production_envelope(407)["task_id"]
    assert row["epic_id"] != _production_envelope(407)["epic_id"]
    assert row["outcome"] == "advanced"
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert "workflow_outcome" not in row["metadata"]


@pytest.mark.parametrize("run_id", [354, 369])
def test_production_preflight_repairs_are_non_verdict_terminal_runs(run_id):
    row = _production_envelope(run_id)
    assert row["step_key"] == "test"
    assert row["outcome"] == "preflight_repaired"
    assert "workflow_outcome" not in row["metadata"]


def test_production_run_410_has_marker_and_canonical_outcome():
    row = _production_envelope(410)
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert row["metadata"]["workflow_outcome"] == {"verdict": "approved"}


# ---------------------------------------------------------------------------
# Pure outcome kernel
# ---------------------------------------------------------------------------


def test_run_407_is_missing_not_changes_requested():
    row = _production_envelope(407)
    with pytest.raises(OutcomeValidationError) as raised:
        validate_terminal_outcome(
            task_id=row["task_id"],
            run_id=row["id"],
            phase="review",
            summary=row["summary"],
            result=None,
            metadata=row["metadata"],
        )
    assert raised.value.code == "missing"
    assert raised.value.qualifier == "serialized_parameter"


def test_run_410_approves_and_records_leak():
    row = _production_envelope(410)
    outcome = validate_terminal_outcome(
        task_id=row["task_id"],
        run_id=row["id"],
        phase="review",
        summary=row["summary"],
        result=None,
        metadata=row["metadata"],
    )
    assert outcome.verdict == "approved"
    assert outcome.target_step is None
    assert outcome.findings == ()
    assert outcome.observations == ("serialized_parameter_leak",)


@pytest.mark.parametrize(
    ("phase", "canonical", "code"),
    [
        ("review", [], "invalid_shape"),
        ("review", {"verdict": "approved", "extra": True}, "invalid_shape"),
        ("test", {"verdict": "approved"}, "phase_mismatch"),
        (
            "review",
            {
                "verdict": "changes_requested",
                "target_step": "development",
                "findings": [],
            },
            "invalid_findings",
        ),
        (
            "review",
            {"verdict": "changes_requested", "target_step": "development", "findings": ["fix"]},
            "contradictory",
        ),
    ],
)
def test_kernel_rejects_unsafe_outcome_shapes(phase, canonical, code):
    metadata = {"workflow_outcome": canonical}
    if code == "contradictory":
        metadata["ai_provenance"] = {
            "reviewer": {"verdict": "approved"},
            "tester": {"verdict": "approved"},
        }
    with pytest.raises(OutcomeValidationError) as raised:
        validate_terminal_outcome(
            task_id="task",
            run_id=1,
            phase=phase,
            summary="ordinary summary",
            result=None,
            metadata=metadata,
        )
    assert raised.value.code == code


@pytest.mark.parametrize("phase", ["test", "review"])
@pytest.mark.parametrize(
    "claimed",
    ["preflight_repaired", "preflight_resolved", "preflight_escalated"],
)
def test_privileged_strings_are_not_ordinary_outcome_authority(phase, claimed):
    with pytest.raises(OutcomeValidationError) as raised:
        validate_terminal_outcome(
            task_id="task",
            run_id=1,
            phase=phase,
            summary="ordinary summary",
            result=None,
            metadata={
                "outcome": claimed,
                "run_outcome": claimed,
                "completion_outcome": claimed,
            },
        )
    assert raised.value.code == "missing"
    assert raised.value.qualifier is None


# ---------------------------------------------------------------------------
# Ordinary completion boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", [304, 407])
def test_missing_canonical_outcome_rejects_before_product_mutation(
    kanban_home, run_id
):
    board = f"missing-outcome-{run_id}"
    _v2_product_board(board)
    row = _production_envelope(run_id)
    task_id, expected_run_id = _seed_product_card(
        board, step="review", assignee="reviewer"
    )
    with kb.connect(board=board) as conn:
        before = conn.execute(
            "SELECT status, current_step_key, assignee, current_run_id, rework_count "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        with pytest.raises(ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                task_id,
                summary=row["summary"],
                metadata=copy.deepcopy(row["metadata"]),
                expected_run_id=expected_run_id,
                board=board,
            )
        after = conn.execute(
            "SELECT status, current_step_key, assignee, current_run_id, rework_count "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run = kb.get_run(conn, expected_run_id)
        events = kb.list_events(conn, task_id)

    assert raised.value.code == "missing"
    assert raised.value.qualifier == "serialized_parameter"
    assert tuple(after) == tuple(before)
    assert run is not None and run.ended_at is None
    rejection = [event for event in events if event.kind == "completion_rejected_outcome"]
    assert len(rejection) == 1
    assert rejection[0].run_id == expected_run_id
    assert rejection[0].payload == {
        "run_id": expected_run_id,
        "phase": "review",
        "code": "missing",
        "qualifier": "serialized_parameter",
    }
    assert not any(event.kind in {"handoff", "workflow_advanced", "rework_requested"} for event in events)


@pytest.mark.parametrize("phase,assignee", [("test", "tester"), ("review", "reviewer")])
@pytest.mark.parametrize(
    "claimed",
    ["preflight_repaired", "preflight_resolved", "preflight_escalated"],
)
def test_privileged_metadata_cannot_bypass_ordinary_completion(
    kanban_home, phase, assignee, claimed
):
    board = f"impersonation-{phase}-{claimed}"
    _v2_product_board(board)
    task_id, expected_run_id = _seed_product_card(board, step=phase, assignee=assignee)
    with kb.connect(board=board) as conn:
        with pytest.raises(ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                task_id,
                summary="ordinary completion",
                metadata={
                    "outcome": claimed,
                    "run_outcome": claimed,
                    "completion_outcome": claimed,
                },
                expected_run_id=expected_run_id,
                board=board,
            )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, expected_run_id)
    assert raised.value.code == "missing"
    assert task is not None and task.current_step_key == phase and task.status == "running"
    assert run is not None and run.ended_at is None


def test_run_410_advances_and_records_only_safe_leak_observation(kanban_home):
    board = "accepted-leak"
    _v2_product_board(board)
    row = _production_envelope(410)
    task_id, expected_run_id = _seed_product_card(
        board, step="review", assignee="reviewer"
    )
    with kb.connect(board=board) as conn:
        assert kb.complete_task(
            conn,
            task_id,
            summary=row["summary"],
            metadata=copy.deepcopy(row["metadata"]),
            expected_run_id=expected_run_id,
            board=board,
        )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, expected_run_id)
        events = kb.list_events(conn, task_id)
    assert task is not None and task.current_step_key == "release_measure"
    assert run is not None and run.ended_at is not None and run.outcome == "advanced"
    leak = [event for event in events if event.kind == "serialized_parameter_leak"]
    assert len(leak) == 1
    assert leak[0].run_id == expected_run_id
    assert leak[0].payload == {"run_id": expected_run_id, "phase": "review"}


def _resolver_expected_snapshot(conn, task_id: str, run_id: int) -> dict[str, object]:
    task = kb.get_task(conn, task_id)
    assert task is not None
    preflight = [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT
    ][-1]
    return {
        "run_id": run_id,
        "preflight_event_id": preflight.id,
        "status": task.status,
        "phase": task.current_step_key,
        "assignee": task.assignee,
        "project_id": task.project_id,
        "workflow_template_id": task.workflow_template_id,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "running": task.running,
        "blocked": task.blocked,
    }


@pytest.mark.parametrize("run_id", [354, 369])
def test_production_preflight_repairs_use_structural_resolver_path(
    kanban_home, run_id
):
    board = f"resolver-fixture-{run_id}"
    _v2_product_board(board)
    row = _production_envelope(run_id)
    with kb.connect(board=board) as conn:
        task_id, first_run_id = _seed_product_card(
            board, step="test", assignee="tester"
        )
        assert kb.block_task(
            conn,
            task_id,
            reason=row["metadata"]["reason"],
            kind="needs_input",
            attempted_resolutions=["replayed the production preflight shape"],
            expected_run_id=first_run_id,
            board=board,
            human_escalation_assignee="resolver",
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ? AND assignee = 'resolver'",
            (task_id,),
        )
        conn.commit()
        resolver = kb.claim_task(conn, task_id)
        assert resolver is not None and resolver.current_run_id is not None
        expected = _resolver_expected_snapshot(
            conn, task_id, resolver.current_run_id
        )
        request = {
            "decision": "repair",
            "fault_domain": row["metadata"]["fault_domain"],
            "diagnosis": row["metadata"]["diagnosis"],
            "reason": row["metadata"]["reason"],
            "expected": expected,
            "repair": {"workflow": {"phase": "development"}},
        }
        assert kb.resolve_product_preflight(
            conn,
            task_id,
            board=board,
            request=request,
            resolver_profile="resolver",
            resolver_model=None,
        )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, resolver.current_run_id)
        events = kb.list_events(conn, task_id)

    assert task is not None and task.current_step_key == "development"
    assert run is not None and run.outcome == "preflight_repaired"
    assert run.metadata is not None
    assert "workflow_outcome" not in run.metadata
    assert any(event.kind == "resolver_repair_applied" for event in events)
    assert events[-1].kind == "human_input_preflight_resolved"


def test_rejected_completion_then_clean_exit_is_a_protocol_violation(
    kanban_home, monkeypatch
):
    board = "rejected-completion-clean-exit"
    _v2_product_board(board)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("clean_exit", 0))

    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: rejected completion",
            assignee="reviewer",
            workflow_template_id="product",
            current_step_key="review",
            board=board,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = 0 WHERE id = ?",
            (91001, task_id),
        )
        conn.commit()

        with pytest.raises(ProductOutcomeError) as raised:
            kb.complete_task(
                conn,
                task_id,
                summary='<parameter name="workflow_outcome">{"verdict":"approved"}',
                metadata={"outcome": "preflight_repaired"},
                expected_run_id=claimed.current_run_id,
                board=board,
            )
        assert raised.value.code == "missing"
        assert kb.detect_crashed_workers(conn) == [task_id]
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)
        events = kb.list_events(conn, task_id)

    assert task is not None
    assert task.status == "blocked"
    assert task.current_step_key == "review"
    assert run is not None and run.outcome == "crashed"
    assert run.status == "crashed"
    rejection = [event for event in events if event.kind == "completion_rejected_outcome"]
    assert len(rejection) == 1
    assert rejection[0].run_id == claimed.current_run_id
    protocol = [event for event in events if event.kind == "protocol_violation"]
    assert len(protocol) == 1
    assert protocol[0].run_id == claimed.current_run_id


# ---------------------------------------------------------------------------
# Latest immutable Test/Review authority and candidate eligibility
# ---------------------------------------------------------------------------


_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _authority_outcome(verdict: str) -> TerminalOutcome:
    if verdict in {"passed", "approved"}:
        return TerminalOutcome(verdict=verdict, target_step=None, findings=(), observations=())
    target = "architecture" if verdict == "architecture_invalid" else "development"
    return TerminalOutcome(
        verdict=verdict,
        target_step=target,
        findings=("needs work",),
        observations=(),
    )


def _review_record(
    run_id: int,
    *,
    verdict: str = "approved",
    base_sha: str = _SHA_A,
    head_sha: str = _SHA_B,
    branch: str | None = "story/example",
    reviewer: str = "codex",
    writer: str = "claude-code",
) -> TerminalRunRecord:
    return TerminalRunRecord(
        run_id=run_id,
        phase="review",
        outcome=_authority_outcome(verdict),
        writer_provider=writer,
        reviewer_provider=reviewer,
        review_branch=branch,
        review_base_sha=base_sha,
        review_head_sha=head_sha,
    )


def _test_record(
    run_id: int,
    *,
    verdict: str = "passed",
    head_sha: str = _SHA_B,
    branch: str | None = "story/example",
    tester: str = "hermes",
    writer: str = "claude-code",
) -> TerminalRunRecord:
    return TerminalRunRecord(
        run_id=run_id,
        phase="test",
        outcome=_authority_outcome(verdict),
        writer_provider=writer,
        tester_provider=tester,
        test_branch=branch,
        test_head_sha=head_sha,
    )


def test_later_review_rejection_invalidates_older_approval():
    runs = [_review_record(1), _review_record(2, verdict="changes_requested")]
    assert latest_review_authority(runs) is None


def test_later_test_rejection_invalidates_older_pass():
    runs = [_test_record(1), _test_record(2, verdict="changes_requested")]
    assert latest_test_authority(runs, _SHA_B) is None


def test_authority_requires_exact_full_sha():
    runs = [_review_record(1, head_sha="a" * 12)]
    assert latest_review_authority(runs) is None


def test_review_authority_uses_dispatcher_pinned_branch_not_worker_alias():
    runs = [_review_record(1, branch=None)]
    assert latest_review_authority(runs) is None


def test_review_authority_rejects_same_provider_writer_and_reviewer():
    runs = [_review_record(1, reviewer="claude-code", writer="claude-code")]
    assert latest_review_authority(runs) is None


def test_test_authority_requires_exact_requested_source_sha():
    assert latest_test_authority([_test_record(1, head_sha=_SHA_C)], _SHA_B) is None


def test_test_authority_accepts_writer_and_tester_from_the_same_provider():
    assert latest_test_authority(
        [_test_record(1, tester="openrouter", writer="openrouter")],
        _SHA_B,
    ) == PassedTest(
        run_id=1,
        branch="story/example",
        source_sha=_SHA_B,
        tester_provider="openrouter",
        writer_provider="openrouter",
    )


def test_candidate_eligibility_rejects_test_branch_mismatch(tmp_path):
    approved = ApprovedCandidate(
        run_id=1,
        branch="story/reviewed",
        base_sha=_SHA_A,
        source_sha=_SHA_B,
        reviewer_provider="codex",
        writer_provider="claude-code",
    )
    passed = PassedTest(
        run_id=2,
        branch="story/other",
        source_sha=_SHA_B,
        tester_provider="hermes",
        writer_provider="claude-code",
    )
    with pytest.raises(CandidateEligibilityError) as raised:
        candidate_eligibility(tmp_path, approved, passed)
    assert raised.value.code == "stale_review"


def test_candidate_eligibility_rejects_empty_review_diff(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    approved = ApprovedCandidate(
        run_id=1,
        branch="story/example",
        base_sha=base_sha,
        source_sha=base_sha,
        reviewer_provider="codex",
        writer_provider="claude-code",
    )
    passed = PassedTest(
        run_id=2,
        branch="story/example",
        source_sha=base_sha,
        tester_provider="hermes",
        writer_provider="claude-code",
    )
    with pytest.raises(CandidateEligibilityError) as raised:
        candidate_eligibility(repo, approved, passed)
    assert raised.value.code == "empty_contribution"


def test_candidate_eligibility_reports_git_diff_exit_failures_as_io_error(
    tmp_path, monkeypatch
):
    approved = ApprovedCandidate(
        run_id=1,
        branch="story/example",
        base_sha=_SHA_A,
        source_sha=_SHA_B,
        reviewer_provider="codex",
        writer_provider="claude-code",
    )
    passed = PassedTest(
        run_id=2,
        branch="story/example",
        source_sha=_SHA_B,
        tester_provider="hermes",
        writer_provider="claude-code",
    )

    monkeypatch.setattr(
        outcomes.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 2})(),
    )

    with pytest.raises(CandidateEligibilityError) as raised:
        candidate_eligibility(tmp_path, approved, passed)
    assert raised.value.code == "io_error"


def test_candidate_eligibility_reports_git_diff_oserror_as_io_error(
    tmp_path, monkeypatch
):
    approved = ApprovedCandidate(
        run_id=1,
        branch="story/example",
        base_sha=_SHA_A,
        source_sha=_SHA_B,
        reviewer_provider="codex",
        writer_provider="claude-code",
    )
    passed = PassedTest(
        run_id=2,
        branch="story/example",
        source_sha=_SHA_B,
        tester_provider="hermes",
        writer_provider="claude-code",
    )

    def raise_os_error(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(outcomes.subprocess, "run", raise_os_error)

    with pytest.raises(CandidateEligibilityError) as raised:
        candidate_eligibility(tmp_path, approved, passed)
    assert raised.value.code == "io_error"
