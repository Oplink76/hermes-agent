from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
import hermes_cli.kanban_db_connect as kanban_db_connect
from hermes_cli import kanban_intake as intake
from hermes_cli import kanban_qualifier as qualifier



@pytest.fixture
def conn(tmp_path):
    connection = kanban_db_connect.connect(tmp_path / "kanban.db")
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


def _intake(conn, *, attachments=(), request=None):
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            request or {
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


def _epic_intake(conn):
    return _intake(
        conn,
        request={
            "kind": "task_create",
            "request": {
                "title": "Reliable customer notifications",
                "item_kind": "epic",
                "stories": _epic_decision()["stories"],
            },
        },
    )


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
    assert "admission and routing classifier" in prompt.lower()
    assert "must not invent product stories" in prompt.lower()
    assert "external analysis is advisory" in prompt.lower()
    assert "earliest unfinished phase" in prompt.lower()
    assert "PO PATH ADDITION" in prompt
    assert '"po_evidence":{"run_id":123' in prompt
    assert '"sizing":{' in prompt
    assert '"requirement_feasibility":{' in prompt
    assert '"development_iteration_budget"' in prompt
    assert "Every binding required_evidence and done_when" in prompt


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
            intake=_epic_intake(conn),
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
            intake=_epic_intake(conn),
            decision=decision,
        )


def test_auxiliary_qualifier_cannot_invent_epic_stories(conn, policy):
    with pytest.raises(
        qualifier.QualificationValidationError,
        match="cannot invent Epic stories",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=_intake(conn),
            decision=_epic_decision(),
        )

    validated = qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=_epic_intake(conn),
        decision=_epic_decision(),
    )
    assert validated["stories"] == _epic_decision()["stories"]


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


def test_qualification_prompt_has_no_governed_agent_memory(conn, policy):
    prompt = qualifier.build_qualification_prompt(
        conn,
        board_metadata=policy,
        intake=_intake(conn),
    )
    assert "agent_memory_recall" not in prompt
    assert "Agent Memory" not in prompt


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


def test_po_path_requires_real_completed_product_owner_run_and_artifact(
    conn, policy, monkeypatch
):
    monkeypatch.setattr(kb, "resolve_profile_iteration_budget", lambda _profile: 500)
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
    budget = 500

    validated = qualifier.validate_decision(
        conn,
        board_metadata=policy,
        intake=intake,
        decision=_decision(
            qualification_path="po",
            po_evidence={"run_id": run_id, "artifact": "/tmp/brief.md"},
            sizing={
                "rationale": "The bounded maintenance card fits one budget.",
                "configured_iteration_budget": budget,
                "estimated_turns": min(20, budget),
                "fits_budget": True,
            },
            requirement_feasibility={
                "rationale": "The existing tests provide an achievable path.",
                "achievable_requirements": [
                    {"requirement": "tests", "basis": ["Existing test suite"]},
                    {"requirement": "green", "basis": ["Existing test suite"]},
                ],
                "deferred_findings": [],
            },
        ),
    )

    assert validated["po_evidence"]["run_id"] == run_id

    contradictory = json.loads(json.dumps(validated))
    contradictory["sizing"]["card_estimates"] = [501]
    with pytest.raises(
        qualifier.QualificationValidationError,
        match="must not include Epic card_estimates",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=intake,
            decision=contradictory,
        )

    advisory_only = _intake(
        conn,
        attachments=[
            {
                "kind": "handoff_document",
                "path": "/tmp/advisory-only.md",
            }
        ],
    )
    advisory_decision = json.loads(json.dumps(validated))
    advisory_decision["po_evidence"]["artifact"] = "/tmp/advisory-only.md"
    conn.execute(
        "UPDATE task_runs SET summary = ? WHERE id = ?",
        ("Reviewed /tmp/advisory-only.md", run_id),
    )
    with pytest.raises(
        qualifier.QualificationValidationError,
        match="advisory handoff documents cannot become",
    ):
        qualifier.validate_decision(
            conn,
            board_metadata=policy,
            intake=advisory_only,
            decision=advisory_decision,
        )


def test_po_epic_rejects_conflicting_summary_estimate(policy, monkeypatch):
    monkeypatch.setattr(kb, "resolve_profile_iteration_budget", lambda _profile: 500)
    decision = {
        "work": {"item_kind": "epic"},
        "stories": [{}, {}],
        "handover": {"required_evidence": [], "done_when": []},
        "sizing": {
            "rationale": "Both cards fit.",
            "configured_iteration_budget": 500,
            "estimated_turns": 499,
            "card_estimates": [20, 30],
            "fits_budget": True,
        },
        "requirement_feasibility": {
            "rationale": "No binding evidence in this focused check.",
            "achievable_requirements": [],
            "deferred_findings": [],
        },
    }
    errors = []

    qualifier._validate_po_budget_and_feasibility(
        decision,
        board_metadata=policy,
        errors=errors,
    )

    assert errors == ["Epic estimated_turns must equal the largest card estimate"]


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

    with kanban_db_connect.connect(board=board) as connection:
        receipt = qualifier.submit_request(
            connection,
            request={
                "functional_intent": {
                    "title": "Reliable customer notifications",
                    "outcome": "Customers receive and can inspect notifications",
                },
                "item_kind": "epic",
                "stories": _epic_decision()["stories"],
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


def test_auxiliary_po_qualification_persists_sizing_and_feasibility(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    developer = home / "profiles" / "developer"
    developer.mkdir(parents=True)
    (developer / "config.yaml").write_text(
        "agent:\n  max_turns: 500\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = "strict-po-auxiliary"
    kb.ensure_product_board_defaults(board)

    with kanban_db_connect.connect(board=board) as connection:
        receipt = qualifier.submit_request(
            connection,
            request={"title": "Qualified maintenance"},
            source="work-inbox:test",
            attachments=({"name": "brief.md", "path": "/tmp/brief.md"},),
        )
        run_task_id = kb.create_task(connection, title="PO discovery")
        cursor = connection.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status, started_at, ended_at, summary
            ) VALUES (?, 'productowner', 'backlog', 'done', 1, 2, ?)
            """,
            (run_task_id, "Product brief: /tmp/brief.md"),
        )
        run_id = int(cursor.lastrowid)
        metadata = kb.read_board_metadata(board)
        metadata["qualification"]["required"] = True
        kb.board_metadata_path(board).write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        decision = _decision(
            qualification_path="po",
            po_evidence={"run_id": run_id, "artifact": "/tmp/brief.md"},
            sizing={
                "rationale": "The maintenance card fits one Development budget.",
                "configured_iteration_budget": 500,
                "estimated_turns": 20,
                "fits_budget": True,
            },
            requirement_feasibility={
                "rationale": "The current test suite can verify both conditions.",
                "achievable_requirements": [
                    {"requirement": "tests", "basis": ["Existing tests"]},
                    {"requirement": "green", "basis": ["Existing tests"]},
                ],
                "deferred_findings": [],
            },
        )

        result = qualifier.qualify_intake(
            connection,
            board=board,
            intake_id=receipt["intake_id"],
            model_call=lambda _prompt: decision,
            secret=b"test-only-secret",
            issued_at=100,
        )
        assert result["status"] == "qualified", result
        task = kb.get_task(connection, result["task_id"])
        contract = kb.get_work_contract(connection, task.work_contract_id)["contract"]

    assert result["status"] == "qualified"
    assert contract["sizing"]["estimated_turns"] == 20
    assert contract["requirement_feasibility"]["achievable_requirements"] == [
        {"requirement": "tests", "basis": ["Existing tests"]},
        {"requirement": "green", "basis": ["Existing tests"]},
    ]


def test_materialization_verification_failure_marks_intake_attention_required(
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

    with kanban_db_connect.connect(board=board) as connection:
        receipt = qualifier.submit_request(
            connection,
            request={"title": "Contract verification failure"},
            source="work-inbox:test",
        )
        monkeypatch.setattr(
            intake,
            "verify_work_contract",
            lambda *_args, **_kwargs: intake.WorkContractVerification(
                valid=False, failure="digest_mismatch"
            ),
        )

        result = qualifier.qualify_intake(
            connection,
            board=board,
            intake_id=receipt["intake_id"],
            model_call=lambda _prompt: _decision(),
            secret=b"test-only-secret",
            issued_at=100,
        )
        stored = kb.get_qualification_intake(connection, receipt["intake_id"])
        events = kb.list_qualification_intake_events(
            connection, receipt["intake_id"]
        )

    assert result == {
        "status": "attention_required",
        "intake_id": receipt["intake_id"],
        "failure_path": "digest_mismatch",
    }
    assert stored["status"] == "attention_required"
    assert events[-1]["kind"] == "work_contract_verification_failed"
    assert events[-1]["payload"] == {"failure_path": "digest_mismatch"}


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

    with kanban_db_connect.connect(board=board) as connection:
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
