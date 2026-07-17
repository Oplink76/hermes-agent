from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_qualifier as qualifier


PHASES = ["backlog", "architecture", "development", "test", "review", "release_measure"]
ASSIGNEES = {
    "backlog": "productowner",
    "architecture": "architect",
    "development": "developer",
    "test": "tester",
    "review": "reviewer",
    "release_measure": None,
}


def _assessment(entry_phase: str, *, review: bool = False):
    skipped = PHASES[: PHASES.index(entry_phase)]
    value = {
        "reason": "Submitted evidence satisfies earlier phases" if skipped else "New work",
        "skipped_phases": [
            {
                "phase": phase,
                "reason": f"{phase} already satisfied",
                "evidence": [f"evidence:{phase}"],
            }
            for phase in skipped
        ],
        "evidence": [f"evidence:{phase}" for phase in skipped],
    }
    if review:
        value["provenance"] = {
            "writer": {"profile": "developer", "artifact": "commit:abc"},
            "tester": {"profile": "tester", "artifact": "suite:green"},
        }
    return value


def _decision(title: str, entry_phase: str, work_type: str, *, path: str = "hermes"):
    index = PHASES.index(entry_phase)
    next_phase = PHASES[index + 1] if index + 1 < len(PHASES) else "done"
    return {
        "qualification_path": path,
        "work": {
            "item_kind": "card",
            "work_type": work_type,
            "title": title,
            "outcome": f"Deliver {title}",
            "scope": [title],
            "out_of_scope": ["unrelated work"],
        },
        "routing": {
            "entry_phase": entry_phase,
            "assignee": ASSIGNEES[entry_phase],
            "epic_id": None,
            "dependencies": [],
        },
        "entry_assessment": _assessment(entry_phase, review=entry_phase == "review"),
        "handover": {
            "deliverables": ["scoped result"],
            "required_evidence": ["tests or verification"],
            "done_when": ["contract outcome is satisfied"],
            "next_phase": next_phase,
            "next_role": ASSIGNEES.get(next_phase),
        },
        "rules": {
            "allowed": ["work inside scope"],
            "forbidden": ["bypass independent review"],
        },
        "classification": [f"framework:{work_type}", f"path:{path}"],
    }


def test_real_entry_shapes_qualify_or_reject_without_bypassing_routing(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = "qualified-flow"
    kb.ensure_product_board_defaults(board)

    brief_path = "/tmp/product-brief.md"
    with kb.connect(board=board) as conn:
        discovery_id = kb.create_task(conn, title="PO discovery")
        cursor = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status, started_at, ended_at, summary
            ) VALUES (?, 'productowner', 'backlog', 'done', 1, 2, ?)
            """,
            (discovery_id, f"Product brief: {brief_path}"),
        )
        po_run_id = int(cursor.lastrowid)

    metadata = kb.read_board_metadata(board)
    metadata["qualification"]["required"] = True
    metadata["qualification"]["work_types"] = list(qualifier.DEFAULT_WORK_TYPES)
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")

    cases = [
        ("PO user story", "backlog", "story", "po"),
        ("Cross-cutting architecture", "architecture", "spike", "hermes"),
        ("Small maintenance bug", "development", "bug", "hermes"),
        ("Existing tested patch", "review", "maintenance", "hermes"),
        ("Documentation operations", "development", "ops", "hermes"),
    ]

    with kb.connect(board=board) as conn:
        materialized = []
        for title, phase, work_type, path in cases:
            decision = _decision(title, phase, work_type, path=path)
            evidence_refs = [
                evidence
                for skipped in decision["entry_assessment"]["skipped_phases"]
                for evidence in skipped["evidence"]
            ]
            provenance = decision["entry_assessment"].get("provenance", {})
            evidence_refs.extend(
                value["artifact"] for value in provenance.values()
            )
            attachments = tuple(
                {"name": reference} for reference in evidence_refs
            )
            if path == "po":
                attachments += (
                    {"name": "product-brief.md", "path": brief_path},
                )
            receipt = qualifier.submit_request(
                conn,
                request={"title": title, "evidence": f"fixture:{phase}"},
                source="e2e",
                attachments=attachments,
            )
            if path == "po":
                decision["po_evidence"] = {
                    "run_id": po_run_id,
                    "artifact": brief_path,
                }
            result = qualifier.qualify_intake(
                conn,
                board=board,
                intake_id=receipt["intake_id"],
                model_call=lambda _prompt, value=decision: value,
                secret=b"test-only-secret",
                issued_at=100,
            )
            assert result["status"] == "qualified"
            task = kb.get_task(conn, result["task_id"])
            assert task is not None
            assert task.current_step_key == phase
            assert task.assignee == ASSIGNEES[phase]
            assert conn.execute(
                "SELECT 1 FROM epic_memberships WHERE task_id = ?", (task.id,)
            ).fetchone() is None
            materialized.append(task.id)

        invalid = qualifier.submit_request(
            conn,
            request={"title": "Invalid default assignee"},
            source="e2e",
        )
        invalid_decision = _decision(
            "Invalid default assignee", "development", "maintenance"
        )
        invalid_decision["routing"]["assignee"] = "default"
        rejected = qualifier.qualify_intake(
            conn,
            board=board,
            intake_id=invalid["intake_id"],
            model_call=lambda _prompt: invalid_decision,
            secret=b"test-only-secret",
            issued_at=100,
        )

        assert rejected["status"] == "rejected"
        assert len(materialized) == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE work_contract_id IS NOT NULL"
        ).fetchone()[0] == 5
