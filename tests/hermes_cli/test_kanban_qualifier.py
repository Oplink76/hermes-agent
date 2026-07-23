from __future__ import annotations

import json
from datetime import datetime

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_qualifier as qualifier
from hermes_cli.agent_memory_vault import SessionGist, append_gist


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


def _epic_decision():
    decision = _decision()
    decision["work"] = {
        "item_kind": "epic",
        "work_type": "story",
        "title": "Reliable customer notifications",
        "outcome": "Customers receive and can inspect delivery notifications",
        "scope": ["Notification delivery", "Delivery history"],
        "out_of_scope": ["Marketing campaigns"],
    }
    decision["routing"] = {
        "entry_phase": None,
        "assignee": None,
        "epic_id": None,
        "dependencies": [],
    }
    decision["handover"] = {
        "deliverables": ["The complete notification outcome"],
        "required_evidence": ["Evidence from every member story"],
        "done_when": ["All member stories are released"],
        "next_phase": None,
        "next_role": None,
    }
    decision["classification"] = [
        "framework:epic",
        "path:hermes",
        "intake:plan",
    ]
    decision["stories"] = [
        {
            "title": "Send a delivery notification",
            "outcome": "A customer receives a notification after delivery",
            "scope": ["Notification sending"],
            "out_of_scope": ["Notification history"],
            "done_when": ["A delivered order produces one customer notification"],
            "depends_on": [],
        },
        {
            "title": "Inspect notification history",
            "outcome": "A customer can inspect previously sent notifications",
            "scope": ["Notification history"],
            "out_of_scope": ["Notification sending"],
            "done_when": ["The customer sees sent notifications in time order"],
            "depends_on": [0],
        },
    ]
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
    assert '"stories":[' in prompt
    assert '"depends_on":[]' in prompt
    assert "determine whether the intake is an idea, plan, epic, or bug" in prompt.lower()
    assert "external analysis is advisory" in prompt.lower()
    assert "earliest unfinished phase" in prompt.lower()
    assert "PO PATH ADDITION" in prompt
    assert '"po_evidence":{"run_id":123' in prompt


def test_new_epic_requires_valid_story_decomposition(conn, policy):
    decision = _epic_decision()
    decision["stories"] = []

    with pytest.raises(
        qualifier.QualificationValidationError,
        match="Epic qualification requires at least one story",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )

    decision = _epic_decision()
    decision["stories"][0]["depends_on"] = [0]
    with pytest.raises(
        qualifier.QualificationValidationError,
        match="earlier story",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_standalone_card_cannot_smuggle_story_decomposition(conn, policy):
    decision = _decision()
    decision["stories"] = _epic_decision()["stories"]

    with pytest.raises(
        qualifier.QualificationValidationError,
        match="Only an Epic can contain stories",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=decision,
        )


def test_qualification_prompt_includes_bounded_advisory_agent_memory(
    conn, policy, tmp_path, monkeypatch
):
    vault = tmp_path / "Agent Memory"
    monkeypatch.setenv("HERMES_AGENT_MEMORY_VAULT", str(vault))
    append_gist(
        vault,
        SessionGist(
            gist_id="qualification-history",
            occurred_at=datetime(2026, 7, 18, 12, 0),
            agent_id="developer",
            role="development",
            function_id="function-governed-flow",
            title="Ship qualified governed flow",
            context="board=product; card=prior",
            summary="The governed flow was previously implemented.",
            reused="none",
            result="Implementation exists.",
            maturity="code_complete",
            evidence="commit abc123; tests green",
            behavior="none",
            decisions="none",
            open_loops="Review remains.",
        ),
    )

    prompt = qualifier.build_qualification_prompt(
        conn,
        board_metadata=policy,
        intake=_intake(conn),
    )
    payload = json.loads(prompt.split("AUTHORITATIVE INPUT:\n", 1)[1])

    assert payload["agent_memory_recall"] == [
        {
            "function_id": "function-governed-flow",
            "title": "Ship qualified governed flow",
            "gist_id": "qualification-history",
            "evidence": "commit abc123; tests green",
            "snippet": (
                "Ship qualified governed flow: The governed flow was previously "
                "implemented."
            ),
        }
    ]
    assert "historical evidence only" in prompt.lower()
    assert "decide reuse or extension" in prompt.lower()
    assert "similarity alone cannot reject or merge" in prompt.lower()


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
    assert "advisory_handoffs cannot be used" in prompt
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


def test_late_entry_rejects_handoff_document_as_phase_evidence(conn, policy):
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
            intake=_intake(
                conn,
                attachments=(
                    {
                        "kind": "handoff_document",
                        "content": "backlog-artifact",
                    },
                ),
            ),
            decision=decision,
        )


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


def test_epic_qualification_materializes_signed_member_stories_and_dependencies(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = "strict"
    kb.ensure_product_board_defaults(board)
    metadata = kb.read_board_metadata(board)
    metadata["qualification"]["required"] = True
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")

    with kb.connect(board=board) as connection:
        receipt = qualifier.submit_request(
            connection,
            request={
                "functional_intent": {
                    "title": "Reliable customer notifications",
                    "outcome": "Customers receive and can inspect notifications",
                }
            },
            source="work-inbox:test",
        )
        result = qualifier.qualify_intake(
            connection,
            board=board,
            intake_id=receipt["intake_id"],
            model_call=lambda _prompt: _epic_decision(),
            secret=b"test-only-secret",
            issued_at=100,
        )

        assert result["status"] == "qualified"
        assert len(result["story_task_ids"]) == 2
        epic = kb.get_task(connection, result["task_id"])
        assert epic.work_item_kind == "epic"
        assert kb.list_epic_members(connection, epic.id) == result["story_task_ids"]

        members = {
            task.title: task
            for task_id in result["story_task_ids"]
            if (task := kb.get_task(connection, task_id)) is not None
        }
        first = members["Send a delivery notification"]
        second = members["Inspect notification history"]
        assert first.work_item_kind == second.work_item_kind == "card"
        assert first.current_step_key == second.current_step_key == "backlog"
        assert first.assignee == second.assignee == "productowner"
        assert kb.parent_ids(connection, first.id) == []
        assert kb.parent_ids(connection, second.id) == [first.id]

        rows = connection.execute(
            """
            SELECT wc.request_id, t.work_contract_id
              FROM tasks t
              JOIN work_contracts wc ON wc.id = t.work_contract_id
             WHERE t.id IN (?, ?, ?)
            """,
            (epic.id, first.id, second.id),
        ).fetchall()
        assert len(rows) == 3
        assert {row["request_id"] for row in rows} == {receipt["intake_id"]}
        assert all(row["work_contract_id"] for row in rows)


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
