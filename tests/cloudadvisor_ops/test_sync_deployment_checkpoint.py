from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync_deployment_checkpoint import (
    PendingDeploymentCheckpoint,
    SyncDeploymentCheckpointError,
    clear_pending_deployment,
    load_pending_deployment,
    write_pending_deployment,
)


REPO = "Oplink76/hermes-agent"


def _checkpoint(**updates: object) -> PendingDeploymentCheckpoint:
    values: dict[str, object] = {
        "schema_version": 1,
        "repo_slug": REPO,
        "stage": "merged_pending_deploy",
        "candidate_sha": "a" * 40,
        "candidate_tree_sha": "9" * 40,
        "pr_number": 7,
        "pr_head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "upstream_sha": "c" * 40,
        "premerge_receipt_path": "/trusted/receipts/sync-premerge-" + "8" * 64 + ".json",
        "premerge_receipt_sha256": "8" * 64,
        "merge_sha": "d" * 40,
        "final_receipt_path": "/trusted/receipts/sync-merged-" + "e" * 64 + ".json",
        "final_receipt_sha256": "e" * 64,
        "install_root": "/trusted/install",
        "previous_installed_sha": "f" * 40,
        "terminal_reason": None,
        "terminal_reason_code": None,
        "terminal_failed_gate": None,
        "rollback_state": None,
        "rollback_sha": None,
        "revert_state": None,
        "revert_sha": None,
    }
    values.update(updates)
    return PendingDeploymentCheckpoint(**values)


def test_pending_deployment_is_content_addressed_and_discoverable(tmp_path: Path):
    checkpoint = _checkpoint()
    artifact = write_pending_deployment(tmp_path, checkpoint)
    expected = (
        json.dumps(
            checkpoint.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")

    assert artifact.path.parent == tmp_path / "deployment"
    assert artifact.path.name == f"pending-deployment-{artifact.sha256}.json"
    assert artifact.path.read_bytes() == expected
    assert artifact.sha256 == hashlib.sha256(expected).hexdigest()
    assert load_pending_deployment(tmp_path, repo_slug=REPO) == checkpoint
    pointer = artifact.path.parent / "pending.json"
    if os.name != "nt":
        assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o400
        assert stat.S_IMODE(pointer.stat().st_mode) == 0o600


@pytest.mark.parametrize("mutation", ["artifact", "pointer_repo", "pointer_digest"])
def test_pending_deployment_rejects_tampered_or_cross_repo_state(
    tmp_path: Path, mutation: str
):
    artifact = write_pending_deployment(tmp_path, _checkpoint())
    pointer = artifact.path.parent / "pending.json"
    if mutation == "artifact":
        if os.name != "nt":
            artifact.path.chmod(0o600)
        artifact.path.write_text("{}\n", encoding="utf-8")
        if os.name != "nt":
            artifact.path.chmod(0o400)
    else:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        if mutation == "pointer_repo":
            payload["repo_slug"] = "Other/hermes-agent"
        else:
            payload["artifact_sha256"] = hashlib.sha256(b"other").hexdigest()
        pointer.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(SyncDeploymentCheckpointError):
        load_pending_deployment(tmp_path, repo_slug=REPO)


def test_pending_deployment_pointer_clears_only_exact_checkpoint(tmp_path: Path):
    first = write_pending_deployment(tmp_path, _checkpoint())
    second_value = _checkpoint(merge_sha="1" * 40)
    second = write_pending_deployment(tmp_path, second_value)

    clear_pending_deployment(tmp_path, sha256=first.sha256)
    assert load_pending_deployment(tmp_path, repo_slug=REPO) == second_value

    clear_pending_deployment(tmp_path, sha256=second.sha256)
    assert load_pending_deployment(tmp_path, repo_slug=REPO) is None


def test_pending_deployment_rejects_crossed_candidate_and_head(tmp_path: Path):
    with pytest.raises(SyncDeploymentCheckpointError, match="candidate"):
        write_pending_deployment(
            tmp_path,
            _checkpoint(pr_head_sha="1" * 40),
        )


def test_merge_intent_is_durable_without_remote_merge_identity(tmp_path: Path):
    intent = _checkpoint(
        stage="merge_intent",
        merge_sha=None,
        final_receipt_path=None,
        final_receipt_sha256=None,
    )

    write_pending_deployment(tmp_path, intent)

    assert load_pending_deployment(tmp_path, repo_slug=REPO) == intent


def test_terminal_failure_requires_safe_structured_evidence(tmp_path: Path):
    terminal = _checkpoint(
        stage="failed_terminal",
        terminal_reason="protected revert failed",
        terminal_reason_code="AUTOMATIC_RECOVERY_FAILED",
        terminal_failed_gate="protected_recovery",
        rollback_state="rolled_back_healthy",
        rollback_sha="f" * 40,
        revert_state="NEEDS_OLE",
        revert_sha=None,
    )

    write_pending_deployment(tmp_path, terminal)

    assert load_pending_deployment(tmp_path, repo_slug=REPO) == terminal
