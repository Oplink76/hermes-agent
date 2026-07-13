from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.decision_packet import (
    DecisionPacket,
    EscalationDecisionPacket,
    load_escalation_decision_packet,
    publish_escalation_decision_packet,
    write_decision_packet,
)
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncResult,
    AutonomousSyncState,
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


def test_escalation_packet_is_canonical_content_addressed_and_safe(tmp_path: Path):
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        candidate_sha="c" * 40,
        pr_number=7,
        merge_sha="d" * 40,
        fork_main_sha="e" * 40,
        installed_sha="f" * 40,
        needs_ole=True,
        reason="raw subprocess output with token=secret-must-not-leak",
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
        affected_files=("ops/sync.py",),
        rollback_state="not_started",
        revert_state="not_started",
    )

    artifact = publish_escalation_decision_packet(
        result,
        fingerprint="1" * 64,
        trusted_root=tmp_path,
        repo_slug="Oplink76/hermes-agent",
    )

    assert artifact.path.parent == tmp_path / "decision-packets" / ("1" * 64)
    assert artifact.path.name == f"{artifact.sha256}.json"
    raw = artifact.path.read_text(encoding="utf-8")
    assert "secret-must-not-leak" not in raw
    assert "secret-must-not-leak" not in artifact.details_path.read_text(
        encoding="utf-8"
    )
    packet = load_escalation_decision_packet(
        artifact.path,
        trusted_root=tmp_path,
    )
    assert packet == EscalationDecisionPacket(
        schema_version=1,
        escalation_fingerprint="1" * 64,
        recommendation="Wait",
        summary="Automation stopped at github_authority (GITHUB_AUTHORITY_INVALID).",
        actions=("Approve", "Wait", "Details"),
        state="NEEDS_OLE",
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
        repo_slug="Oplink76/hermes-agent",
        candidate_sha="c" * 40,
        pr_number=7,
        merge_sha="d" * 40,
        fork_main_sha="e" * 40,
        installed_sha="f" * 40,
        affected_files=("ops/sync.py",),
        rollback_state="not_started",
        rollback_sha=None,
        revert_state="not_started",
        revert_sha=None,
        details_artifact=str(artifact.details_path),
    )
    if os.name != "nt":
        assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o600


def test_confirmed_major_packet_links_evidence_without_copying_findings(tmp_path: Path):
    evidence_reference = f"conflict-reviews/review-{'e' * 64}.json"
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        candidate_sha="c" * 40,
        pr_number=7,
        needs_ole=True,
        reason="kanban release gate is absent",
        reason_code="CONFLICT_REVIEW_CONFIRMED_MAJOR",
        failed_gate="conflict_review",
        details_artifact=evidence_reference,
    )

    artifact = publish_escalation_decision_packet(
        result,
        fingerprint="2" * 64,
        trusted_root=tmp_path,
        repo_slug="Oplink76/hermes-agent",
    )

    packet = load_escalation_decision_packet(
        artifact.path,
        trusted_root=tmp_path,
    )
    assert packet.summary == (
        "Automation stopped at conflict_review "
        "(CONFLICT_REVIEW_CONFIRMED_MAJOR)."
    )
    details = json.loads(artifact.details_path.read_text(encoding="utf-8"))
    assert details["controller_details_artifact"] == evidence_reference
    assert "kanban release gate is absent" not in artifact.path.read_text(
        encoding="utf-8"
    )


def test_escalation_packet_loader_rejects_path_outside_trusted_root(tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="trusted decision-packet root"):
        load_escalation_decision_packet(
            outside,
            trusted_root=tmp_path / "trusted",
        )


def test_escalation_packet_rejects_generic_placeholder_evidence(tmp_path: Path):
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        needs_ole=True,
    )

    with pytest.raises(ValueError, match="stable reason code"):
        publish_escalation_decision_packet(
            result,
            fingerprint="1" * 64,
            trusted_root=tmp_path,
            repo_slug="Oplink76/hermes-agent",
        )


def test_escalation_packet_loader_rejects_unknown_fields(tmp_path: Path):
    result = AutonomousSyncResult(
        state=AutonomousSyncState.NEEDS_OLE,
        needs_ole=True,
        reason_code="GITHUB_AUTHORITY_INVALID",
        failed_gate="github_authority",
    )
    artifact = publish_escalation_decision_packet(
        result,
        fingerprint="1" * 64,
        trusted_root=tmp_path,
        repo_slug="Oplink76/hermes-agent",
    )
    payload = json.loads(artifact.path.read_text(encoding="utf-8"))
    payload["raw_logs"] = "must be rejected"
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    path = artifact.path.with_name(f"{digest}.json")
    path.write_bytes(encoded)
    if os.name != "nt":
        path.chmod(0o600)

    with pytest.raises(ValueError, match="fields do not match schema"):
        load_escalation_decision_packet(path, trusted_root=tmp_path)
