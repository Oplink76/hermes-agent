from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_qualifier as qualifier


@pytest.fixture
def conn(tmp_path):
    connection = kb.connect(tmp_path / "kanban.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def policy():
    return {
        "preset": "product",
        "qualification": {
            "required": True,
            "contract_version": 1,
            "policy_version": "product-handoff-v2+qualification-v1",
            "paths": ["po", "hermes"],
            "work_types": ["story", "bug", "maintenance", "ops", "spike"],
            "phase_assignees": {
                "backlog": "productowner",
                "architecture": "architect",
                "development": "developer",
                "test": "tester",
                "review": "reviewer",
                "release_measure": None,
            },
        },
    }


def _intake(conn, *, attachments=()):
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            {
                "kind": "task_create",
                "request": {
                    "title": "Ship qualified work",
                    "body": "Make the governed flow visible",
                },
            }
        ),
        source="codex",
        attachments=attachments,
    )
    return kb.get_qualification_intake(conn, intake_id)


def _decision(**overrides):
    decision = {
        "qualification_path": "hermes",
        "work": {
            "item_kind": "card",
            "work_type": "maintenance",
            "title": "Ship qualified work",
            "outcome": "Only governed work reaches execution",
            "scope": ["Hermes"],
            "out_of_scope": ["Cockpit"],
        },
        "routing": {
            "entry_phase": "backlog",
            "assignee": "productowner",
            "epic_id": None,
            "dependencies": [],
        },
        "entry_assessment": {
            "reason": "New work starts at backlog",
            "skipped_phases": [],
            "evidence": [],
        },
        "handover": {
            "deliverables": ["implementation"],
            "required_evidence": ["tests"],
            "done_when": ["green"],
            "next_phase": "architecture",
            "next_role": "architect",
        },
        "rules": {
            "allowed": ["edit the scoped repository"],
            "forbidden": ["bypass review"],
        },
        "classification": ["framework:maintenance", "path:hermes"],
    }
    for key, value in overrides.items():
        decision[key] = value
    return decision


def _late_assessment(*phases, provenance=None):
    value = {
        "reason": "Earlier phases are already satisfied",
        "skipped_phases": [
            {
                "phase": phase,
                "reason": f"{phase} evidence exists",
                "evidence": [f"{phase}-artifact"],
            }
            for phase in phases
        ],
        "evidence": [f"{phase}-artifact" for phase in phases],
    }
    if provenance is not None:
        value["provenance"] = provenance
    return value


def _evidence_attachments(*references):
    return tuple({"name": reference} for reference in references)


def test_qualification_prompt_includes_exact_card_and_epic_output_shapes(conn, policy):
    prompt = qualifier.build_qualification_prompt(
        conn,
        board_metadata=policy,
        intake=_intake(conn),
    )

    assert "CARD OUTPUT SHAPE" in prompt
    assert '"item_kind":"card"' in prompt
    assert '"outcome":"Required measurable outcome"' in prompt
    assert '"dependencies":[]' in prompt
    assert '"classification":[' in prompt
    assert "EPIC OUTPUT SHAPE" in prompt
    assert '"item_kind":"epic"' in prompt
    assert "PO PATH ADDITION" in prompt
    assert '"po_evidence":{"run_id":123' in prompt


def _requalification_intake(conn, target_task_id: str):
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            {
                "kind": "task_requalification",
                "target_task_id": target_task_id,
                "reason": "resume governed work",
                "evidence": {"task": {"status": "scheduled"}},
            }
        ),
        source="hermes-reconcile",
    )
    return kb.get_qualification_intake(conn, intake_id)


def test_requalification_prompt_preserves_identity_and_normal_handover(conn, policy):
    task_id = kb.create_task(conn, title="Existing qualified card")
    prompt = qualifier.build_qualification_prompt(
        conn,
        board_metadata=policy,
        intake=_requalification_intake(conn, task_id),
    )

    assert f"Requalify the existing card {task_id}" in prompt
    assert "preserve its identity" in prompt
    assert "normal handover" in prompt
    assert "dependencies, not scheduled" in prompt
    assert "already-delivered work to the latest justified phase" in prompt
    assert "earliest unfinished phase" in prompt


def test_requalification_prompt_matches_late_entry_validator_contract(conn, policy):
    task_id = kb.create_task(conn, title="Existing qualified card")
    prompt = qualifier.build_qualification_prompt(
        conn,
        board_metadata=policy,
        intake=_requalification_intake(conn, task_id),
    )

    assert (
        '"entry_assessment":{"reason":"<why this phase is the correct entry>",'
        '"skipped_phases":[{"phase":"<skipped phase>"' in prompt
    )
    assert "copy each evidence reference exactly" in prompt.lower()
    assert "raw_intake or submitted_evidence only" in prompt
    assert "current_task_graph cannot be used as phase evidence" in prompt
    assert (
        '"entry_assessment":{"reason":"<why Review is the correct entry>",'
        '"skipped_phases":[{"phase":"<skipped phase>"' in prompt
    )
    assert (
        '"provenance":{"writer":{"profile":"<writer profile>",'
        '"artifact":"<exact writer artifact>"},"tester":'
        '{"profile":"<independent tester profile>",'
        '"artifact":"<exact test artifact>"}}' in prompt
    )


def test_requalification_decision_cannot_change_work_item_kind(conn, policy):
    task_id = kb.create_task(conn, title="Existing qualified card")
    decision = _decision()
    decision["work"]["item_kind"] = "epic"
    decision["routing"] = {
        "entry_phase": None,
        "assignee": None,
        "epic_id": None,
        "dependencies": [],
    }
    decision["handover"]["next_phase"] = None
    decision["handover"]["next_role"] = None

    with pytest.raises(
        qualifier.QualificationValidationError,
        match="preserve the existing work item kind",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_requalification_intake(conn, task_id),
            decision=decision,
        )


def test_requalification_decision_cannot_depend_on_its_target(conn, policy):
    task_id = kb.create_task(conn, title="Existing qualified card")
    decision = _decision()
    decision["routing"]["dependencies"] = [task_id]

    with pytest.raises(
        qualifier.QualificationValidationError,
        match="cannot depend on itself",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_requalification_intake(conn, task_id),
            decision=decision,
        )


def test_hermes_path_validates_without_product_owner_evidence(conn, policy):
    validated = qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=_intake(conn),
        decision=_decision(),
    )

    assert validated["qualification_path"] == "hermes"
    assert "po_evidence" not in validated


def test_po_path_requires_real_completed_product_owner_run_and_artifact(conn, policy):
    intake = _intake(conn, attachments=[{"name": "brief.md", "path": "/tmp/brief.md"}])
    with pytest.raises(qualifier.QualificationValidationError, match="Product Owner run"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=intake,
            decision=_decision(
                qualification_path="po",
                po_evidence={"run_id": 999, "artifact": "/tmp/brief.md"},
            ),
        )

    task_id = kb.create_task(conn, title="PO discovery")
    cursor = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status, started_at, ended_at, summary
        ) VALUES (?, 'productowner', 'backlog', 'done', 1, 2, ?)
        """,
        (task_id, "Product brief: /tmp/brief.md"),
    )
    run_id = int(cursor.lastrowid)

    validated = qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=intake,
        decision=_decision(
            qualification_path="po",
            po_evidence={"run_id": run_id, "artifact": "/tmp/brief.md"},
        ),
    )

    assert validated["po_evidence"]["run_id"] == run_id


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"work_type": "project"}, "work type"),
        ({"entry_phase": "qa"}, "entry phase"),
        ({"entry_phase": "development", "assignee": "default"}, "assignee"),
    ],
)
def test_finite_work_type_phase_and_role_are_enforced(conn, policy, mutation, message):
    decision = _decision()
    if "work_type" in mutation:
        decision["work"]["work_type"] = mutation["work_type"]
    else:
        decision["routing"].update(mutation)

    with pytest.raises(qualifier.QualificationValidationError, match=message):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_dependencies_and_epic_membership_are_separate_and_explicit(conn, policy):
    dependency = kb.create_task(conn, title="Dependency")
    epic = kb.create_task(conn, title="Outcome Epic", work_item_kind="epic")
    decision = _decision()
    decision["routing"].update(
        {"dependencies": [dependency], "epic_id": epic}
    )

    validated = qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=_intake(conn),
        decision=decision,
    )
    assert validated["routing"]["dependencies"] == [dependency]
    assert validated["routing"]["epic_id"] == epic

    decision["routing"]["dependencies"] = [epic]
    with pytest.raises(qualifier.QualificationValidationError, match="dependency.*card"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_late_entry_requires_reasons_and_evidence_for_every_skipped_phase(conn, policy):
    decision = _decision()
    decision["routing"].update(
        {"entry_phase": "development", "assignee": "developer"}
    )

    with pytest.raises(qualifier.QualificationValidationError, match="skipped phases"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )

    decision["entry_assessment"] = _late_assessment("backlog", "architecture")
    decision["handover"].update(next_phase="test", next_role="tester")
    assert qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=_intake(
            conn,
            attachments=_evidence_attachments(
                "backlog-artifact", "architecture-artifact"
            ),
        ),
        decision=decision,
    )["routing"]["entry_phase"] == "development"


def test_late_entry_rejects_non_object_and_unsubmitted_evidence(conn, policy):
    decision = _decision()
    decision["routing"].update(
        {"entry_phase": "architecture", "assignee": "architect"}
    )
    decision["handover"].update(next_phase="development", next_role="developer")
    decision["entry_assessment"] = _late_assessment("backlog")

    with pytest.raises(qualifier.QualificationValidationError, match="not grounded"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )

    decision["entry_assessment"]["skipped_phases"].append("not-an-object")
    with pytest.raises(qualifier.QualificationValidationError, match="objects"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_late_entry_accepts_exact_non_ascii_attachment_evidence(conn, policy):
    decision = _decision()
    decision["routing"].update(
        {"entry_phase": "architecture", "assignee": "architect"}
    )
    decision["handover"].update(next_phase="development", next_role="developer")
    decision["entry_assessment"] = {
        "reason": "Backlog is already satisfied",
        "skipped_phases": [
            {
                "phase": "backlog",
                "reason": "The approved brief is attached",
                "evidence": ["review-grøn"],
            }
        ],
        "evidence": ["review-grøn"],
    }

    validated = qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=_intake(conn, attachments=({"name": "review-grøn"},)),
        decision=decision,
    )

    assert validated["routing"]["entry_phase"] == "architecture"


def test_late_entry_cannot_reuse_evidence_from_an_unrelated_card(conn, policy):
    unrelated = kb.create_task(conn, title="Unrelated completed work")
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, 'tester', 'backlog-artifact architecture-artifact', 1)",
        (unrelated,),
    )
    decision = _decision()
    decision["routing"].update(
        {"entry_phase": "development", "assignee": "developer"}
    )
    decision["handover"].update(next_phase="test", next_role="tester")
    decision["entry_assessment"] = _late_assessment("backlog", "architecture")

    with pytest.raises(qualifier.QualificationValidationError, match="not grounded"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_review_entry_requires_independent_writer_and_test_provenance(conn, policy):
    decision = _decision()
    decision["routing"].update({"entry_phase": "review", "assignee": "reviewer"})
    decision["entry_assessment"] = _late_assessment(
        "backlog",
        "architecture",
        "development",
        "test",
        provenance={
            "writer": {"profile": "developer", "artifact": "commit:abc"},
            "tester": {"profile": "developer", "artifact": "tests:green"},
        },
    )
    decision["handover"].update(next_phase="release_measure", next_role=None)
    evidence = _evidence_attachments(
        "backlog-artifact",
        "architecture-artifact",
        "development-artifact",
        "test-artifact",
        "commit:abc",
        "tests:green",
    )

    with pytest.raises(qualifier.QualificationValidationError, match="independent"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn, attachments=evidence),
            decision=decision,
        )

    decision["entry_assessment"]["provenance"]["tester"]["profile"] = "tester"
    assert qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=_intake(conn, attachments=evidence),
        decision=decision,
    )["entry_assessment"]["provenance"]["tester"]["profile"] == "tester"


def test_release_measure_cannot_be_assigned_to_an_ordinary_worker(conn, policy):
    decision = _decision()
    decision["routing"].update(
        {"entry_phase": "release_measure", "assignee": "developer"}
    )
    decision["entry_assessment"] = _late_assessment(
        "backlog", "architecture", "development", "test", "review"
    )

    with pytest.raises(qualifier.QualificationValidationError, match="release_measure.*unassigned"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_handover_requires_legal_next_phase_and_role(conn, policy):
    decision = _decision()
    del decision["handover"]["next_role"]

    with pytest.raises(qualifier.QualificationValidationError, match="next_role"):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_standalone_work_is_valid_without_a_synthetic_epic(conn, policy):
    validated = qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=_intake(conn),
        decision=_decision(),
    )

    assert validated["routing"]["epic_id"] is None


def test_invalid_model_decision_retries_once_then_stores_rejection_without_card(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = "strict"
    kb.ensure_product_board_defaults(board)
    metadata = kb.read_board_metadata(board)
    metadata["qualification"]["required"] = True
    metadata["qualification"]["work_types"] = [
        "story", "bug", "maintenance", "ops", "spike"
    ]
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")

    with kb.connect(board=board) as connection:
        receipt = qualifier.submit_request(
            connection,
            request={"title": "Ambiguous request"},
            source="codex",
        )
        calls = []

        def invalid_model(prompt):
            calls.append(prompt)
            value = _decision()
            del value["handover"]["next_role"]
            return value

        result = qualifier.qualify_intake(
            connection,
            board=board,
            intake_id=receipt["intake_id"],
            model_call=invalid_model,
            secret=b"test-only-secret",
            issued_at=100,
        )

        assert result["status"] == "rejected"
        assert len(calls) == 2
        assert "next_role" in calls[1]
        assert kb.get_qualification_intake(connection, receipt["intake_id"])["status"] == "rejected"
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
