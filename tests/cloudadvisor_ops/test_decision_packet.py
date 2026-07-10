from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from ops.cloudadvisor.hermes_ops.decision_packet import (
    DecisionPacket,
    write_decision_packet,
)


def packet(**overrides) -> DecisionPacket:
    values = {
        "pr_number": 41,
        "candidate_sha": "new-sha",
        "upstream_commit_count": 12,
        "fork_custom_areas_touched": ("gateway", "kanban"),
        "test_results": ({"name": "local-gate", "status": "passed"},),
        "ci_url": "https://github.com/Oplink76/hermes-agent/actions/runs/1",
        "ci_status": "success",
        "independent_review_status": "green",
        "risk_explanation": "Touches fork gateway and Kanban customizations.",
        "rollback_sha": "base-sha",
        "recommendation": "Approve",
    }
    values.update(overrides)
    return DecisionPacket(**values)


def test_approve_is_available_only_after_ci_review_and_tests_are_green():
    assert packet().approve_available is True
    assert packet(ci_status="pending").approve_available is False
    assert packet(independent_review_status="pending").approve_available is False
    assert (
        packet(
            test_results=({"name": "local-gate", "status": "failed"},)
        ).approve_available
        is False
    )


def test_writer_creates_matching_json_markdown_and_hash(tmp_path: Path):
    artifacts = write_decision_packet(packet(), tmp_path / "sync-decision")

    payload = json.loads(artifacts.json_path.read_text())
    assert payload["approve_available"] is True
    assert payload["candidate_sha"] == "new-sha"
    assert payload["rollback_sha"] == "base-sha"
    markdown = artifacts.markdown_path.read_text()
    assert "Approve" in markdown
    assert "All required checks" in markdown
    expected = hashlib.sha256(artifacts.json_path.read_bytes()).hexdigest()
    assert artifacts.sha256 == expected
    if os.name != "nt":
        assert stat.S_IMODE(artifacts.markdown_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".sync-decision.md.*")) == []
