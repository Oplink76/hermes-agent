from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync import (
    SyncClassification,
    SyncResult,
    SyncState,
)
from ops.cloudadvisor.hermes_ops.sync_resolution import (
    ResolutionRecordError,
    ResolutionRecordArtifact,
    freeze_resolution_record,
)


CANDIDATE = "a" * 40


def candidate(record: Path, evidence_dir: Path) -> SyncResult:
    return SyncResult(
        state=SyncState.PR_UPDATED,
        candidate_sha=CANDIDATE,
        classification=SyncClassification.MINOR_REVIEW_REQUIRED,
        conflicted_files=("gateway/run.py",),
        resolution_record=record,
        resolution_evidence_dir=evidence_dir,
    )


def raw_record(evidence_dir: Path) -> Path:
    evidence_dir.mkdir(parents=True)
    path = evidence_dir / "resolution.json"
    path.write_text(
        json.dumps(
            {
                "conflicts": [
                    {
                        "path": "gateway/run.py",
                        "decision": "preserve fork behavior",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_freeze_writes_canonical_immutable_candidate_bound_artifact(tmp_path: Path):
    evidence_dir = tmp_path / ".git" / "hermes-sync-evidence"
    record = raw_record(evidence_dir)

    artifact = freeze_resolution_record(
        tmp_path / "receipts", candidate(record, evidence_dir)
    )

    assert artifact.path.parent == tmp_path / "receipts" / "resolutions"
    assert artifact.path.name == f"resolution-{artifact.sha256}.json"
    assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o400
    loaded = ResolutionRecordArtifact.load(artifact.path)
    assert loaded.sha256 == artifact.sha256
    payload = json.loads(artifact.path.read_text(encoding="utf-8"))
    assert payload["candidate_sha"] == CANDIDATE
    assert payload["conflicts"][0]["path"] == "gateway/run.py"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_freeze_rejects_original_record_symlink(tmp_path: Path):
    evidence_dir = tmp_path / ".git" / "hermes-sync-evidence"
    evidence_dir.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text('{"conflicts": []}', encoding="utf-8")
    record = evidence_dir / "resolution.json"
    record.symlink_to(target)

    with pytest.raises(ResolutionRecordError, match="regular file"):
        freeze_resolution_record(
            tmp_path / "receipts", candidate(record, evidence_dir)
        )


def test_freeze_rejects_record_outside_expected_evidence_directory(tmp_path: Path):
    evidence_dir = tmp_path / ".git" / "hermes-sync-evidence"
    evidence_dir.mkdir(parents=True)
    outside = raw_record(tmp_path / "other")

    with pytest.raises(ResolutionRecordError, match="evidence directory"):
        freeze_resolution_record(
            tmp_path / "receipts", candidate(outside, evidence_dir)
        )
