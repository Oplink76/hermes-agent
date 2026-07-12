from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.sync_reconstruction_checkpoint import (
    PendingReconstructionCheckpoint,
    ReconstructionCheckpointError,
    clear_pending_reconstruction,
    load_pending_reconstruction,
    write_pending_reconstruction,
)


REPO = "Oplink76/hermes-agent"


def _checkpoint(**updates: object) -> PendingReconstructionCheckpoint:
    values: dict[str, object] = {
        "schema_version": 1,
        "repo_slug": REPO,
        "failed_base_sha": "a" * 40,
        "failed_upstream_sha": "b" * 40,
        "failed_candidate_sha": "c" * 40,
        "failed_candidate_tree_sha": "d" * 40,
        "failed_pr_number": 7,
        "failed_merge_sha": "e" * 40,
        "revert_main_sha": "f" * 40,
        "previous_healthy_installed_sha": "1" * 40,
        "rolling_candidate_sha": "2" * 40,
        "pending_upstream_sha": "3" * 40,
        "reason": "official upstream advanced during post-revert repair",
        "resume_attempts": 0,
    }
    values.update(updates)
    return PendingReconstructionCheckpoint(**values)


def test_pending_reconstruction_is_content_addressed_and_discoverable(
    tmp_path: Path,
):
    artifact = write_pending_reconstruction(tmp_path, _checkpoint())

    assert artifact.path.parent == tmp_path / "reconstruction"
    assert artifact.path.name == f"pending-reconstruction-{artifact.sha256}.json"
    pointer = artifact.path.parent / "pending.json"
    pointer_payload = json.loads(pointer.read_text(encoding="utf-8"))
    assert pointer_payload == {
        "artifact_sha256": artifact.sha256,
        "repo_slug": REPO,
        "schema_version": 1,
    }
    assert load_pending_reconstruction(tmp_path, repo_slug=REPO) == _checkpoint()
    if os.name != "nt":
        assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o400
        assert stat.S_IMODE(pointer.stat().st_mode) == 0o600


def test_pending_reconstruction_rejects_symlinked_checkpoint_scope(tmp_path: Path):
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "reconstruction").symlink_to(external, target_is_directory=True)

    with pytest.raises(ReconstructionCheckpointError, match="scope"):
        write_pending_reconstruction(tmp_path, _checkpoint())


@pytest.mark.parametrize("mutation", ["unknown_artifact", "wrong_digest", "cross_repo"])
def test_pending_reconstruction_rejects_untrusted_or_cross_repo_state(
    tmp_path: Path,
    mutation: str,
):
    artifact = write_pending_reconstruction(tmp_path, _checkpoint())
    pointer = artifact.path.parent / "pending.json"
    if mutation == "unknown_artifact":
        payload = json.loads(artifact.path.read_text(encoding="utf-8"))
        payload["surprise"] = True
        content = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        mutated = artifact.path.parent / f"pending-reconstruction-{digest}.json"
        mutated.write_bytes(content)
        if os.name != "nt":
            mutated.chmod(0o400)
        pointer.write_text(
            json.dumps(
                {
                    "artifact_sha256": digest,
                    "repo_slug": REPO,
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
    elif mutation == "wrong_digest":
        pointer.write_text(
            json.dumps(
                {
                    "artifact_sha256": "0" * 64,
                    "repo_slug": REPO,
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        pointer.write_text(
            json.dumps(
                {
                    "artifact_sha256": artifact.sha256,
                    "repo_slug": "Other/hermes-agent",
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    with pytest.raises(ReconstructionCheckpointError):
        load_pending_reconstruction(tmp_path, repo_slug=REPO)


def test_pending_reconstruction_pointer_clears_only_the_exact_checkpoint(
    tmp_path: Path,
):
    first = write_pending_reconstruction(tmp_path, _checkpoint())
    second_checkpoint = _checkpoint(pending_upstream_sha="4" * 40)
    write_pending_reconstruction(tmp_path, second_checkpoint)

    clear_pending_reconstruction(tmp_path, sha256=first.sha256)
    assert load_pending_reconstruction(tmp_path, repo_slug=REPO) == second_checkpoint

    second = write_pending_reconstruction(tmp_path, second_checkpoint)
    clear_pending_reconstruction(tmp_path, sha256=second.sha256)
    assert load_pending_reconstruction(tmp_path, repo_slug=REPO) is None
