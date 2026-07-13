from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.upstream_sync_status import SyncStatus
from ops.cloudadvisor.hermes_ops.sync_deployment_checkpoint import (
    PendingDeploymentCheckpoint,
    write_pending_deployment,
)


@pytest.fixture(autouse=True)
def _no_pending_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        web_server,
        "_upstream_sync_receipt_root",
        lambda: tmp_path / "no-pending-receipts",
    )


def _write_status(
    path: Path,
    *,
    upstream_behind: int,
    sync_state: str = "PR_UPDATED",
) -> None:
    SyncStatus(
        schema_version=1,
        checked_at=datetime.now(timezone.utc).isoformat(),
        upstream_behind=upstream_behind,
        fork_behind=0,
        sync_state=sync_state,
        sync_pr_number=7,
        required_check="All required checks pass",
        fork_main_sha="a" * 40,
        installed_sha="a" * 40,
        escalation_fingerprint=None,
    ).write(path)


def _client() -> TestClient:
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


def test_installed_current_does_not_hide_upstream_backlog(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(status_file, upstream_behind=54)
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 0)
    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path)

    payload = _client().get("/api/hermes/update/check?force=true").json()

    assert payload["behind"] == 0
    assert payload["fork_behind"] == 0
    assert payload["update_available"] is False
    assert payload["upstream_behind"] == 54
    assert payload["sync_state"] == "PR_UPDATED"
    assert payload["sync_pr_number"] == 7
    assert payload["sync_required_check"] == "All required checks pass"
    assert payload["installed_sha"] == "a" * 40


def test_missing_status_file_keeps_existing_update_contract(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        web_server,
        "_upstream_sync_status_path",
        lambda: tmp_path / "missing.json",
    )
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 3)
    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path)

    payload = _client().get("/api/hermes/update/check").json()

    assert payload["behind"] == 3
    assert payload["fork_behind"] == 3
    assert payload["update_available"] is True
    assert payload["upstream_behind"] is None
    assert payload["sync_state"] is None


def test_needs_ole_status_does_not_claim_upstream_is_syncing(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(status_file, upstream_behind=54, sync_state="NEEDS_OLE")
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 0)

    payload = _client().get("/api/hermes/update/check").json()

    assert payload["message"] == (
        "Installed current · Official upstream sync needs attention"
    )
    assert "syncing" not in payload["message"]
    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["sync_update_blocked"] is True


def test_safe_rollback_suppresses_update_action_and_shows_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(
        status_file,
        upstream_behind=2,
        sync_state="ROLLED_BACK_REVERTED",
    )
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 1)

    payload = _client().get("/api/hermes/update/check").json()

    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["sync_update_blocked"] is True
    assert "recovery active after safe rollback" in payload["message"]


def test_needs_ole_blocks_direct_update_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(status_file, upstream_behind=2, sync_state="NEEDS_OLE")
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    spawned: list[object] = []
    monkeypatch.setattr(
        web_server,
        "_spawn_hermes_action",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    payload = _client().post("/api/hermes/update").json()

    assert payload["ok"] is False
    assert payload["error"] == "upstream_sync_update_blocked"
    assert spawned == []


@pytest.mark.parametrize("stage", ["merge_intent", "merged_pending_deploy"])
def test_pending_deployment_blocks_check_and_direct_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    monkeypatch.setattr(web_server, "_pending_sync_deployment_state", lambda: stage)
    monkeypatch.setattr(
        web_server,
        "_upstream_sync_status_path",
        lambda: tmp_path / "missing-status.json",
    )
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 1)
    spawned: list[object] = []
    monkeypatch.setattr(
        web_server,
        "_spawn_hermes_action",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    check = _client().get("/api/hermes/update/check").json()
    direct = _client().post("/api/hermes/update").json()

    assert check["update_available"] is False
    assert check["can_apply"] is False
    assert check["sync_update_blocked"] is True
    assert check["sync_deployment_state"] == stage
    assert direct["error"] == "upstream_sync_update_blocked"
    assert spawned == []


def test_malformed_pending_pointer_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "receipts"
    pointer = root / "deployment" / "pending.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text('{"unexpected":true}\n', encoding="utf-8")
    monkeypatch.setattr(web_server, "_upstream_sync_receipt_root", lambda: root)

    assert web_server._pending_sync_deployment_state() == "crossed_invalid"


def test_real_trusted_merge_intent_pointer_blocks_updates(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "receipts"
    write_pending_deployment(
        root,
        PendingDeploymentCheckpoint(
            schema_version=1,
            repo_slug="Oplink76/hermes-agent",
            stage="merge_intent",
            candidate_sha="a" * 40,
            candidate_tree_sha="b" * 40,
            pr_number=7,
            pr_head_sha="a" * 40,
            base_sha="c" * 40,
            upstream_sha="d" * 40,
            premerge_receipt_path=str((tmp_path / "premerge.json").resolve()),
            premerge_receipt_sha256="e" * 64,
            merge_sha=None,
            final_receipt_path=None,
            final_receipt_sha256=None,
            install_root=str((tmp_path / "install").resolve()),
            previous_installed_sha="f" * 40,
            terminal_reason=None,
            terminal_reason_code=None,
            terminal_failed_gate=None,
            rollback_state=None,
            rollback_sha=None,
            revert_state=None,
            revert_sha=None,
        ),
    )
    monkeypatch.setattr(web_server, "_upstream_sync_receipt_root", lambda: root)

    assert web_server._pending_sync_deployment_state() == "merge_intent"
