from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


def _strict_board(tmp_path, monkeypatch, board="po-intake"):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults(board)
    path = kb.board_metadata_path(board)
    metadata = json.loads(path.read_text())
    metadata["qualification"]["required"] = True
    metadata.setdefault("product_workflow", {})["handoff_v2"] = True
    path.write_text(json.dumps(metadata))
    developer_profile = home / "profiles" / "developer"
    developer_profile.mkdir(parents=True)
    (developer_profile / "config.yaml").write_text(
        "agent:\n  max_turns: 500\n",
        encoding="utf-8",
    )
    return board


def _active_intake(
    conn, monkeypatch, *, now=100, session_id="session-1", title="Build export"
):
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            {"kind": "task_create", "request": {"title": title}}
        ),
        source="work-inbox",
        session_id=session_id,
        created_at=now,
    )
    run = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity={
            "provider": "claude-cli",
            "model": "claude-opus-5",
            "effort": "high",
            "surface": "work_inbox_intake",
        },
        now=now + 1,
    )
    monkeypatch.setenv("HERMES_WORK_INBOX_INTAKE", intake_id)
    monkeypatch.setenv("HERMES_WORK_INBOX_RUN_ID", str(run["id"]))
    monkeypatch.setenv("HERMES_WORK_INBOX_CLAIM_LOCK", run["claim_lock"])
    monkeypatch.setenv("HERMES_PROFILE", "productowner")
    return intake_id, run


def _proposal():
    return {
        "work": {
            "item_kind": "card",
            "work_type": "story",
            "title": "Build export",
            "outcome": "Users can export a report",
            "scope": ["CSV export"],
            "out_of_scope": ["PDF export"],
        },
        "routing": {"epic_id": None, "dependencies": []},
        "entry_assessment": {
            "reason": "placeholder",
            "skipped_phases": [],
            "evidence": [],
        },
        "handover": {
            "deliverables": ["Architecture decision"],
            "required_evidence": ["Architecture tests"],
            "done_when": ["Architecture is implementable"],
            "next_phase": "development",
            "next_role": "developer",
        },
        "rules": {
            "allowed": ["Implement CSV export"],
            "forbidden": ["Add PDF export"],
        },
        "sizing": {
            "rationale": "One bounded export outcome fits one Development turn budget.",
            "configured_iteration_budget": 500,
            "estimated_turns": 80,
            "fits_budget": True,
        },
        "requirement_feasibility": {
            "rationale": (
                "Each binding requirement is achievable with the current "
                "Architecture test surface."
            ),
            "achievable_requirements": [
                {
                    "requirement": "Architecture tests",
                    "basis": ["The repository already exposes Architecture tests."],
                },
                {
                    "requirement": "Architecture is implementable",
                    "basis": ["Architecture review can verify implementability."],
                },
            ],
            "deferred_findings": [],
        },
        "classification": ["framework:story", "path:po", "intake:feature"],
        "stories": [],
    }


def test_work_inbox_decision_tool_publishes_complete_proposal_shape():
    from tools.kanban_tools import WORK_INBOX_DECIDE_SCHEMA

    proposal = WORK_INBOX_DECIDE_SCHEMA["parameters"]["properties"]["proposal"]
    assert set(proposal["required"]) == {
        "work",
        "routing",
        "handover",
        "rules",
        "sizing",
        "requirement_feasibility",
        "classification",
        "stories",
    }
    assert set(proposal["properties"]["work"]["required"]) == {
        "item_kind",
        "work_type",
        "title",
        "outcome",
        "scope",
        "out_of_scope",
    }
    assert set(proposal["properties"]["routing"]["required"]) == {
        "entry_phase",
        "assignee",
        "epic_id",
        "dependencies",
    }
    assert set(proposal["properties"]["handover"]["required"]) == {
        "deliverables",
        "required_evidence",
        "done_when",
        "next_phase",
        "next_role",
    }
    assert set(proposal["properties"]["rules"]["required"]) == {
        "allowed",
        "forbidden",
    }
    assert set(
        proposal["properties"]["requirement_feasibility"]["required"]
    ) == {
        "rationale",
        "achievable_requirements",
        "deferred_findings",
    }


def test_development_budget_comes_from_the_profile_config(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "developer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "agent:\n  max_turns: 37\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert kb.resolve_profile_iteration_budget("developer") == 37


def test_new_work_launches_configured_product_owner_without_waiting(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    conn = kb.connect(tmp_path / "kanban.db")
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            {"kind": "task_create", "request": {"title": "Assess feature"}}
        ),
        source="work-inbox",
        session_id="session-1",
        created_at=100,
    )
    identity = {
        "profile": "productowner",
        "provider": "claude-cli",
        "model": "claude-opus-5",
        "effort": "high",
        "surface": "work_inbox_intake",
        "source": "work_inbox_intake",
        "version": 1,
    }
    monkeypatch.setattr(
        kb,
        "resolve_profile_runtime_identity",
        lambda profile, **kwargs: identity,
    )
    monkeypatch.setattr(
        kb,
        "read_board_metadata",
        lambda _board: {
            "qualification": {
                "phase_assignees": {"backlog": "productowner"}
            }
        },
    )
    spawned = []

    def spawn(run, *, board):
        spawned.append((run, board))
        return 4242

    try:
        result = kanban_po_intake.dispatch_product_owner_intake(
            conn,
            board="strict",
            intake_id=intake_id,
            spawn_fn=spawn,
            now=110,
        )
        persisted = kb.get_qualification_intake_run(conn, result["run_id"])
    finally:
        conn.close()

    assert result["status"] == "running"
    assert result["provider"] == "claude-cli"
    assert spawned[0][1] == "strict"
    assert persisted["worker_pid"] == 4242
    assert persisted["model"] == "claude-opus-5"
    assert persisted["effort"] == "high"


def test_spawn_is_detached_intake_scoped_and_disables_provider_fallback(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    captured = {}

    class _Popen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            self.pid = 5150

    monkeypatch.setattr(kanban_po_intake.subprocess, "Popen", _Popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["/opt/hermes"])
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(
        kb, "workspaces_root", lambda board=None: tmp_path / "workspaces"
    )
    monkeypatch.setattr(
        kb, "worker_logs_dir", lambda board=None: tmp_path / "logs"
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda profile: str(tmp_path / "profiles" / profile),
    )
    run = {
        "id": 7,
        "intake_id": "qi_one",
        "claim_lock": "claim-secret",
        "profile": "productowner",
        "provider": "claude-cli",
        "model": "claude-opus-5",
        "effort": "high",
    }

    pid = kanban_po_intake._spawn_product_owner_intake(run, board="strict")

    assert pid == 5150
    assert captured["start_new_session"] is True
    assert captured["stdin"] is kanban_po_intake.subprocess.DEVNULL
    assert captured["cmd"] == [
        "/opt/hermes",
        "-p",
        "productowner",
        "--cli",
        "--accept-hooks",
        "--toolsets",
        "kanban",
        "chat",
        "-q",
        kanban_po_intake.PRODUCT_OWNER_PROMPT,
    ]
    env = captured["env"]
    assert env["HERMES_WORK_INBOX_INTAKE"] == "qi_one"
    assert env["HERMES_WORK_INBOX_RUN_ID"] == "7"
    assert env["HERMES_WORK_INBOX_CLAIM_LOCK"] == "claim-secret"
    assert env["HERMES_DISABLE_PROVIDER_FALLBACK"] == "1"
    assert env["HERMES_INFERENCE_PROVIDER"] == "claude-cli"
    assert env["HERMES_INFERENCE_MODEL"] == "claude-opus-5"
    assert env["HERMES_INFERENCE_EFFORT"] == "high"
    assert "HERMES_KANBAN_TASK" not in env


def test_requalification_keeps_auxiliary_qualifier(monkeypatch):
    from hermes_cli import kanban_po_intake
    from hermes_cli import kanban_qualifier

    record = {
        "id": "qi_requal",
        "raw_request": json.dumps(
            {"kind": "task_requalification", "task_id": "t_one"}
        ),
    }
    called = []
    monkeypatch.setattr(
        kanban_qualifier,
        "qualify_intake",
        lambda conn, *, board, intake_id: called.append(intake_id)
        or {"status": "qualified"},
    )

    result = kanban_po_intake.route_pending_intake(
        SimpleNamespace(), board="strict", intake=record
    )

    assert result["status"] == "qualified"
    assert called == ["qi_requal"]


def test_budget_requalification_routes_to_product_owner(monkeypatch):
    from hermes_cli import kanban_po_intake

    record = {
        "id": "qi_budget",
        "raw_request": json.dumps(
            {
                "kind": "task_requalification",
                "target_task_id": "t_one",
                "qualification_route": "product_owner",
            }
        ),
    }
    called = []
    monkeypatch.setattr(
        kanban_po_intake,
        "dispatch_product_owner_intake",
        lambda conn, *, board, intake_id: called.append((board, intake_id))
        or {"status": "running"},
    )

    result = kanban_po_intake.route_pending_intake(
        SimpleNamespace(), board="strict", intake=record
    )

    assert result["status"] == "running"
    assert called == [("strict", "qi_budget")]


def test_accepted_po_decision_is_signed_and_materialized_at_architecture(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch)
    conn = kb.connect(board=board)
    intake_id, run = _active_intake(conn, monkeypatch)
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Clear product outcome",
            proposal=_proposal(),
        )
        task = kb.get_task(conn, result["task_id"])
        contract = kb.get_work_contract(conn, task.work_contract_id)["contract"]
    finally:
        conn.close()

    assert result["status"] == "qualified"
    assert task.current_step_key == "architecture"
    assert task.assignee == "architect"
    assert contract["qualification_path"] == "po"
    assert contract["issuer"] == {
        "surface": "work_inbox_intake",
        "profile": "productowner",
        "provider": "claude-cli",
        "model": "claude-opus-5",
        "effort": "high",
        "run_id": run["id"],
        "issued_at": contract["issuer"]["issued_at"],
    }
    check = kb.connect(board=board)
    try:
        assert kb.get_qualification_intake(check, intake_id)["status"] == "qualified"
        assert kb.get_qualification_intake_run(check, run["id"])["status"] == "completed"
    finally:
        check.close()


def test_po_sizing_rationale_is_durable_and_handoff_shape_is_advisory(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-sizing")
    conn = kb.connect(board=board)
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            {
                "kind": "task_create",
                "request": {"title": "Several outcomes"},
            }
        ),
        source="work-inbox",
        attachments=(
            {
                "kind": "handoff_document",
                "title": "qualification handoff",
                "content": (
                    "Closed decision: retain the existing API. "
                    "Suggested shape: one user-story card."
                ),
            },
        ),
        created_at=100,
    )
    run = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity={
            "provider": "claude-cli",
            "model": "claude-opus-5",
            "effort": "high",
            "surface": "work_inbox_intake",
        },
        now=101,
    )
    monkeypatch.setenv("HERMES_WORK_INBOX_INTAKE", intake_id)
    monkeypatch.setenv("HERMES_WORK_INBOX_RUN_ID", str(run["id"]))
    monkeypatch.setenv("HERMES_WORK_INBOX_CLAIM_LOCK", run["claim_lock"])
    monkeypatch.setenv("HERMES_PROFILE", "productowner")
    shown = kanban_po_intake.show_product_owner_intake(conn, board=board)
    guidance = shown["qualification_guidance"]
    assert guidance["development_iteration_budget"] == 500
    assert guidance["handoffs"].startswith("context only")
    assert shown["intake"]["attachments"][0]["content"].startswith("Closed decision:")
    proposal = _proposal()
    proposal["work"] = {
        "item_kind": "epic",
        "work_type": "story",
        "title": "Several outcomes",
        "outcome": "Each outcome is independently deliverable",
        "scope": ["API", "CLI", "documentation"],
        "out_of_scope": [],
    }
    proposal["routing"] = {
        "entry_phase": None,
        "assignee": None,
        "epic_id": None,
        "dependencies": [],
    }
    proposal["handover"] = {
        "deliverables": ["Three delivered outcomes"],
        "required_evidence": ["All three outcomes verified"],
        "done_when": ["The three outcomes are complete"],
        "next_phase": None,
        "next_role": None,
    }
    proposal["rules"] = {
        "allowed": ["Retain the existing API"],
        "forbidden": ["Reopen the closed API decision"],
    }
    proposal["stories"] = [
        {
            "title": "API outcome",
            "outcome": "API remains compatible",
            "scope": ["API"],
            "out_of_scope": [],
            "done_when": ["Compatibility is verified"],
            "depends_on": [],
        },
        {
            "title": "CLI outcome",
            "outcome": "CLI exposes the outcome",
            "scope": ["CLI"],
            "out_of_scope": [],
            "done_when": ["CLI behavior is verified"],
            "depends_on": [0],
        },
        {
            "title": "Documentation outcome",
            "outcome": "The outcome is documented",
            "scope": ["documentation"],
            "out_of_scope": [],
            "done_when": ["Documentation is checked"],
            "depends_on": [1],
        },
    ]
    proposal["sizing"] = {
        "rationale": "I independently split the three deliverables into three cards.",
        "configured_iteration_budget": 500,
        "estimated_turns": 80,
        "card_estimates": [80, 60, 40],
        "fits_budget": True,
    }
    proposal["requirement_feasibility"] = {
        "rationale": "Each Epic and story requirement has a current verification path.",
        "achievable_requirements": [
            {"requirement": requirement, "basis": ["Existing verification path"]}
            for requirement in (
                "All three outcomes verified",
                "The three outcomes are complete",
                "Compatibility is verified",
                "CLI behavior is verified",
                "Documentation is checked",
            )
        ],
        "deferred_findings": [],
    }
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Closed API decision retained; decomposition is independently sized.",
            proposal=proposal,
        )
        contract = kb.get_work_contract(
            conn, kb.get_task(conn, result["task_id"]).work_contract_id
        )["contract"]
        task_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE work_item_kind = 'card'"
        ).fetchone()[0]
        story_ids = kb.list_epic_members(conn, result["task_id"])
        story_contracts = [
            kb.get_work_contract(
                conn, kb.get_task(conn, story_id).work_contract_id
            )["contract"]
            for story_id in story_ids
        ]
        worker_contexts = [
            kb.build_worker_context(conn, story_id) for story_id in story_ids
        ]
    finally:
        conn.close()

    assert result["status"] == "qualified"
    assert task_count == 3
    estimates_by_title = {
        contract["work"]["title"]: contract["sizing"]["estimated_turns"]
        for contract in story_contracts
    }
    assert estimates_by_title == {
        "API outcome": 80,
        "CLI outcome": 60,
        "Documentation outcome": 40,
    }
    assert "independently split" in contract["sizing"]["rationale"]
    assert "Closed decision:" not in json.dumps(contract)
    assert "Closed decision:" not in json.dumps(story_contracts)
    assert all("Closed decision:" not in context for context in worker_contexts)


def test_po_requires_achievability_basis_for_every_binding_requirement(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-feasibility-gate")
    conn = kb.connect(board=board)
    _active_intake(conn, monkeypatch, title="Unproven requirement")
    proposal = _proposal()
    proposal["requirement_feasibility"]["achievable_requirements"] = []
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Requirement was copied from a Test finding.",
            proposal=proposal,
        )
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()

    assert result["status"] == "invalid"
    assert any("achievable requirement" in error for error in result["errors"])
    assert task_count == 0


def test_po_defers_unachievable_test_finding_instead_of_contractualizing(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-feasibility-deferral")
    conn = kb.connect(board=board)
    _active_intake(conn, monkeypatch, title="Attachment projection")
    impossible = "Verify authentic attachment bytes with Range support"
    proposal = _proposal()
    proposal["requirement_feasibility"]["deferred_findings"] = [
        {
            "finding": impossible,
            "reason": "The current asset endpoint does not implement Range.",
            "enabling_dependency": "T2 Paperclip write path and byte-serving support",
        }
    ]
    try:
        accepted = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Keep achievable evidence binding and defer the Test symptom.",
            proposal=proposal,
        )
        contract = kb.get_work_contract(
            conn, kb.get_task(conn, accepted["task_id"]).work_contract_id
        )["contract"]

        _active_intake(
            conn,
            monkeypatch,
            now=200,
            session_id="impossible-binding",
            title="Impossible binding",
        )
        invalid = _proposal()
        invalid["handover"]["required_evidence"].append(impossible)
        invalid["requirement_feasibility"]["deferred_findings"] = [
            {
                "finding": impossible,
                "reason": "The current asset endpoint does not implement Range.",
                "enabling_dependency": "T2 Paperclip write path and byte-serving support",
            }
        ]
        rejected = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Do not permit an impossible Test symptom to become binding.",
            proposal=invalid,
        )
    finally:
        conn.close()

    assert accepted["status"] == "qualified"
    assert impossible not in contract["handover"]["required_evidence"]
    assert contract["requirement_feasibility"]["deferred_findings"][0][
        "enabling_dependency"
    ].startswith("T2")
    assert rejected["status"] == "invalid"
    assert any("achievable requirement" in error for error in rejected["errors"])


def test_po_rejects_a_card_that_does_not_fit_configured_development_budget(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-budget-gate")
    conn = kb.connect(board=board)
    intake_id, _run = _active_intake(conn, monkeypatch, title="Too large")
    proposal = _proposal()
    proposal["sizing"] = {
        "rationale": "The card is larger than one configured Development budget.",
        "configured_iteration_budget": 500,
        "estimated_turns": 501,
        "fits_budget": False,
    }
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Needs decomposition",
            proposal=proposal,
        )
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        intake_status = kb.get_qualification_intake(conn, intake_id)["status"]
    finally:
        conn.close()

    assert result["status"] == "invalid"
    assert any("exceeds" in error for error in result["errors"])
    assert task_count == 0
    assert intake_status == "running"


def test_real_budget_exit_creates_product_owner_requalification_intake(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-budget-e2e")
    conn = kb.connect(board=board)
    _intake_id, _run = _active_intake(conn, monkeypatch, title="Budgeted card")
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Bounded outcome",
            proposal=_proposal(),
        )
        task_id = result["task_id"]
        assert kb.set_phase(conn, task_id, "development", board=board)
        assert kb.claim_task(conn, task_id, board=board) is not None
        routed = kb.handle_development_budget_exhaustion(conn, task_id, board=board)
        intakes = kb.list_qualification_intakes(conn)
        budget_intakes = [
            intake
            for intake in intakes
            if '"qualification_route":"product_owner"' in intake["raw_request"]
        ]
    finally:
        conn.close()

    assert routed is True
    assert len(budget_intakes) == 1
    assert json.loads(budget_intakes[0]["raw_request"])["target_task_id"] == task_id


def test_budget_exhaustion_routing_rolls_back_as_one_transaction(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_intake, kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-budget-atomic")
    conn = kb.connect(board=board)
    _active_intake(conn, monkeypatch, title="Atomic budget card")
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Bounded outcome",
            proposal=_proposal(),
        )
        task_id = result["task_id"]
        assert kb.set_phase(conn, task_id, "development", board=board)
        assert kb.claim_task(conn, task_id, board=board) is not None
        kb._set_worker_pid(conn, task_id, 981002)
        monkeypatch.setattr(
            kanban_intake,
            "submit_requalification",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("intake failed")),
        )
        with pytest.raises(RuntimeError, match="intake failed"):
            kb.handle_development_budget_exhaustion(conn, task_id, board=board)

        card = kb.get_task(conn, task_id)
        budget_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "development_budget_exhausted"
        ]
        intakes = kb.list_qualification_intakes(conn)
    finally:
        conn.close()

    assert card.status == "running"
    assert card.worker_pid == 981002
    assert budget_events == []
    assert len(intakes) == 1  # only the original admission intake


def test_clarification_stays_inert_and_two_invalid_decisions_need_attention(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-invalid")
    conn = kb.connect(board=board)
    intake_id, run = _active_intake(conn, monkeypatch)
    try:
        first = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="try",
            proposal={"work": {}},
        )
        second = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="try again",
            proposal={"work": {}},
        )
        assert first["status"] == "invalid"
        assert second["status"] == "attention_required"
        assert kb.get_qualification_intake(conn, intake_id)["status"] == "attention_required"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()

    conn = kb.connect(board=board)
    second_id = kb.create_qualification_intake(
        conn,
        raw_request='{"kind":"task_create","request":{"title":"Clarify"}}',
        source="work-inbox",
        session_id="session-2",
    )
    second_run = kb.claim_qualification_intake(
        conn,
        second_id,
        profile="productowner",
        runtime_identity={"provider": "claude-cli", "model": "opus", "effort": "high"},
    )
    monkeypatch.setenv("HERMES_WORK_INBOX_INTAKE", second_id)
    monkeypatch.setenv("HERMES_WORK_INBOX_RUN_ID", str(second_run["id"]))
    monkeypatch.setenv("HERMES_WORK_INBOX_CLAIM_LOCK", second_run["claim_lock"])
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="needs_clarification",
            reason="Customer is ambiguous",
            question="Which customer segment?",
        )
        assert result["status"] == "needs_clarification"
        assert kb.get_qualification_intake(conn, second_id)["status"] == "needs_clarification"
        clarification = next(
            event
            for event in kb.list_qualification_intake_events(conn, second_id)
            if event["kind"] == "clarification_requested"
        )
        assert clarification["payload"]["question"] == "Which customer segment?"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_accepted_epic_materializes_all_stories_ready_at_architecture(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_intake
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-epic")
    conn = kb.connect(board=board)
    _intake_id, _run = _active_intake(conn, monkeypatch)
    proposal = _proposal()
    proposal["work"]["item_kind"] = "epic"
    proposal["work"]["title"] = "Eksport — rapportering"
    proposal["routing"] = {
        "entry_phase": None,
        "assignee": None,
        "epic_id": None,
        "dependencies": [],
    }
    proposal.pop("entry_assessment")
    proposal["handover"]["next_phase"] = None
    proposal["handover"]["next_role"] = None
    proposal["stories"] = [
        {
            "title": "Eksportér data",
            "outcome": "CSV can be generated",
            "scope": ["CSV generation"],
            "out_of_scope": ["Download UI"],
            "done_when": ["CSV is valid"],
            "depends_on": [],
        },
        {
            "title": "Hent CSV — København",
            "outcome": "User can download CSV",
            "scope": ["Download UI"],
            "out_of_scope": ["PDF"],
            "done_when": ["Download succeeds"],
            "depends_on": [0],
        },
    ]
    proposal["sizing"]["card_estimates"] = [80, 80]
    proposal["requirement_feasibility"] = {
        "rationale": "The Epic and both story outcomes have current verification paths.",
        "achievable_requirements": [
            {"requirement": requirement, "basis": ["Existing verification path"]}
            for requirement in (
                "Architecture tests",
                "Architecture is implementable",
                "CSV is valid",
                "Download succeeds",
            )
        ],
        "deferred_findings": [],
    }
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="A bounded Epic is needed",
            proposal=proposal,
        )
        parent = kb.get_task(conn, result["task_id"])
        parent_contract = kb.get_work_contract(conn, parent.work_contract_id)
        stories = [kb.get_task(conn, task_id) for task_id in kb.list_epic_members(
            conn, result["task_id"]
        )]
        story_contracts = [
            kb.get_work_contract(conn, story.work_contract_id) for story in stories
        ]
    finally:
        conn.close()

    assert result["status"] == "qualified"
    assert sorted(story.title for story in stories) == [
        "Eksportér data",
        "Hent CSV — København",
    ]
    assert all(
        kanban_intake.verify_work_contract(envelope).valid
        for envelope in [parent_contract, *story_contracts]
    )
    assert [(story.current_step_key, story.assignee, story.status) for story in stories] == [
        ("architecture", "architect", "ready"),
        ("architecture", "architect", "ready"),
    ]


def test_dependency_is_ignored_at_architecture_then_reapplied_at_development(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-dependencies")
    conn = kb.connect(board=board)
    _active_intake(conn, monkeypatch, session_id="parent", title="Parent")
    parent_proposal = _proposal()
    parent_proposal["work"]["title"] = "Parent"
    parent = kanban_po_intake.decide_product_owner_intake(
        conn,
        board=board,
        disposition="accepted",
        reason="Parent is required",
        proposal=parent_proposal,
    )["task_id"]

    _active_intake(
        conn, monkeypatch, now=200, session_id="child", title="Child"
    )
    child_proposal = _proposal()
    child_proposal["work"]["title"] = "Child"
    child_proposal["routing"]["dependencies"] = [parent]
    child = kanban_po_intake.decide_product_owner_intake(
        conn,
        board=board,
        disposition="accepted",
        reason="Child depends on parent implementation",
        proposal=child_proposal,
    )["task_id"]

    assert kb.get_task(conn, child).status == "ready"
    with kb.authorized_governance_write(), kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?",
            (child,),
        )
    with kb.authorized_governance_write():
        assert kb.unblock_task(conn, child)
    assert kb.get_task(conn, child).status == "ready"
    claimed = kb.claim_task(conn, child, board=board, claimer="architect")
    assert claimed is not None
    assert kb.handoff(
        conn,
        child,
        board=board,
        summary="Architecture complete",
        expected_run_id=claimed.current_run_id,
        expected_phase="architecture",
    )
    assert (kb.get_task(conn, child).current_step_key, kb.get_task(conn, child).status) == (
        "development",
        "todo",
    )

    with kb.authorized_governance_write(), kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    assert kb.recompute_ready(conn) == 1
    assert kb.get_task(conn, child).status == "ready"
    conn.close()


def test_materialization_failure_rolls_back_every_governance_write(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_intake, kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-rollback")
    conn = kb.connect(board=board)
    intake_id, run = _active_intake(conn, monkeypatch)
    monkeypatch.setattr(
        kanban_intake,
        "_create_materialized_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected materialization failure")
        ),
    )
    with pytest.raises(RuntimeError, match="injected materialization"):
        kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Valid but injected failure",
            proposal=_proposal(),
        )

    assert conn.execute("SELECT COUNT(*) FROM work_contracts").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM qualification_intake_decisions"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert kb.get_qualification_intake(conn, intake_id)["status"] == "running"
    assert kb.get_qualification_intake_run(conn, run["id"])["status"] == "running"
    conn.close()


def test_explicit_rejection_is_terminal_and_creates_no_card(tmp_path, monkeypatch):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-rejected")
    conn = kb.connect(board=board)
    intake_id, _run = _active_intake(conn, monkeypatch)
    result = kanban_po_intake.decide_product_owner_intake(
        conn,
        board=board,
        disposition="rejected",
        reason="Conflicts with explicit product policy",
    )

    assert result["status"] == "rejected"
    assert kb.get_qualification_intake(conn, intake_id)["status"] == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert "explicitly_rejected" in [
        event["kind"]
        for event in kb.list_qualification_intake_events(conn, intake_id)
    ]
    conn.close()
